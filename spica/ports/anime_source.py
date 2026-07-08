"""Anime source capability port (Phase 1).

One implementation per source (bilibili space / mikan RSS). ``search`` returns
per-episode candidates already parsed for matching; the coordinator runs
``spica.anime.resolver.resolve`` over the union with main->fallback ordering.

Qt-free (CLAUDE.md #1). Network I/O lives in the ADAPTER, never here. Failures
are raised as ``AnimeSourceError`` and folded into a ToolError envelope upstream
(never crash the turn). Adapters must honour a per-call timeout budget (P1-8).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from spica.anime.models import AnimeCandidate, AnimeResource


class AnimeSourceError(Exception):
    """A source failed (network / risk-control / parse). Carries a stable code
    so the host can map to ANIME_SOURCE_ERROR vs ANIME_NOT_FOUND (P1-10)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@runtime_checkable
class AnimeSourcePort(Protocol):
    name: str  # "bilibili" | "mikan"

    def search(self, title_query: str, *,
               deadline: float | None = None) -> list[AnimeCandidate]:
        """Return per-episode candidates for the anime name (matching data only).
        A bilibili collection is expanded to per-part single-episode candidates
        here (finding #1); mikan multi-episode torrents are filtered (D11). Raises
        AnimeSourceError on failure; an empty list means "reachable but nothing
        found" (-> NOT_FOUND upstream).

        ``deadline`` (F6/P1-8) is the remaining seconds this call may spend --
        the adapter checks it before EVERY HTTP request and raises
        AnimeSourceError("TIMEOUT") once exhausted; each request's timeout is
        min(the adapter's own, the remaining budget). None = no budget."""
        ...

    def materialize(self, candidate: AnimeCandidate) -> AnimeResource:
        """Turn the resolver-chosen candidate into a concrete downloadable
        resource (magnet / bvid:part), doing any final per-source fetch. Split
        from ``search`` so search stays cheap and the expensive/last-mile step
        runs only for the ONE chosen candidate (finding #3). Raises
        AnimeSourceError on failure.

        MUST only resolve and return an AnimeResource -- it must NOT start a
        download or cause any irreversible side effect (that is the download
        worker's job, behind the host closure)."""
        ...
