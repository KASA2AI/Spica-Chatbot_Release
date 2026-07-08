"""mss screen-rectangle capture (Phase 6), Qt-free.

Grabs an absolute physical-pixel rect via mss into a PIL image. The actual mss
grab is a single injectable seam (``grabber``) so tests never touch the real
screen. Whether the grabbed pixels are REAL game content vs a black/blocked frame
(Wayland/occlusion) is environment-dependent and cannot be known here -- the
calibrator flags it via ``ocr_region.looks_blank`` (Phase 6 #3).

Qt-free (CLAUDE.md #1): mss + PIL, never Qt grabWindow.
"""

from __future__ import annotations

from typing import Any, Callable

from spica.ports.screen_capture import CaptureImage


class MssScreenCapture:
    name = "mss"

    def __init__(self, *, grabber: Callable[[dict[str, int]], Any] | None = None) -> None:
        # grabber(monitor_dict) -> PIL.Image. Default uses mss; injectable for tests.
        self._grab = grabber or self._default_grab

    def capture_rect(self, left: int, top: int, width: int, height: int) -> CaptureImage:
        monitor = {"left": int(left), "top": int(top), "width": max(1, int(width)), "height": max(1, int(height))}
        image = self._grab(monitor)
        return CaptureImage(image=image, width=image.width, height=image.height)

    @staticmethod
    def _default_grab(monitor: dict[str, int]) -> Any:
        import mss  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]

        with mss.mss() as sct:
            raw = sct.grab(monitor)
            return Image.frombytes("RGB", raw.size, raw.rgb)
