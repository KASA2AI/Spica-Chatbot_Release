"""Window locator capability port (Phase 5).

Enumerates on-screen windows so the binder can match the target game window by
``title_keywords`` (§17.3). The PORT only enumerates (platform-specific); the
match/scoring is pure domain (``spica/galgame/window_match.py``) so Bottles/Windows
share one scorer and it is unit-testable without a real window system.

``enumerate_windows`` returns a structured ``WindowEnumeration`` (NOT a bare list):
when the backend can't enumerate (wmctrl missing / Wayland / call failed) it returns
``windows=[]`` with ``available=False`` + a readable ``reason`` and a machine
``reason_code`` -- it NEVER raises a raw exception (would crash binding) and NEVER
silently returns empty (would read as "no window found"). The binder turns an
unavailable result into a user-facing ``galgame_bind_failed``.

Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from spica.galgame.models import WindowMatchRule


@dataclass(frozen=True)
class WindowCandidate:
    window_id: str
    title: str
    process_name: str | None = None
    app_id: str | None = None
    pid: int | None = None
    visible: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "window_id": self.window_id,
            "title": self.title,
            "process_name": self.process_name,
            "app_id": self.app_id,
            "pid": self.pid,
            "visible": self.visible,
        }


@dataclass(frozen=True)
class WindowEnumeration:
    windows: list[WindowCandidate] = field(default_factory=list)
    available: bool = True
    reason_code: str = ""  # "" | WMCTRL_MISSING | WAYLAND_UNSUPPORTED | ENUMERATION_FAILED
    reason: str = ""


@dataclass(frozen=True)
class WindowGeometry:
    """A window's on-screen rectangle in physical pixels (for region capture)."""

    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class WindowSafetyResult:
    """Whether it is safe to OCR the bound window right now (§7). ``reason_code``
    is machine-readable (e.g. WINDOW_NOT_FOCUSED) so a pause is distinguishable from
    a bug. NOT raised -- the loop reads it and pauses."""

    ok: bool
    reason_code: str = ""  # "" | WINDOW_GONE | WINDOW_MINIMIZED | WINDOW_NOT_FOCUSED | SAFETY_PROBE_FAILED
    reason: str = ""


@runtime_checkable
class WindowLocatorPort(Protocol):
    def enumerate_windows(self) -> WindowEnumeration:
        ...

    def get_window_geometry(self, window_id: str) -> WindowGeometry | None:
        """The window's on-screen rect (physical px), or None if unavailable.
        Used by OCR-region capture to grab the bound window's rect (Phase 6)."""
        ...

    def check_safety(
        self, window_id: str, rule: WindowMatchRule, overlay_window_id: str | None = None
    ) -> WindowSafetyResult:
        """Conservative pre-OCR safety check (Phase 7, §7): safe only if the window
        exists, is not minimized, AND the FOCUSED window's title matches the game's
        ``rule`` keywords (or the focus is on the Spica overlay, exempt per §7.4).

        Focus is judged by title-keyword match, NOT window-id equality: wine/Bottles
        spawns several windows (outer + inner render) with shifting ids, so id-equality
        wrongly reads "not focused" forever (§17.3). Anything else -> not safe, so OCR
        pauses rather than risk capturing another app. Overlay-covers-region is checked
        separately by the loop (it has the overlay rect)."""
        ...

    def format_native_window_id(self, native: int) -> str:
        """Format a NATIVE window handle (Qt ``winId()`` int) into this backend's
        ``window_id`` string form (W1 / A3): X11 -> hex ("0x5000003"), Win32 ->
        decimal HWND. The UI feeds its own overlay handle through this so the
        ``check_safety`` focus exemption compares like-with-like -- the native int
        exists ONLY as this method's argument and never crosses controller /
        ocr_loop / privacy_gate (F2); providers stay ``Callable[[], str | None]``."""
        ...
