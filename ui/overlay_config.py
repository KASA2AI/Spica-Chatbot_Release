from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ui.widgets.common import MAX_UI_SCALE, MIN_UI_SCALE

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).with_name("overlay_config.json")


@dataclass(frozen=True)
class OverlayConfig:
    default_character_scale: float = 1.0
    default_ui_scale: float = 1.0
    default_typewriter_speed: float = 1.0
    character_label_height_scale: float = 1.12
    overlay_initial_height_scale: float = 1.08
    character_max_height_ratio: float = 1.08
    # Her-voice (chat/TTS) playback volume, linear 0.0-1.0. Default 0.86 == the
    # historical AudioController hardcode, so an absent key is byte-identical to old
    # behaviour. UNLIKE the other (load-only / hand-edited) fields, this one is
    # written back by save_overlay_config_value when the user moves the slider.
    spica_voice_volume: float = 0.86


def load_overlay_config(path: Path | None = None) -> OverlayConfig:
    config_path = path or CONFIG_PATH
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return OverlayConfig()
    except Exception as exc:
        logger.warning("event=overlay_config_fallback path=%s reason=%s", config_path, exc)
        return OverlayConfig()

    if not isinstance(raw, dict):
        logger.warning("event=overlay_config_fallback path=%s reason=not_object", config_path)
        raw = {}

    defaults = OverlayConfig()
    return OverlayConfig(
        default_character_scale=_config_float(
            raw,
            "default_character_scale",
            defaults.default_character_scale,
            0.5,
            1.8,
        ),
        default_ui_scale=_config_float(
            raw,
            "default_ui_scale",
            defaults.default_ui_scale,
            MIN_UI_SCALE,
            MAX_UI_SCALE,
        ),
        default_typewriter_speed=_config_float(
            raw,
            "default_typewriter_speed",
            defaults.default_typewriter_speed,
            0.5,
            3.0,
        ),
        character_label_height_scale=_config_float(
            raw,
            "character_label_height_scale",
            defaults.character_label_height_scale,
            0.9,
            1.35,
        ),
        overlay_initial_height_scale=_config_float(
            raw,
            "overlay_initial_height_scale",
            defaults.overlay_initial_height_scale,
            1.0,
            1.20,
        ),
        character_max_height_ratio=_config_float(
            raw,
            "character_max_height_ratio",
            defaults.character_max_height_ratio,
            0.96,
            1.15,
        ),
        spica_voice_volume=_config_float(
            raw,
            "spica_voice_volume",
            defaults.spica_voice_volume,
            0.0,
            1.0,
        ),
    )


def save_overlay_config_value(key: str, value: Any, path: Path | None = None) -> bool:
    """Merge-safe write of a SINGLE overlay-config key, preserving every other
    (hand-edited) key in the file. This is the ONLY writer of overlay_config.json --
    every other field stays load-only (see load_overlay_config); only the voice volume
    is persisted this way. Never raises: on a missing file it writes a fresh object, on
    an unreadable/corrupt file it skips the write (so a file we cannot parse is left
    intact rather than clobbered), and a failed write degrades to session-only. Returns
    True only when the value was actually persisted."""
    config_path = path or CONFIG_PATH
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("not_object")
    except FileNotFoundError:
        raw = {}
    except Exception as exc:
        logger.warning("event=overlay_config_save_skip path=%s reason=%s", config_path, exc)
        return False

    raw[key] = value
    try:
        config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return True
    except Exception as exc:
        logger.warning("event=overlay_config_save_failed path=%s reason=%s", config_path, exc)
        return False


def _config_float(raw: dict[str, Any], key: str, fallback: float, minimum: float, maximum: float) -> float:
    value = raw.get(key, fallback)
    try:
        number = float(value)
    except (TypeError, ValueError):
        logger.warning("event=overlay_config_field_fallback key=%s value=%r fallback=%s", key, value, fallback)
        number = fallback
    return max(minimum, min(maximum, number))
