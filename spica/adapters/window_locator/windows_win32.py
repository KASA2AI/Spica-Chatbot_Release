"""Windows Win32 window locator -- W1 STUB (WINDOWS_COMPAT_PLAN §5-W1 内容 4).

The real Win32 implementation (ctypes user32: EnumWindows / GetWindowRect /
GetForegroundWindow / IsIconic -- A2) lands in W2. This stub gives the W1
platform factory a windows lane that degrades STRUCTURALLY -- ``available=False``
+ a machine ``reason_code``, per the port's never-raise / never-silent-empty
discipline -- instead of crashing assembly on a Windows machine before W2.

IMPORT DISCIPLINE (§3.5): this module MUST import cleanly on Linux -- no
module-level ``ctypes.windll`` / win32 API access, ever (the clean-import guard
test pins this). W2's real Win32 calls go INSIDE methods, lazily.

Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from spica.galgame.models import WindowMatchRule
from spica.ports.window_locator import (
    WindowEnumeration,
    WindowGeometry,
    WindowSafetyResult,
)

_PENDING_REASON = "Windows 窗口枚举尚未实现（W2 落地 Win32 探针），当前为 W1 占位。"


class WindowsWin32WindowLocator:
    name = "windows_win32"

    def enumerate_windows(self) -> WindowEnumeration:
        return WindowEnumeration(
            windows=[],
            available=False,
            reason_code="WIN32_LOCATOR_PENDING",
            reason=_PENDING_REASON,
        )

    def get_window_geometry(self, window_id: str) -> WindowGeometry | None:
        return None

    def check_safety(
        self, window_id: str, rule: WindowMatchRule, overlay_window_id: str | None = None
    ) -> WindowSafetyResult:
        # Conservatively unsafe until the W2 probes exist -- never risk mis-capture.
        return WindowSafetyResult(
            ok=False, reason_code="SAFETY_PROBE_FAILED", reason=_PENDING_REASON
        )

    def format_native_window_id(self, native: int) -> str:
        # HWND is compared/stored as a DECIMAL string on the windows lane
        # (W2 spec: window_id=str(hwnd)); the X11 lane formats hex. (A3)
        return str(int(native))
