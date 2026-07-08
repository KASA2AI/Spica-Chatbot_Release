"""Windows native game launcher (W2, WINDOWS_COMPAT_PLAN §5-W2 内容 2).

Windows launch semantics (E2): ``manual_bind`` first-class (the game is already
open; nothing to launch), ``exe`` spawns via fire-and-forget ``Popen`` (same
form as ``linux_desktop.py`` -- the pid rides the LaunchResult but nothing
consumes it), ``desktop_entry`` PERMANENTLY unsupported (.desktop scanning is
Linux-only; start-menu/.lnk/registry discovery is an explicit non-goal, E2).

P3-1 (the one Windows-specific trap): ``command`` passes the string WHOLE to
``Popen`` -- Windows CreateProcess parses the command line natively. NEVER
POSIX ``shlex.split`` here (it eats the backslashes in ``C:\\game\\a.exe``),
and no ``shell=True`` (nothing here needs cmd.exe semantics).

The process spawn is a single injectable seam (``runner``) so Linux unit tests
never spawn a real process (same seam as ``linux_desktop.py``). Failures return
``LaunchResult(ok=False, error=...)``, never raise (the binder turns them into
a user-facing ``galgame_bind_failed``).

IMPORT DISCIPLINE (§3.5): imports cleanly on Linux -- subprocess only, no win32
API. Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any, Callable

from spica.galgame.models import LaunchProfile
from spica.ports.game_launcher import DesktopEntry, LaunchResult

logger = logging.getLogger(__name__)


class WindowsNativeGameLauncher:
    name = "windows_native"

    def __init__(self, *, runner: Callable[..., Any] | None = None) -> None:
        # Injectable so tests don't spawn real processes; default fire-and-forget Popen.
        self._run = runner or subprocess.Popen

    def scan_desktop_entries(self) -> list[DesktopEntry]:
        # .desktop entries do not exist on Windows; discovery mechanisms
        # (start menu / .lnk / registry) are an explicit non-goal (E2).
        return []

    def launch(self, profile: LaunchProfile) -> LaunchResult:
        if profile.launch_type == "manual_bind":
            return LaunchResult(ok=True)  # the game is already open; nothing to launch
        if profile.launch_type == "desktop_entry":
            return LaunchResult(
                ok=False, error="desktop_entry 启动在 Windows 不支持（请改用 manual_bind 或 exe）。"
            )
        command = self._command_for(profile)
        if command is None:
            return LaunchResult(ok=False, error=f"no launch command for launch_type={profile.launch_type!r}")
        try:
            proc = self._run(command, cwd=profile.working_dir or None)
            return LaunchResult(ok=True, pid=getattr(proc, "pid", None))
        except FileNotFoundError as exc:
            return LaunchResult(ok=False, error=f"executable not found: {exc}")
        except Exception as exc:  # noqa: BLE001 -- surfaced as ok=False, never raised to the binder
            logger.warning("game launch failed (%s): %s", profile.launch_type, exc, exc_info=True)
            return LaunchResult(ok=False, error=f"launch failed: {type(exc).__name__}: {exc}")

    @staticmethod
    def _command_for(profile: LaunchProfile) -> list[str] | str | None:
        if profile.launch_type == "exe" and profile.launch_target:
            # List-of-one: Popen's list2cmdline quotes it, so spaces in
            # C:\Games\A B\game.exe survive without the caller quoting anything.
            return [profile.launch_target]
        if profile.launch_type == "command" and profile.command:
            # WHOLE string -> CreateProcess parses the command line natively (P3-1).
            # POSIX shlex.split would eat the C:\ backslashes -- never do it here.
            return profile.command
        return None
