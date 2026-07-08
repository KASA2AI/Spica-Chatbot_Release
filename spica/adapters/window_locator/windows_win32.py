"""Windows Win32 window locator (W2, WINDOWS_COMPAT_PLAN §5-W2 内容 1).

Real Win32 implementation over ctypes user32 (A2): ``EnumWindows`` +
``GetWindowTextW`` + ``IsWindowVisible`` (enumeration, ``window_id=str(hwnd)``
decimal), ``GetWindowRect`` (geometry, physical px), ``GetForegroundWindow``
(focus), ``IsIconic`` (minimized), ``GetWindowThreadProcessId`` (pid for the
candidate list). No pywin32.

``check_safety`` is isomorphic to ``linux_x11.py``: geometry gone ->
``WINDOW_GONE``, Iconic -> ``WINDOW_MINIMIZED``, then the FOREGROUND window must
either be the Spica overlay (id exemption, §7.4 -- decimal HWND via
``format_native_window_id``) or title-match the game's keywords (§17.3 -- never
window-id equality for the game itself). Anything undeterminable is
conservatively unsafe (``SAFETY_PROBE_FAILED``), never a risked capture (§7.1).

IMPORT DISCIPLINE (§3.5): this module MUST import cleanly on Linux. All ctypes /
user32 access lives inside ``_RealWin32Api`` which is built LAZILY on first use
(and is injectable, so Linux unit tests drive the full contract with a fake).
On a host without ``ctypes.windll`` the locator degrades structurally --
``available=False`` + ``WIN32_UNAVAILABLE`` -- never raises (port discipline).

DPI note (L3/L4): ``GetWindowRect`` returns physical pixels when the process is
per-monitor-DPI-aware -- true in production (Qt6 sets awareness); mixed-DPI
alignment is acceptance-gated on the real machine (§6.1), not assumed here.

Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import logging

from spica.galgame.models import WindowMatchRule
from spica.galgame.window_match import title_matches_rule
from spica.ports.window_locator import (
    WindowCandidate,
    WindowEnumeration,
    WindowGeometry,
    WindowSafetyResult,
)

logger = logging.getLogger(__name__)

_WIN32_UNAVAILABLE_REASON = "Win32 窗口 API 不可用（当前主机非 Windows，或 user32 加载失败）。"
_PROBE_FAILED_REASON = "无法完成 Win32 安全探测，保守暂停 OCR。"


def _same_hwnd(a: str | None, b: str | None) -> bool:
    """Compare decimal HWND strings numerically (tolerant of stray whitespace /
    a non-numeric id -- falls back to string equality, mirroring x11's helper)."""
    if not a or not b:
        return False
    try:
        return int(a) == int(b)
    except ValueError:
        return a == b


def _parse_hwnd(window_id: str) -> int | None:
    try:
        return int(window_id)
    except (TypeError, ValueError):
        return None


class _RealWin32Api:
    """Thin ctypes wrapper over user32 -- the ONLY place Win32 calls live.

    Constructing on a non-Windows host raises (no ``ctypes.windll``); the
    locator catches that and degrades. argtypes/restype are declared for every
    call so HWND stays a proper pointer-sized handle on 64-bit (no c_int
    truncation of pointer-like args).
    """

    def __init__(self) -> None:
        import ctypes
        import ctypes.wintypes as wintypes

        windll = getattr(ctypes, "windll", None)
        if windll is None:
            raise RuntimeError("ctypes.windll 不可用（非 Windows 主机）")
        self._ctypes = ctypes
        self._wintypes = wintypes
        user32 = windll.user32
        self._enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [self._enum_proc, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32 = user32

    def list_top_level_windows(self) -> list[int]:
        hwnds: list[int] = []

        def _collect(hwnd: object, _lparam: object) -> bool:
            hwnds.append(int(hwnd) if hwnd else 0)
            return True  # keep enumerating

        self._user32.EnumWindows(self._enum_proc(_collect), 0)
        return [h for h in hwnds if h]

    def window_title(self, hwnd: int) -> str:
        length = self._user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = self._ctypes.create_unicode_buffer(length + 1)
        self._user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value

    def is_window_visible(self, hwnd: int) -> bool:
        return bool(self._user32.IsWindowVisible(hwnd))

    def window_rect(self, hwnd: int) -> tuple[int, int, int, int] | None:
        rect = self._wintypes.RECT()
        if not self._user32.GetWindowRect(hwnd, self._ctypes.byref(rect)):
            return None  # window destroyed (or call failed) -> "gone"
        return (rect.left, rect.top, rect.right, rect.bottom)

    def foreground_window(self) -> int:
        hwnd = self._user32.GetForegroundWindow()
        return int(hwnd) if hwnd else 0

    def is_iconic(self, hwnd: int) -> bool:
        return bool(self._user32.IsIconic(hwnd))

    def window_pid(self, hwnd: int) -> int | None:
        pid = self._wintypes.DWORD(0)
        self._user32.GetWindowThreadProcessId(hwnd, self._ctypes.byref(pid))
        return int(pid.value) or None


class WindowsWin32WindowLocator:
    name = "windows_win32"

    def __init__(self, *, api: object | None = None) -> None:
        # Injectable Win32 call layer so Linux unit tests drive the full contract
        # with a fake (same seam idea as linux_x11's probes); None -> the real
        # ctypes api, built lazily on first use (§3.5 import discipline).
        self._api = api
        self._api_failed_reason: str | None = None

    # -- api seam ---------------------------------------------------------------
    def _api_or_none(self) -> object | None:
        if self._api is not None:
            return self._api
        if self._api_failed_reason is not None:
            return None  # load already failed once; don't retry every OCR tick
        try:
            self._api = _RealWin32Api()
            return self._api
        except Exception as exc:  # noqa: BLE001 -- degrade, never crash assembly/loop
            self._api_failed_reason = str(exc)
            logger.warning("Win32 API unavailable: %s", exc)
            return None

    # -- port -------------------------------------------------------------------
    def enumerate_windows(self) -> WindowEnumeration:
        api = self._api_or_none()
        if api is None:
            return WindowEnumeration(
                windows=[], available=False,
                reason_code="WIN32_UNAVAILABLE", reason=_WIN32_UNAVAILABLE_REASON,
            )
        try:
            candidates: list[WindowCandidate] = []
            for hwnd in api.list_top_level_windows():
                # Alt-tab-ish candidate filter: EnumWindows yields hordes of
                # invisible/untitled helper windows -- unpickable noise.
                if not api.is_window_visible(hwnd):
                    continue
                title = (api.window_title(hwnd) or "").strip()
                if not title:
                    continue
                candidates.append(
                    WindowCandidate(
                        window_id=str(hwnd),  # DECIMAL str(hwnd) -- W2 spec / A3
                        title=title,
                        process_name=None,  # process name resolution is out of the approved API set
                        app_id=None,
                        pid=api.window_pid(hwnd),
                        visible=True,
                    )
                )
            return WindowEnumeration(windows=candidates, available=True)
        except Exception as exc:  # noqa: BLE001 -- structured degradation, never raise
            logger.warning("Win32 window enumeration failed: %s", exc, exc_info=True)
            return WindowEnumeration(
                windows=[], available=False,
                reason_code="ENUMERATION_FAILED", reason=f"窗口枚举失败：{exc}",
            )

    def get_window_geometry(self, window_id: str) -> WindowGeometry | None:
        api = self._api_or_none()
        hwnd = _parse_hwnd(window_id)
        if api is None or hwnd is None:
            return None
        try:
            rect = api.window_rect(hwnd)
        except Exception:  # noqa: BLE001
            return None
        if rect is None:
            return None
        left, top, right, bottom = rect
        return WindowGeometry(x=left, y=top, width=right - left, height=bottom - top)

    def check_safety(
        self, window_id: str, rule: WindowMatchRule, overlay_window_id: str | None = None
    ) -> WindowSafetyResult:
        api = self._api_or_none()
        if api is None:
            # Distinct from WINDOW_GONE: we could not probe at all -> conservative.
            return WindowSafetyResult(
                ok=False, reason_code="SAFETY_PROBE_FAILED", reason=_WIN32_UNAVAILABLE_REASON
            )
        try:
            hwnd = _parse_hwnd(window_id)
            if hwnd is None or self.get_window_geometry(window_id) is None:
                return WindowSafetyResult(ok=False, reason_code="WINDOW_GONE", reason="目标游戏窗口不存在或已关闭。")
            if api.is_iconic(hwnd):
                return WindowSafetyResult(ok=False, reason_code="WINDOW_MINIMIZED", reason="游戏窗口已最小化。")
            foreground = api.foreground_window()
            if not foreground:
                # Cannot determine focus -> conservatively unsafe (never risk mis-capture, §7.1).
                return WindowSafetyResult(
                    ok=False, reason_code="SAFETY_PROBE_FAILED", reason="无法确定当前前台窗口。"
                )
            # §7.4: focus on the Spica overlay is exempt. The overlay is our OWN window,
            # so its id is stable/known -> id match is reliable here (decimal HWND form
            # from format_native_window_id, like-with-like).
            if _same_hwnd(str(foreground), overlay_window_id):
                return WindowSafetyResult(ok=True)
            # §17.3: judge "is the game focused" by the FOREGROUND window's TITLE vs the
            # game's keywords -- NOT by window-id equality (multi-window engines shift ids;
            # same discipline as the x11 lane).
            foreground_title = api.window_title(foreground)
            if foreground_title and title_matches_rule(foreground_title, rule):
                return WindowSafetyResult(ok=True)
            return WindowSafetyResult(
                ok=False, reason_code="WINDOW_NOT_FOCUSED",
                reason="焦点不在游戏窗口（前台窗口标题未命中 keyword），暂停 OCR。",
            )
        except Exception as exc:  # noqa: BLE001 -- conservative, never raise into the loop
            logger.warning("Win32 safety probe failed: %s", exc, exc_info=True)
            return WindowSafetyResult(
                ok=False, reason_code="SAFETY_PROBE_FAILED", reason=_PROBE_FAILED_REASON
            )

    def format_native_window_id(self, native: int) -> str:
        # HWND is compared/stored as a DECIMAL string on the windows lane
        # (W2 spec: window_id=str(hwnd)); the X11 lane formats hex. (A3)
        return str(int(native))
