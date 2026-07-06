"""Media player capability port (Phase 1) -- whitelisted action surface (#9).

The ONLY action is ``play_file(path)``. The adapter is the SINGLE enforcement
point (P0-4): it must, before handing the path to the OS opener, verify ALL of:
  - ``Path(path).resolve().is_relative_to(download_dir.resolve())``  (real path
    containment, NOT a string ``startswith`` -- ``SpicaAnimeEvil`` must fail);
  - the path exists and ``is_file()`` (a regular file);
  - the suffix is in a MEDIA extension whitelist (.mkv/.mp4/.ts/...). A torrent's
    internal filenames are attacker-controlled, so a ``.desktop``/``.sh``/``.html``
    would be an xdg-open code-execution surface -- the path whitelist is not
    enough on its own.

Qt-free (CLAUDE.md #1). Auto-play must also go THROUGH this port (never let the
UI controller open files directly and bypass validation, P0-4c).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# The whitelist the adapter enforces (kept here so tests pin the same set).
MEDIA_EXTENSIONS = frozenset(
    {".mkv", ".mp4", ".ts", ".avi", ".mov", ".webm", ".flv", ".m4v", ".wmv"}
)


class MediaPlayerError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@runtime_checkable
class MediaPlayerPort(Protocol):
    def play_file(self, path: str) -> None:
        """Open the file in the system/default player after whitelist checks.
        Raises MediaPlayerError('UNSAFE_PATH') if the path escapes download_dir,
        is not a regular file, or is not a whitelisted media extension."""
        ...
