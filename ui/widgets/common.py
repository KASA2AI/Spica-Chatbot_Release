from __future__ import annotations

from spica.config.overlay_owner import MAX_UI_SCALE, MIN_UI_SCALE

DEFAULT_DIALOGUE_OPACITY = 0.88
MIN_DIALOGUE_OPACITY = 0.20
MAX_DIALOGUE_OPACITY = 1.0


def scaled_px(value: float, scale: float) -> int:
    return max(1, round(value * scale))
