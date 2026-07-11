"""Anime-watch domain assembly (Phase 3/4) -- WIRING + host-held write closures.

``install(host)`` builds the config-driven sources / torrent client / player /
library, wires the host watch_anime closure, and registers the tool. The business
flow lives in ``spica.anime.watch_flow`` (Host stays thin, review); this module
only injects real ports into it and maps its ``WatchAnimeError`` onto the runtime
``ScreenToolError`` envelope (so failures are ToolErrors, never ack dicts).

Phase 4 additions (all file I/O lives HERE -- spica/anime stays pure logic):
- library persistence: the host is the SINGLE write point (P1-6). Loaded at
  install from ``cfg.library_file``; every change is an atomic JSON write
  (same-dir tmp + fsync + os.replace). A corrupt file is quarantined
  (renamed ``*.corrupt-<stamp>``), never silently overwritten.
- pending-download sidecar (``pending.json``, same dir): identity source for
  restart reconcile (P1-9) -- written when a download is handed to the UI,
  erased when it registers. Records carry EXPLICIT title/season/episode fields
  (the episode-key reverse parse is only the fallback at record time).
- host closures for the UI controller: ``anime_register_download`` (validates
  download_dir containment + media extension BEFORE registering),
  ``anime_mark_played``, ``anime_note_task_id``, ``anime_list_pending``,
  ``anime_drop_pending``, ``anime_is_played``, ``anime_play_file``.
- the F8 ``in_flight`` seam now live-reads ``host._anime_in_flight`` (attached
  by the UI together with the sink).

One lock guards library+pending+files: closures are called from BOTH the
ChatWorker thread (watch_flow inside a turn) and the GUI thread (controller).

INSTALL TIMING (review): call from ``AppHost.initialize()`` AFTER config / secrets
/ services exist -- adapters read ``config.anime`` and ``secrets`` at build time,
so registering in ``__init__`` (like the older tools) would read None.

``host`` stays duck-typed ``Any`` -- this module must not import AppHost
(app_host imports us; the reverse edge would be a cycle). Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from agent_tools.function_tools.screen.schema import ScreenToolError
from spica.adapters.anime_source.bilibili_space import BilibiliSpaceSource
from spica.adapters.anime_source.mikan import MikanRssSource
from spica.adapters.media_player.system_default import SystemDefaultPlayer
from spica.adapters.torrent.qbittorrent import QBittorrentClient
from spica.adapters.tools.cancel_anime_download import CancelAnimeDownloadTool
from spica.adapters.tools.watch_anime import WatchAnimeTool
from spica.anime.library import AnimeLibrary, LibraryEntry
from spica.anime.watch_flow import WatchAnimeError, run_watch_request
from spica.core.anime_events import AnimeCancelRequestEvent, AnimeRequestEvent
from spica.ports.anime_source import AnimeSourcePort
from spica.ports.media_player import MEDIA_EXTENSIONS

_LOG = logging.getLogger(__name__)

# repo root (assemblies -> host -> spica -> repo), same convention as
# spica/config/manager.py::_REPO_ROOT.
_REPO_ROOT = Path(__file__).resolve().parents[3]

# reverse of models.episode_key's pinned "title|sN|eM" format. Greedy title +
# end anchor => the RIGHTMOST "|sN|eM" wins, so a title containing "|s2|e3"
# still parses correctly.
_EPISODE_KEY_RE = re.compile(r"^(?P<title>.*)\|s(?P<season>\d+)\|e(?P<episode>\d+)$")


def resolve_data_path(raw: str) -> Path:
    """expanduser, then anchor RELATIVE paths at the repo root (never the cwd)."""
    p = Path(raw).expanduser()
    return p if p.is_absolute() else _REPO_ROOT / p


def parse_episode_key(key: str) -> "tuple[str, int, int] | None":
    """``"无职转生|s3|e1"`` -> ``("无职转生", 3, 1)``; None when the key does not
    match the pinned format (callers must handle it -- never guess silently)."""
    m = _EPISODE_KEY_RE.match(key or "")
    if m is None:
        return None
    return m.group("title"), int(m.group("season")), int(m.group("episode"))


# -- JSON persistence helpers (host-side single write point, P1-6) ------------


def _atomic_write_json(path: Path, data: Any) -> None:
    """Same-dir tmp + fsync + os.replace: readers never observe a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _quarantine_corrupt(path: Path, reason: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        os.replace(path, target)
        _LOG.warning("anime store %s unreadable (%s); moved to %s, starting empty",
                     path, reason, target)
    except OSError as e:  # keep going -- an unreadable store must never block startup
        _LOG.warning("anime store %s unreadable (%s) and quarantine failed (%s)",
                     path, reason, e)


def load_library(path: Path) -> AnimeLibrary:
    if not path.exists():
        return AnimeLibrary()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("library json is not a list")
        return AnimeLibrary.from_json(data)
    except Exception as e:  # noqa: BLE001 -- corrupt store: quarantine, don't crash
        _quarantine_corrupt(path, str(e))
        return AnimeLibrary()


def load_pending(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("pending json is not a list")
        return [dict(p) for p in data if isinstance(p, dict)]
    except Exception as e:  # noqa: BLE001
        _quarantine_corrupt(path, str(e))
        return []


def resolve_media_file(path: Path, root: Path) -> Path:
    """Pre-registration gate (review): the library must only ever hold a real
    media file inside download_dir. A directory result (qbt folder torrent)
    picks its largest media file. Raises ValueError on any violation -- the
    entry is then NOT registered."""
    rp = path.expanduser().resolve()
    if not rp.is_relative_to(root):
        raise ValueError(f"outside download_dir: {rp}")
    if rp.is_dir():
        media = [f for f in rp.rglob("*")
                 if f.is_file() and f.suffix.lower() in MEDIA_EXTENSIONS]
        if not media:
            raise ValueError(f"no media file inside {rp}")
        rp = max(media, key=lambda f: f.stat().st_size).resolve()
        if not rp.is_relative_to(root):     # symlink escaping via the folder
            raise ValueError(f"media file resolves outside download_dir: {rp}")
    if not rp.is_file():
        raise ValueError(f"not a regular file: {rp}")
    if rp.suffix.lower() not in MEDIA_EXTENSIONS:
        raise ValueError(f"disallowed extension {rp.suffix!r}")
    return rp


def _available(host: Any) -> bool:
    """Supply predicate (live-read, fault-tolerant): enabled AND a UI sink is
    attached. Any missing attr -> False (never crash the registry)."""
    cfg = getattr(getattr(host, "config", None), "anime", None)
    if cfg is None or not getattr(cfg, "enabled", False):
        return False
    return getattr(host, "_anime_sink", None) is not None


def _active_anime_request(host: Any) -> "dict[str, str] | None":
    """Read a defensive snapshot of the UI-owned in-flight identity."""
    reader = getattr(host, "_anime_in_flight", None)
    if not callable(reader):
        return None
    try:
        state = reader()
    except Exception:  # noqa: BLE001 -- a broken state seam hides the tool
        return None
    if not isinstance(state, dict):
        return None
    request_id = state.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        return None
    title = state.get("title")
    return {
        # IDs are opaque identity tokens.  Whitespace is only an emptiness
        # check above; never normalize the value used for binding/comparison.
        "request_id": request_id,
        "title": str(title).strip() if title is not None else "",
    }


def _cancel_available(
    host: Any,
    tool: CancelAnimeDownloadTool,
) -> bool:
    """Stopping remains available for an active job even after anime is disabled."""
    # Availability may be queried repeatedly on a long-lived producer thread.
    # Clear first so a declined/abandoned prior turn cannot authorize this one.
    tool.clear_offer()
    if not callable(getattr(host, "_anime_sink", None)):
        return False
    active = _active_anime_request(host)
    if active is None:
        return False
    tool.bind_offer(active["request_id"])
    return True


def build_request_anime_cancel(host: Any) -> Callable[[str], dict[str, Any]]:
    """Build the Host-owned, identity-bound cancel submission action."""

    def _request_cancel(expected_request_id: str) -> dict[str, Any]:
        # The opaque identity came from THIS turn's offer.  Re-read only to
        # compare; execution must never rebind the request to the current job.
        expected = expected_request_id
        active = _active_anime_request(host)
        if (not expected or active is None
                or active["request_id"] != expected):
            raise ScreenToolError(
                "ANIME_CANCEL_REQUEST_STALE",
                "下载任务已经变化，这次停止请求不会作用到新的任务。",
            )
        sink = getattr(host, "_anime_sink", None)
        if not callable(sink):
            raise ScreenToolError(
                "ANIME_UI_NOT_READY", "动漫下载界面还没准备好，暂时无法停止。")
        try:
            sink(AnimeCancelRequestEvent(
                request_id=active["request_id"]))
        except Exception as exc:  # noqa: BLE001 -- act failures use ToolError envelopes
            _LOG.warning("anime cancel request submit failed: %s", exc)
            raise ScreenToolError(
                "ANIME_CANCEL_SUBMIT_FAILED", "停止请求没有提交成功，请再试一次。") from exc
        # This only acknowledges hand-off.  The UI worker reports the terminal
        # result later, so do not claim that deletion has already completed.
        return {"status": "submitted", **active}

    return _request_cancel


def _build_sources(cfg: Any, secrets: Any) -> list[AnimeSourcePort]:
    # bilibili main, mikan fallback (coordinator order). Constructors do NO I/O.
    # An EMPTY config list skips that source (P2-6, D2): a bare `mikan_base_urls:
    # []` must not crash startup (MikanRssSource enforces non-empty internally --
    # that invariant stays). Both empty -> no sources -> resolve returns a stable
    # ANIME_SOURCE_ERROR, never a startup crash.
    cookie = getattr(secrets, "bilibili_cookie", None)
    timeout = float(getattr(cfg, "source_timeout_seconds", 15) or 15)
    sources: list[AnimeSourcePort] = []
    spaces = list(cfg.bilibili_spaces)
    if spaces:
        sources.append(BilibiliSpaceSource(spaces, cookie=cookie, timeout=timeout))
    urls = list(cfg.mikan_base_urls)
    if urls:
        sources.append(MikanRssSource(urls, timeout=timeout))
    return sources


def _build_torrent(cfg: Any, secrets: Any, download_dir: str) -> QBittorrentClient:
    return QBittorrentClient(
        cfg.qbittorrent_url, download_dir,
        username=getattr(cfg, "qbittorrent_username", None),
        password=getattr(secrets, "qbittorrent_password", None),
    )


def _build_player(cfg: Any, download_dir: str) -> SystemDefaultPlayer:
    return SystemDefaultPlayer(download_dir,
                               player_command=(cfg.player_command or None))


def build_request_anime(
    host: Any,
    *,
    sources: Sequence[AnimeSourcePort],
    library: AnimeLibrary,
    play_file: Callable[[str], None],
    new_id: Callable[[], str] | None = None,
    now: Callable[[], str] | None = None,
    in_flight: Callable[[], "dict[str, Any] | None"] | None = None,
    mark_played: Callable[[str], None] | None = None,
    note_pending: Callable[[AnimeRequestEvent], None] | None = None,
) -> Callable[..., dict[str, Any]]:
    """The host watch_anime closure: injects live config + real ports into the
    pure flow and maps WatchAnimeError -> ScreenToolError (review #4). ``config``
    and the event sink are read LIVE from the host each call. ``in_flight`` is
    the busy seam (F8): install wires it to ``host._anime_in_flight`` (the UI
    controller's live state); the default reports None. ``note_pending`` records
    the handed-off download for restart reconcile (P1-9) BEFORE the event
    reaches the UI; ``mark_played`` is the persistence hook for every play."""
    new_id = new_id or (lambda: uuid.uuid4().hex)
    now = now or (lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    in_flight = in_flight or (lambda: None)

    def _emit(ev: AnimeRequestEvent) -> None:
        if note_pending is not None:
            note_pending(ev)                        # record first: crash-safe order
        host._anime_sink(ev)

    def _request_anime(query: str, episode: "int | str | None",
                       use_recent_unplayed: bool = False) -> dict[str, Any]:
        try:
            return run_watch_request(
                query=query, episode=episode,
                config=host.config.anime,                       # live
                sources=sources, library=library, play_file=play_file,
                emit=_emit,                                     # gated by is_ready
                is_ready=lambda: getattr(host, "_anime_sink", None) is not None,
                new_id=new_id, now=now, in_flight=in_flight,
                mark_played=mark_played,
                use_recent_unplayed=use_recent_unplayed,
            )
        except WatchAnimeError as e:
            raise ScreenToolError(e.code, e.message)

    return _request_anime


def install(
    host: Any,
    *,
    sources: Sequence[AnimeSourcePort] | None = None,
    torrent: QBittorrentClient | None = None,
    player: Any = None,
    library: AnimeLibrary | None = None,
) -> None:
    """Wire the anime domain onto the host and register watch_anime. Components
    are injectable for tests; the defaults build the real adapters. Building the
    adapters does NO network / qbt / player I/O -- only construction plus the
    local library/pending JSON load (Phase 4). Reconcile with qbt is the UI
    controller's startup job -- install never touches the network."""
    cfg = host.config.anime
    secrets = getattr(host, "secrets", None)
    sources = sources if sources is not None else _build_sources(cfg, secrets)
    # download_dir may be repo-relative (the default static/generated_anime) --
    # anchor it at the repo root like the other data paths, NOT the cwd, and
    # .resolve() it: this is the containment root the registration check compares
    # RESOLVED file paths against (assemblies:resolve_media_file), so it must be
    # normalized too (else `static/../static/...` or a symlink dir mis-flags an
    # in-dir file as "outside download_dir").
    download_root = resolve_data_path(cfg.download_dir).resolve()
    torrent = torrent if torrent is not None else _build_torrent(cfg, secrets, str(download_root))
    player = player if player is not None else _build_player(cfg, str(download_root))

    lib_path = resolve_data_path(
        getattr(cfg, "library_file", "data/anime/library.json"))
    pending_path = lib_path.with_name("pending.json")
    library = library if library is not None else load_library(lib_path)
    pending: list[dict] = load_pending(pending_path)
    lock = threading.Lock()          # ChatWorker thread + GUI thread both write

    # held for the Phase 4 download worker / controller wiring in qt_overlay
    host.anime_torrent = torrent
    host.anime_player = player
    host.anime_library = library
    host.anime_play_file = player.play_file      # adapter stays the ONLY check point
    host.anime_download_dir = str(download_root)
    host.anime_cookies_file = str(resolve_data_path(
        getattr(cfg, "cookies_file", "data/cookies.txt")))

    def _persist_library_locked() -> None:
        _atomic_write_json(lib_path, library.to_json())

    def _persist_pending_locked() -> None:
        _atomic_write_json(pending_path, [dict(p) for p in pending])

    def _note_pending(ev: AnimeRequestEvent) -> None:
        parsed = parse_episode_key(ev.episode_key)
        if parsed is None:
            # never guess silently -- placeholder is explicit and logged, and the
            # episode_key still dedups correctly (it is the identity, not s/e).
            _LOG.warning("anime pending: unparseable episode_key %r, using "
                         "placeholder season/episode", ev.episode_key)
            season, episode = 1, 0
            title = ev.title or ev.display_title or ev.episode_key
        else:
            key_title, season, episode = parsed
            title = ev.title or ev.display_title or key_title
        with lock:
            pending[:] = [p for p in pending
                          if p.get("request_id") != ev.request_id]
            pending.append({
                "request_id": ev.request_id, "episode_key": ev.episode_key,
                "title": title, "season": season, "episode": episode,
                "source": ev.source, "locator": ev.locator,
                "task_id": None, "created_at": ev.created_at,
            })
            _persist_pending_locked()

    def _register_download(request_id: str, episode_key: str,
                           save_path: str) -> LibraryEntry:
        """Register a finished download. Validates the file (containment +
        media extension, review) BEFORE it can ever enter the library; raises
        ValueError on rejection -- the caller reports, nothing is registered."""
        final = resolve_media_file(Path(save_path), download_root)
        with lock:
            rec = next((p for p in pending if p.get("request_id") == request_id
                        or p.get("episode_key") == episode_key), None)
            if rec is not None:
                title = str(rec.get("title") or episode_key)
                season = int(rec.get("season") or 1)
                episode = int(rec.get("episode") or 0)
                source = str(rec.get("source") or "")
            else:                        # no pending record -> key parse fallback
                parsed = parse_episode_key(episode_key)
                title, season, episode = (
                    parsed if parsed is not None else (episode_key, 1, 0))
                source = ""
            existing = library.find(episode_key)
            entry = LibraryEntry(
                episode_key=episode_key, title=title, season=season,
                episode=episode, file_path=str(final),
                size_bytes=final.stat().st_size, source=source,
                played=bool(existing is not None and existing.played))
            library.add(entry)
            _persist_library_locked()
            pending[:] = [p for p in pending
                          if p.get("request_id") != request_id]
            _persist_pending_locked()
        return entry

    def _mark_played(episode_key: str) -> None:
        with lock:
            library.mark_played(episode_key)
            _persist_library_locked()

    def _note_task_id(request_id: str, task_id: str) -> None:
        with lock:
            for p in pending:
                if p.get("request_id") == request_id:
                    p["task_id"] = task_id
            _persist_pending_locked()

    def _list_pending() -> list[dict]:
        with lock:
            return [dict(p) for p in pending]

    def _drop_pending(request_id: str) -> None:
        with lock:
            pending[:] = [p for p in pending
                          if p.get("request_id") != request_id]
            _persist_pending_locked()

    def _is_played(episode_key: str) -> bool:
        entry = library.find(episode_key)
        return bool(entry is not None and entry.played)

    # write-authority closures stay on the host (铁律 #9 / P1-6); the UI
    # controller only ever calls these -- it never touches library/files itself.
    host.anime_register_download = _register_download
    host.anime_mark_played = _mark_played
    host.anime_note_task_id = _note_task_id
    host.anime_list_pending = _list_pending
    host.anime_drop_pending = _drop_pending
    host.anime_is_played = _is_played

    closure = build_request_anime(
        host, sources=sources, library=library, play_file=player.play_file,
        # F8 live seam; getattr-tolerant like _available (fake hosts in tests)
        in_flight=lambda: getattr(host, "_anime_in_flight", lambda: None)(),
        mark_played=_mark_played, note_pending=_note_pending)
    tool = WatchAnimeTool(closure)
    host.registry.register_tool(
        tool.schema(), tool.run,
        available=lambda: _available(host),
        intent_gated=False,          # state supply -- no router wordlist (review)
        effect="act",
    )
    cancel_tool = CancelAnimeDownloadTool(build_request_anime_cancel(host))
    host.registry.register_tool(
        cancel_tool.schema(), cancel_tool.run,
        available=lambda: _cancel_available(host, cancel_tool),
        intent_gated=False,
        chainable=False,
        effect="act",
    )
