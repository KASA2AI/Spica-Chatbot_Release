"""Linux X11 window locator (Phase 5), backed by ``wmctrl -lpx``.

The real syscall is a single injectable seam (``runner``) so tests never shell
out. Degradation is structured, NOT raised and NOT silent (Phase 5 requirement):

- wmctrl binary missing -> ``WMCTRL_MISSING`` (readable "install wmctrl").
- wmctrl ran but failed AND a Wayland socket is present -> ``WAYLAND_UNSUPPORTED``
  (readable "v1 needs X11"). Wayland is detected via ``/run/user/$uid/wayland-*``
  (``os.getuid`` -- NOT ``os.getenv``, so test_no_getenv stays green).
- otherwise -> ``ENUMERATION_FAILED`` (carries the stderr).

X11/XWayland only; full Wayland window capture is out of v1 (Phase 0 ⑥).
Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Callable

from spica.galgame.models import WindowMatchRule
from spica.galgame.window_match import title_matches_rule
from spica.ports.window_locator import (
    WindowCandidate,
    WindowEnumeration,
    WindowGeometry,
    WindowSafetyResult,
)

logger = logging.getLogger(__name__)

_WMCTRL_MISSING_REASON = "未检测到 wmctrl(X11 窗口枚举依赖)。请安装：sudo apt install wmctrl。"
_WAYLAND_REASON = "当前疑似 Wayland 会话，v1 暂不支持 Wayland 窗口枚举；请在 X11(Xorg)会话下运行游戏与 Spica。"


class _WmctrlError(RuntimeError):
    pass


def _same_window(a: str | None, b: str | None) -> bool:
    """Compare X11 window ids tolerant of 0-padding / case (0x05000003 == 0x5000003)."""
    if not a or not b:
        return False
    try:
        return int(a, 16) == int(b, 16)
    except ValueError:
        return a == b


class LinuxX11WindowLocator:
    name = "linux_x11"

    def __init__(
        self,
        *,
        runner: Callable[[list[str]], str] | None = None,
        wmctrl_path: str = "wmctrl",
        wayland_probe: Callable[[], bool] | None = None,
        active_window_probe: Callable[[], str | None] | None = None,
        window_title_probe: Callable[[str], str | None] | None = None,
        minimized_probe: Callable[[str], bool] | None = None,
    ) -> None:
        self._run = runner or self._default_run
        self._wmctrl = wmctrl_path
        self._wayland_probe = wayland_probe or self._is_wayland
        # Injectable so safety tests don't shell out to xprop.
        self._active_window_probe = active_window_probe or self._default_active_window
        self._window_title_probe = window_title_probe or self._default_window_title
        self._minimized_probe = minimized_probe or self._default_minimized

    def enumerate_windows(self) -> WindowEnumeration:
        try:
            output = self._run([self._wmctrl, "-lpx"])
        except FileNotFoundError:
            return WindowEnumeration(windows=[], available=False, reason_code="WMCTRL_MISSING", reason=_WMCTRL_MISSING_REASON)
        except _WmctrlError as exc:
            if self._wayland_probe():
                return WindowEnumeration(windows=[], available=False, reason_code="WAYLAND_UNSUPPORTED", reason=_WAYLAND_REASON)
            return WindowEnumeration(
                windows=[], available=False, reason_code="ENUMERATION_FAILED",
                reason=f"窗口枚举失败：{exc}",
            )
        return WindowEnumeration(windows=self._parse(output), available=True)

    def _default_run(self, command: list[str]) -> str:
        proc = subprocess.run(command, capture_output=True, text=True)
        if proc.returncode != 0:
            raise _WmctrlError((proc.stderr or "").strip() or f"exit {proc.returncode}")
        return proc.stdout

    def get_window_geometry(self, window_id: str) -> WindowGeometry | None:
        # `wmctrl -lG`: <win_id> <desktop> <x> <y> <w> <h> <host> <title...>
        try:
            output = self._run([self._wmctrl, "-lG"])
        except (FileNotFoundError, _WmctrlError):
            return None
        for line in output.splitlines():
            parts = line.split(None, 7)
            if len(parts) < 6 or parts[0] != window_id:
                continue
            try:
                return WindowGeometry(x=int(parts[2]), y=int(parts[3]), width=int(parts[4]), height=int(parts[5]))
            except ValueError:
                return None
        return None

    def check_safety(
        self, window_id: str, rule: WindowMatchRule, overlay_window_id: str | None = None
    ) -> WindowSafetyResult:
        if self.get_window_geometry(window_id) is None:
            return WindowSafetyResult(ok=False, reason_code="WINDOW_GONE", reason="目标游戏窗口不存在或已关闭。")
        if self._minimized_probe(window_id):
            return WindowSafetyResult(ok=False, reason_code="WINDOW_MINIMIZED", reason="游戏窗口已最小化/隐藏。")
        active = self._active_window_probe()
        if active is None:
            # Cannot determine focus -> conservatively unsafe (never risk mis-capture, §7.1).
            return WindowSafetyResult(
                ok=False, reason_code="SAFETY_PROBE_FAILED", reason="无法确定当前焦点窗口（xprop 不可用？）。"
            )
        # §7.4: focus on the Spica overlay is exempt. The overlay is our OWN window, so
        # its id is stable/known -> id match is reliable here (unlike the game window).
        if _same_window(active, overlay_window_id):
            return WindowSafetyResult(ok=True)
        # §17.3: judge "is the game focused" by the ACTIVE window's TITLE vs the game's
        # keywords -- NOT by window-id equality. wine/Bottles spawns an outer window and
        # an inner render window with different (and shifting) ids; either of them being
        # focused means we are "on the game" as long as the title hits a keyword.
        active_title = self._window_title_probe(active)
        if active_title and title_matches_rule(active_title, rule):
            return WindowSafetyResult(ok=True)
        return WindowSafetyResult(
            ok=False, reason_code="WINDOW_NOT_FOCUSED",
            reason="焦点不在游戏窗口（活动窗口标题未命中 keyword），暂停 OCR。",
        )

    def format_native_window_id(self, native: int) -> str:
        # W1 / A3: X11 window ids are compared as hex strings (see _same_window);
        # hex(native) is byte-identical to the `hex(int(winId()))` string the UI
        # used to build inline, so the focus-exemption comparison is unchanged.
        return hex(int(native))

    def _default_active_window(self) -> str | None:
        try:
            output = self._run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
        except (FileNotFoundError, _WmctrlError):
            return None
        # `_NET_ACTIVE_WINDOW(WINDOW): window id # 0x5000003`
        match = re.search(r"0x[0-9a-fA-F]+", output)
        return match.group(0) if match else None

    def _default_window_title(self, window_id: str) -> str | None:
        for prop in ("_NET_WM_NAME", "WM_NAME"):
            try:
                output = self._run(["xprop", "-id", window_id, prop])
            except (FileNotFoundError, _WmctrlError):
                return None
            # `_NET_WM_NAME(UTF8_STRING) = "anemoi Day 1"`
            match = re.search(r'=\s*"(.*)"\s*$', output.strip())
            if match:
                return match.group(1)
        return None

    def _default_minimized(self, window_id: str) -> bool:
        try:
            output = self._run(["xprop", "-id", window_id, "_NET_WM_STATE"])
        except (FileNotFoundError, _WmctrlError):
            return False  # can't tell -> the focus check is the main guard
        return "_NET_WM_STATE_HIDDEN" in output

    @staticmethod
    def _is_wayland() -> bool:
        getuid = getattr(os, "getuid", None)
        if getuid is None:
            return False
        try:
            return any(Path(f"/run/user/{getuid()}").glob("wayland-*"))
        except OSError:
            return False

    @staticmethod
    def _parse(output: str) -> list[WindowCandidate]:
        # `wmctrl -lpx`: <win_id> <desktop> <pid> <WM_CLASS> <host> <title...>
        windows: list[WindowCandidate] = []
        for line in output.splitlines():
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            win_id, _desktop, pid, wm_class, _host, title = parts
            windows.append(
                WindowCandidate(
                    window_id=win_id,
                    title=title.strip(),
                    process_name=None,  # wmctrl exposes WM_CLASS, not the process name
                    app_id=wm_class,
                    pid=int(pid) if pid.isdigit() else None,
                    visible=True,
                )
            )
        return windows
