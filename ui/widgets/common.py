from __future__ import annotations

from pathlib import Path

from spica.config.overlay_owner import MAX_UI_SCALE, MIN_UI_SCALE

BASE_DIR = Path(__file__).resolve().parents[2]
DIALOG_FILTER_PATH = BASE_DIR / "spica_data" / "diffs" / "ui" / "_mw_filter01.png"


def scaled_px(value: float, scale: float) -> int:
    return max(1, round(value * scale))
