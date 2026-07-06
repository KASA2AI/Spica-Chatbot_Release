"""watch_anime orchestration flow (Phase 3) -- pure domain logic, no Qt, no host.

The business flow behind the watch_anime tool, kept OUT of the host assembly so
AppHost stays thin (review): ready-gate -> merge query/episode -> library dedup
-> coordinator.resolve_episode -> outcome mapping / event construction / playback
error mapping. All effects (source I/O, playback, event emit, id/clock) are
INJECTED callables, so this is unit-testable with fakes and the host closure only
supplies the real ports.

Failures raise ``WatchAnimeError(code, message)`` with a STABLE code; the host
closure translates it into the runtime's ``ScreenToolError`` envelope (so a
failure is never a normal ack dict the followup prompt could misread). Qt-free.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from spica.anime.coordinator import (
    AMBIGUOUS,
    CANCELLED,
    MATCHED,
    NEED_EPISODE,
    NOT_FOUND,
    RESOLVE_TIMEOUT,
    SOURCE_ERROR,
    resolve_episode,
)
from spica.anime.library import AnimeLibrary
from spica.anime.models import LATEST, EpisodeRef
from spica.anime.resolver import canonical_episode_key, parse_query
from spica.core.anime_events import AnimeRequestEvent
from spica.ports.anime_source import AnimeSourcePort
from spica.ports.media_player import MediaPlayerError


class WatchAnimeError(Exception):
    """A stable tool-facing failure; the host closure maps ``code`` to a
    ScreenToolError. Never returned as a result dict (review #4)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def merge_episode_ref(query: str, episode: int | str | None) -> EpisodeRef:
    """parse the query, then let an explicit ``episode`` arg override the
    episode parsed from the query text ("latest" -> the LATEST sentinel)."""
    ref = parse_query(query)
    if episode is None:
        return ref
    if isinstance(episode, str):
        ep: int | str = LATEST if episode.strip().lower() == "latest" else episode.strip()
        # a non-"latest" string that isn't a number stays as-is -> resolver treats
        # unknown episode spec as "need episode" (never a silent guess)
        if isinstance(ep, str) and ep != LATEST and ep.isdigit():
            ep = int(ep)
    else:
        ep = episode
    return EpisodeRef(title_query=ref.title_query, season=ref.season, episode=ep)


def _map_outcome(result: Any) -> WatchAnimeError:
    o = result.outcome
    if o == NEED_EPISODE:
        return WatchAnimeError("ANIME_NEED_EPISODE", "想看第几集呀？")
    if o == AMBIGUOUS:
        cands = list(result.match.candidates) if result.match else []
        titles = "、".join(c.display_title or c.parsed.name_zh for c in cands[:4])
        return WatchAnimeError("ANIME_AMBIGUOUS", f"找到好几个，帮我确认下：{titles}")
    if o == NOT_FOUND:
        return WatchAnimeError("ANIME_NOT_FOUND", "没找到这部番的这一集")
    if o == SOURCE_ERROR:
        return WatchAnimeError("ANIME_SOURCE_ERROR", "来源都连不上，稍后再试")
    if o == RESOLVE_TIMEOUT:
        return WatchAnimeError("ANIME_RESOLVE_TIMEOUT", "找太久了，稍后再试")
    if o == CANCELLED:
        return WatchAnimeError("ANIME_CANCELLED", "这次先不找了")
    return WatchAnimeError("ANIME_SOURCE_ERROR", f"未知结果：{o}")


def run_watch_request(
    *,
    query: str,
    episode: int | str | None,
    config: Any,
    sources: Sequence[AnimeSourcePort],
    library: AnimeLibrary,
    play_file: Callable[[str], None],
    emit: Callable[[AnimeRequestEvent], None],
    is_ready: Callable[[], bool],
    new_id: Callable[[], str],
    now: Callable[[], str],
    in_flight: Callable[[], dict | None] | None = None,
) -> dict[str, Any]:
    """Return a small fire-and-ack dict on success; raise WatchAnimeError on any
    failure. ``config`` is the live ``AppConfig.anime`` section. ``in_flight``
    reports the current download as ``{"progress": 0..1, "title": str}`` or None
    (F8); v1 downloads are single-flight, so a non-None answer means BUSY."""
    if not getattr(config, "enabled", False):
        raise WatchAnimeError("ANIME_DISABLED", "看番功能还没开启哦")
    if not is_ready():
        raise WatchAnimeError("ANIME_NOT_READY", "看番功能还没准备好（界面还没接上）")

    ref = merge_episode_ref(query, episode)

    # fast path: a concrete, already-downloaded episode -> play, skip the network.
    # The key MUST be the canonical one (F2) or the library dedup silently misses.
    if isinstance(ref.episode, int):
        hit = library.find(
            canonical_episode_key(ref.title_query, ref.season, ref.episode))
        if hit is not None:
            return _play(play_file, hit.file_path, hit.episode_key, hit.title)

    # busy gate (F8): AFTER the library fast path (a hit plays, that's never
    # "busy") and BEFORE any network resolve. Phase 3 wires lambda: None; the
    # Phase 4 worker will supply the real in-flight state.
    busy = in_flight() if in_flight is not None else None
    if busy is not None:
        pct = int(round(float(busy.get("progress") or 0.0) * 100))
        title = busy.get("title") or "上一集"
        raise WatchAnimeError(
            "ANIME_DOWNLOAD_BUSY", f"《{title}》还在下载中（{pct}%），先等它下完哦")

    result = resolve_episode(
        ref, sources,
        quality=getattr(config, "quality", "1080p"),
        subtitle_pref=list(getattr(config, "subtitle_preference", []) or []),
        budget_seconds=getattr(config, "resolve_budget_seconds", None),
        per_source_timeout=getattr(config, "source_timeout_seconds", None),
    )
    if result.outcome != MATCHED or result.resource is None:
        raise _map_outcome(result)

    res = result.resource
    # a LATEST request resolves to a concrete episode -> dedup again by its key
    hit = library.find(res.episode_key)
    if hit is not None:
        return _play(play_file, hit.file_path, hit.episode_key, hit.title)

    event = AnimeRequestEvent(
        request_id=new_id(), query=query, title=res.display_title,
        episode_key=res.episode_key, source=res.source, locator=res.locator,
        display_title=res.display_title, size_bytes=res.size_bytes, created_at=now(),
    )
    emit(event)                                    # hand the download to the UI worker
    return {
        "status": "downloading", "request_id": event.request_id,
        "episode_key": res.episode_key, "title": res.display_title,
    }


def _play(play_file: Callable[[str], None], path: str, key: str,
          title: str) -> dict[str, Any]:
    try:
        play_file(path)                            # port is the single check point
    except MediaPlayerError as e:
        raise WatchAnimeError("ANIME_PLAYBACK_ERROR", str(e))
    return {"status": "playing", "episode_key": key, "title": title}
