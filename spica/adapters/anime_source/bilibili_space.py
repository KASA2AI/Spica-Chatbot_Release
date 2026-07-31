"""Deprecated compatibility shim for the former Bilibili space adapter.

New code imports :class:`BilibiliSearchSource` from ``bilibili_search``.
``BilibiliSpaceSource`` keeps the old import and ``space_uids=`` constructor
keyword working while delegating to the global-search implementation.
"""

from __future__ import annotations

from typing import Any

from spica.adapters.anime_source.bilibili_search import BilibiliSearchSource


class BilibiliSpaceSource(BilibiliSearchSource):
    """Compatibility name; performs global search, never space-list crawling."""

    def __init__(
        self,
        space_uids: list[str],
        *,
        cookie: str | None = None,
        session: Any = None,
        timeout: float = 12,
        max_retries: int = 5,
        max_pages: int = 10,
        sleep: Any = None,
        clock: Any = None,
    ) -> None:
        super().__init__(
            space_uids,
            cookie=cookie,
            session=session,
            timeout=timeout,
            max_retries=max_retries,
            max_pages=max_pages,
            sleep=sleep,
            clock=clock,
        )


__all__ = ["BilibiliSearchSource", "BilibiliSpaceSource"]
