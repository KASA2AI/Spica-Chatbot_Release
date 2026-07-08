"""Pure OCR-region geometry (Phase 6). Qt-free, no I/O.

Regions are stored as RATIOS (resolution-independent) so cropping adapts when the
window is resized (§18.3): crop = ratios x current image size. Pixel coords +
window_size_at_calibration are stored for debug/validation only.

Also hosts ``looks_blank`` -- the suspect-blank heuristic that turns "mss returned
a black/empty frame" (e.g. Wayland / occlusion) into an observable signal instead
of a silent black preview.
"""

from __future__ import annotations

from typing import Any

from spica.ports.window_locator import WindowGeometry

# suspect-blank thresholds (0..255 luma).
_BLACK_MAX_LUMA = 10  # whole frame darker than this -> near all-black
_UNIFORM_RANGE = 6  # max-min luma this small -> near single flat colour


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def screen_rect_to_ratios(
    rect: tuple[int, int, int, int], geom: WindowGeometry
) -> tuple[float, float, float, float]:
    """A physical screen rect (x, y, w, h) -> ratios within the window geometry."""
    width = max(1, geom.width)
    height = max(1, geom.height)
    x, y, w, h = rect
    return (
        _clamp01((x - geom.x) / width),
        _clamp01((y - geom.y) / height),
        _clamp01(w / width),
        _clamp01(h / height),
    )


def ratios_to_pixel_rect(
    ratios: tuple[float, float, float, float], size: tuple[int, int]
) -> tuple[int, int, int, int]:
    rx, ry, rw, rh = ratios
    width, height = size
    return (round(rx * width), round(ry * height), round(rw * width), round(rh * height))


def crop_by_ratios(image: Any, ratios: tuple[float, float, float, float]) -> Any:
    """Crop a PIL image by ratios x its CURRENT size (so it adapts to resize)."""
    x, y, w, h = ratios_to_pixel_rect(ratios, (image.width, image.height))
    left = max(0, min(x, image.width))
    top = max(0, min(y, image.height))
    right = max(left + 1, min(x + max(1, w), image.width))
    bottom = max(top + 1, min(y + max(1, h), image.height))
    return image.crop((left, top, right, bottom))


def overlay_covers_region(
    overlay_rect: tuple[int, int, int, int], region_rect: tuple[int, int, int, int]
) -> bool:
    """True if the Spica overlay rect intersects the OCR region rect (§7.2). Both
    are physical-pixel (x, y, w, h) screen rects; the UI supplies the overlay rect."""
    ox, oy, ow, oh = overlay_rect
    rx, ry, rw, rh = region_rect
    if ow <= 0 or oh <= 0 or rw <= 0 or rh <= 0:
        return False
    return not (ox + ow <= rx or rx + rw <= ox or oy + oh <= ry or ry + rh <= oy)


def looks_blank(image: Any) -> bool:
    """True if the image is near all-black OR near a single flat colour -- the
    signal that a capture likely failed (Wayland/occlusion) rather than showing
    real game pixels."""
    luma = image.convert("L")
    minimum, maximum = luma.getextrema()
    if maximum <= _BLACK_MAX_LUMA:
        return True
    return (maximum - minimum) <= _UNIFORM_RANGE
