"""Screen capture capability port (Phase 6).

Captures an absolute screen rectangle (physical pixels). The galgame path grabs
the whole bound-window rect once, then crops the OCR regions by ratio
(``spica/galgame/ocr_region.py``) -- so this port is geometry-agnostic: it just
grabs a rect. The window's on-screen geometry comes from
``WindowLocatorPort.get_window_geometry``; the calibrator composes the two.

This is NEW vs the existing full-screen capture (Phase 0 ⑤): per-window/region.
Qt-free (CLAUDE.md #1): no Qt grabWindow here -- the galgame capture path runs in
``spica/`` (the calibrator), so it uses mss, not Qt.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class CaptureImage:
    """A captured RGB image (PIL.Image) + its pixel size. PIL is Qt-free."""

    image: Any  # PIL.Image.Image -- typed Any so the port import stays light
    width: int
    height: int

    def to_png_bytes(self) -> bytes:
        buffer = BytesIO()
        self.image.save(buffer, format="PNG")
        return buffer.getvalue()


@runtime_checkable
class ScreenCapturePort(Protocol):
    def capture_rect(self, left: int, top: int, width: int, height: int) -> CaptureImage:
        """Grab the absolute screen rectangle (physical pixels) into a CaptureImage."""
        ...
