"""Torrent client capability port (Phase 1) -- whitelisted action surface (#9).

The ONLY four actions, deliberately narrow (P0-3 / P2-20):
- ``add_magnet(magnet, subfolder=None)`` -- magnet URIs ONLY; the adapter MUST
                              reject anything that is not ``magnet:?xt=urn:btih:...``
                              (an http(s) torrent URL would make qBittorrent fetch
                              an arbitrary URL). ``save_dir`` is still NOT a caller
                              argument -- it is pinned at construction. ``subfolder``
                              is an OPTIONAL per-anime grouping component (from
                              ``anime_dirname``): the adapter joins it UNDER the
                              pinned save_dir and re-validates real-path containment
                              (rejects any traversal), so writes still never escape
                              download_dir -- no arbitrary caller path reaches qbt.
- ``add_torrent_bytes(payload, expected_infohash, subfolder=None)`` -- verified
                              in-memory bencode ONLY; no URL or filesystem path.
                              The adapter must independently validate the exact
                              infohash and direct tracker boundary before upload.
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
    def add_magnet(self, magnet: str, *, subfolder: str | None = None) -> str:
        """Start a download from a magnet URI; return an opaque task_id. Raises
        TorrentClientError('BAD_MAGNET') if ``magnet`` is not a magnet URI, or
        ('UNSAFE_PATH') if ``subfolder`` (joined under the pinned save_dir) would
        escape it. ``subfolder`` groups the download under ``save_dir/<subfolder>``."""
        ...

    def add_torrent_bytes(
        self,
        payload: bytes,
        *,
        expected_infohash: str,
        subfolder: str | None = None,
    ) -> str:
        """Start a verified in-memory ``.torrent`` payload.

        The implementation must reject malformed bytes or an infohash mismatch;
        callers cannot supply a URL or filesystem path.  Returns the v1 infohash
        as the opaque task id.
        """
        ...

    def status(self, task_id: str) -> DownloadStatus:
        """Report the task's live state (category-scoped)."""
        ...

    def cancel(self, task_id: str) -> None:
        """Remove the task and its data (category-scoped)."""
        ...
