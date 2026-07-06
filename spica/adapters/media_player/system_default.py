"""System-default media player adapter (Phase 2).

Implements ``MediaPlayerPort``. It is the SINGLE enforcement point (P0-4): before
handing a path to the OS opener it verifies real-path containment in
download_dir (``is_relative_to`` on resolved paths, NOT string startswith),
regular-file existence, and a MEDIA extension whitelist (rejecting
``.part``/``.desktop``/``.sh``/``.html`` -- torrent filenames are attacker
controlled). Opening uses ``xdg-open`` (Linux) / ``os.startfile`` (Windows) /
``player_command`` (from config) via ``subprocess.Popen`` WITHOUT ``shell=True``.

Launching is fire-and-forget with a short probe window (F3): ``play_file`` runs
inside the turn, so it must never wait for the player to exit -- it polls for
~``probe_window`` seconds only to catch an immediate launch failure (rc != 0),
then lets a still-alive player run. An exit code 0 inside the window (xdg-open's
normal fork-and-return) also counts as a successful launch.

Qt-free (CLAUDE.md #1). No os.getenv. ``platform`` / ``popen`` / ``startfile`` /
``sleep`` are injectable so tests never actually open a player.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from spica.ports.media_player import MEDIA_EXTENSIONS, MediaPlayerError

_PROBE_STEP = 0.05


class SystemDefaultPlayer:
    def __init__(self, download_dir: str, *, player_command: str | None = None,
                 platform: str | None = None,
                 popen: Callable[..., Any] | None = None,
                 startfile: Callable[[str], Any] | None = None,
                 probe_window: float = 0.3,
                 sleep: Callable[[float], None] | None = None) -> None:
        self._dir = Path(download_dir).expanduser().resolve()
        self._player_command = player_command or None
        self._platform = platform if platform is not None else sys.platform
        self._popen = popen if popen is not None else subprocess.Popen
        self._startfile = (startfile if startfile is not None
                           else getattr(os, "startfile", None))
        self._probe_window = probe_window
        self._sleep = sleep if sleep is not None else time.sleep

    def play_file(self, path: str) -> None:
        try:
            rp = Path(path).resolve()
        except OSError as e:
            raise MediaPlayerError("UNSAFE_PATH", str(e))
        if not rp.is_relative_to(self._dir):           # real containment, not prefix
            raise MediaPlayerError("UNSAFE_PATH", "outside download_dir")
        if not rp.exists() or not rp.is_file():
            raise MediaPlayerError("UNSAFE_PATH", "not a regular file")
        if rp.suffix.lower() not in MEDIA_EXTENSIONS:   # rejects .part/.desktop/.sh
            raise MediaPlayerError("UNSAFE_PATH", f"disallowed extension {rp.suffix!r}")
        self._open(str(rp))

    def _open(self, path: str) -> None:
        if self._player_command:
            self._run_argv([*shlex.split(self._player_command), path])
        elif self._platform.startswith("win"):
            if self._startfile is None:
                raise MediaPlayerError("NO_OPENER", "os.startfile unavailable")
            try:
                self._startfile(path)
            except OSError as e:                         # don't swallow (review #2)
                raise MediaPlayerError("OPEN_FAILED", str(e))
        else:
            self._run_argv(["xdg-open", path])           # never shell=True

    def _run_argv(self, argv: list[str]) -> None:
        try:
            proc = self._popen(argv)                     # fire-and-forget (F3)
        except OSError as e:                             # e.g. opener not installed
            raise MediaPlayerError("OPEN_FAILED", str(e))
        # Short probe: catch a launch that dies immediately (missing handler,
        # bad flag) without EVER waiting for the player to exit.
        waited = 0.0
        while True:
            rc = proc.poll()
            if rc is not None:
                if rc:                                   # died inside the window
                    raise MediaPlayerError("OPEN_FAILED", f"opener exited with {rc}")
                return                                   # quick clean exit (xdg-open)
            if waited >= self._probe_window:
                return                                   # still alive -> launched OK
            self._sleep(_PROBE_STEP)
            waited += _PROBE_STEP
