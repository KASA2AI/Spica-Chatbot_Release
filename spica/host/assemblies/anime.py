"""Anime-watch domain assembly (Phase 3) -- WIRING only, no business flow.

``install(host)`` builds the config-driven sources / torrent client / player /
library, wires the host watch_anime closure, and registers the tool. The business
flow lives in ``spica.anime.watch_flow`` (Host stays thin, review); this module
only injects real ports into it and maps its ``WatchAnimeError`` onto the runtime
``ScreenToolError`` envelope (so failures are ToolErrors, never ack dicts).

INSTALL TIMING (review): call from ``AppHost.initialize()`` AFTER config / secrets
/ services exist -- adapters read ``config.anime`` and ``secrets`` at build time,
so registering in ``__init__`` (like the older tools) would read None.

``host`` stays duck-typed ``Any`` -- this module must not import AppHost
(app_host imports us; the reverse edge would be a cycle). Qt-free (CLAUDE.md #1).
Phase 3 does NOT touch UI, does NOT download, does NOT auto-play; the torrent
client is built and held on the host for the Phase 4 worker but never called here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from agent_tools.function_tools.screen.schema import ScreenToolError
from spica.adapters.anime_source.bilibili_space import BilibiliSpaceSource
from spica.adapters.anime_source.mikan import MikanRssSource
from spica.adapters.media_player.system_default import SystemDefaultPlayer
from spica.adapters.torrent.qbittorrent import QBittorrentClient
from spica.adapters.tools.watch_anime import WatchAnimeTool
from spica.anime.library import AnimeLibrary
from spica.anime.watch_flow import WatchAnimeError, run_watch_request
from spica.ports.anime_source import AnimeSourcePort


def _available(host: Any) -> bool:
    """Supply predicate (live-read, fault-tolerant): enabled AND a UI sink is
    attached. Any missing attr -> False (never crash the registry)."""
    cfg = getattr(getattr(host, "config", None), "anime", None)
    if cfg is None or not getattr(cfg, "enabled", False):
        return False
    return getattr(host, "_anime_sink", None) is not None


def _build_sources(cfg: Any, secrets: Any) -> list[AnimeSourcePort]:
    # bilibili main, mikan fallback (coordinator order). Constructors do NO I/O.
    cookie = getattr(secrets, "bilibili_cookie", None)
    timeout = float(getattr(cfg, "source_timeout_seconds", 15) or 15)
    bilibili = BilibiliSpaceSource(list(cfg.bilibili_spaces), cookie=cookie,
                                   timeout=timeout)
    mikan = MikanRssSource(list(cfg.mikan_base_urls), timeout=timeout)
    return [bilibili, mikan]


def _build_torrent(cfg: Any, secrets: Any) -> QBittorrentClient:
    return QBittorrentClient(
        cfg.qbittorrent_url, cfg.download_dir,
        username=getattr(cfg, "qbittorrent_username", None),
        password=getattr(secrets, "qbittorrent_password", None),
    )


def _build_player(cfg: Any) -> SystemDefaultPlayer:
    return SystemDefaultPlayer(cfg.download_dir,
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
) -> Callable[[str, "int | str | None"], dict[str, Any]]:
    """The host watch_anime closure: injects live config + real ports into the
    pure flow and maps WatchAnimeError -> ScreenToolError (review #4). ``config``
    and the event sink are read LIVE from the host each call. ``in_flight`` is
    the busy seam (F8): Phase 3 has no downloads, so the default reports None;
    Phase 4 swaps in the real worker state without touching the flow."""
    new_id = new_id or (lambda: uuid.uuid4().hex)
    now = now or (lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    in_flight = in_flight or (lambda: None)

    def _request_anime(query: str, episode: "int | str | None") -> dict[str, Any]:
        try:
            return run_watch_request(
                query=query, episode=episode,
                config=host.config.anime,                       # live
                sources=sources, library=library, play_file=play_file,
                emit=lambda ev: host._anime_sink(ev),           # gated by is_ready
                is_ready=lambda: getattr(host, "_anime_sink", None) is not None,
                new_id=new_id, now=now, in_flight=in_flight,
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
    are injectable for tests; the defaults build the real adapters. Phase 3 uses
    an EMPTY library (persistence deferred to Phase 4, review #3). Building the
    adapters does NO network / qbt / player I/O -- only construction."""
    cfg = host.config.anime
    secrets = getattr(host, "secrets", None)
    sources = sources if sources is not None else _build_sources(cfg, secrets)
    torrent = torrent if torrent is not None else _build_torrent(cfg, secrets)
    player = player if player is not None else _build_player(cfg)
    library = library if library is not None else AnimeLibrary()

    # held for the Phase 4 download worker; NOT called by the Phase 3 closure
    host.anime_torrent = torrent
    host.anime_player = player
    host.anime_library = library

    closure = build_request_anime(
        host, sources=sources, library=library, play_file=player.play_file)
    tool = WatchAnimeTool(closure)
    host.registry.register_tool(
        tool.schema(), tool.run,
        available=lambda: _available(host),
        intent_gated=False,          # state supply -- no router wordlist (review)
        effect="act",
    )
