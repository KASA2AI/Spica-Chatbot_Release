"""Torrent client capability port (Phase 1) -- whitelisted action surface (#9).

The ONLY three actions, deliberately narrow (P0-3 / P2-20):
- ``add_magnet(magnet)``   -- magnet URIs ONLY; the adapter MUST reject anything
                              that is not ``magnet:?xt=urn:btih:...`` (an http(s)
                              torrent URL would make qBittorrent fetch an
                              arbitrary URL). ``save_dir`` is NOT a parameter --
                              it is pinned at adapter construction to the resolved
                              download_dir, so a caller can never redirect writes.
- ``status(task_id)``      -- category-scoped read.
- ``cancel(task_id)``      -- category-scoped delete.

All operations are constrained to the adapter's category (``spica-anime``); the
adapter must never touch torrents the user added by hand (P2-20). Qt-free (#1).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from spica.anime.models import DownloadStatus


class TorrentClientError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@runtime_checkable
class TorrentClientPort(Protocol):
    def add_magnet(self, magnet: str) -> str:
        """Start a download from a magnet URI; return an opaque task_id. Raises
        TorrentClientError('BAD_MAGNET') if ``magnet`` is not a magnet URI."""
        ...

    def status(self, task_id: str) -> DownloadStatus:
        """Report the task's live state (category-scoped)."""
        ...

    def cancel(self, task_id: str) -> None:
        """Remove the task and its data (category-scoped)."""
        ...
