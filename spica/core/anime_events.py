"""Anime-watch runtime events crossing the Host -> UI boundary (Phase 3).

Same dataclass channel as the song/companion events (CLAUDE.md #2: cross-boundary
= RuntimeEvent; the UI bridge hops threads via a Qt signal). ``AnimeRequestEvent``
is emitted by the host's watch_anime closure when a NEW episode resolves -- the UI
(Phase 4) dispatches it to an AnimeController which starts the download worker.
``AnimeReadyEvent`` is emitted by that worker when the download finishes (defined
now, USED in Phase 4 -- the production flow never emits it yet).

Fields are all primitives and carry NO secrets/cookie (the locator is a public
magnet / bvid:part). Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from spica.core.events import RuntimeEvent, register_event


@dataclass(frozen=True)
class AnimeRequestEvent(RuntimeEvent):
    kind: ClassVar[str] = "anime_request"
    request_id: str
    query: str
    title: str
    episode_key: str
    source: str
    locator: str
    display_title: str = ""
    # the ANIME NAME only (RequestSpec.title_query, e.g. "无职转生") -- used to
    # group the cache <download_dir>/<anime>/. Distinct from title/display_title,
    # which are the full source release name (fansub + episode + quality / [Pxx]).
    series_title: str = ""
    size_bytes: int | None = None
    created_at: str = ""

    def _data(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "query": self.query,
            "title": self.title,
            "episode_key": self.episode_key,
            "source": self.source,
            "locator": self.locator,
            "display_title": self.display_title,
            "series_title": self.series_title,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class AnimeReadyEvent(RuntimeEvent):
    """Phase 4: the download worker reports a finished (or failed) episode."""

    kind: ClassVar[str] = "anime_ready"
    request_id: str
    episode_key: str
    save_path: str | None = None
    elapsed_seconds: float | None = None
    error: str | None = None

    def _data(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "episode_key": self.episode_key,
            "save_path": self.save_path,
            "elapsed_seconds": self.elapsed_seconds,
            "error": self.error,
        }


def _opt_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _opt_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


register_event(
    "anime_request",
    lambda d: AnimeRequestEvent(
        request_id=str(d.get("request_id") or ""),
        query=str(d.get("query") or ""),
        title=str(d.get("title") or ""),
        episode_key=str(d.get("episode_key") or ""),
        source=str(d.get("source") or ""),
        locator=str(d.get("locator") or ""),
        display_title=str(d.get("display_title") or ""),
        series_title=str(d.get("series_title") or ""),
        size_bytes=_opt_int(d.get("size_bytes")),
        created_at=str(d.get("created_at") or ""),
    ),
)

register_event(
    "anime_ready",
    lambda d: AnimeReadyEvent(
        request_id=str(d.get("request_id") or ""),
        episode_key=str(d.get("episode_key") or ""),
        save_path=(str(d["save_path"]) if d.get("save_path") is not None else None),
        elapsed_seconds=_opt_float(d.get("elapsed_seconds")),
        error=(str(d["error"]) if d.get("error") is not None else None),
    ),
)
