"""Windows native game launcher -- W1 STUB (WINDOWS_COMPAT_PLAN §5-W1 内容 4).

Windows v1 launch semantics (E2): ``manual_bind`` first-class (the game is
already open; nothing to launch -- platform-neutral, usable TODAY), hand-filled
``exe`` second (real ``Popen`` spawn lands in W2), ``desktop_entry`` PERMANENTLY
unsupported (.desktop scanning is Linux-only). W2's ``command`` lane passes the
string WHOLE to Popen (Windows CreateProcess parsing) -- never POSIX
``shlex.split``, which would eat the backslashes in ``C:\\game\\a.exe`` (P3-1).

IMPORT DISCIPLINE (§3.5): must import cleanly on Linux; no win32 API at module
level. Failures return ``LaunchResult(ok=False, error=...)``, never raise
(the binder turns them into a user-facing ``galgame_bind_failed``).

Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from spica.galgame.models import LaunchProfile
from spica.ports.game_launcher import DesktopEntry, LaunchResult


class WindowsNativeGameLauncher:
    name = "windows_native"

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
        if profile.launch_type in ("exe", "command"):
            return LaunchResult(
                ok=False,
                error=f"Windows {profile.launch_type} 启动尚未实现（W2 落地），当前请用 manual_bind。",
            )
        return LaunchResult(ok=False, error=f"unknown launch_type={profile.launch_type!r}")
