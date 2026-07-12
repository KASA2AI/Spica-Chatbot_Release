"""Qt-free value contract for the existing overlay preference document."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


logger = logging.getLogger(__name__)

MIN_UI_SCALE = 0.6
MAX_UI_SCALE = 1.8


@dataclass(frozen=True, slots=True)
class OverlayFieldSpec:
    default: float
    minimum: float
    maximum: float


OVERLAY_FIELD_SPECS: Mapping[str, OverlayFieldSpec] = MappingProxyType(
    {
        "default_character_scale": OverlayFieldSpec(1.0, 0.5, 1.8),
        "default_ui_scale": OverlayFieldSpec(1.0, MIN_UI_SCALE, MAX_UI_SCALE),
        "default_typewriter_speed": OverlayFieldSpec(1.0, 0.5, 3.0),
        "character_label_height_scale": OverlayFieldSpec(1.12, 0.9, 1.35),
        "overlay_initial_height_scale": OverlayFieldSpec(1.08, 1.0, 1.20),
        "character_max_height_ratio": OverlayFieldSpec(1.08, 0.96, 1.15),
        "spica_voice_volume": OverlayFieldSpec(0.86, 0.0, 1.0),
    }
)


@dataclass(frozen=True)
class OverlayConfig:
    default_character_scale: float = OVERLAY_FIELD_SPECS[
        "default_character_scale"
    ].default
    default_ui_scale: float = OVERLAY_FIELD_SPECS["default_ui_scale"].default
    default_typewriter_speed: float = OVERLAY_FIELD_SPECS[
        "default_typewriter_speed"
    ].default
    character_label_height_scale: float = OVERLAY_FIELD_SPECS[
        "character_label_height_scale"
    ].default
    overlay_initial_height_scale: float = OVERLAY_FIELD_SPECS[
        "overlay_initial_height_scale"
    ].default
    character_max_height_ratio: float = OVERLAY_FIELD_SPECS[
        "character_max_height_ratio"
    ].default
    spica_voice_volume: float = OVERLAY_FIELD_SPECS["spica_voice_volume"].default


def overlay_field_bounds(key: str) -> tuple[float, float] | None:
    spec = OVERLAY_FIELD_SPECS.get(key)
    if spec is None:
        return None
    return spec.minimum, spec.maximum


def resolve_overlay_config(raw: Mapping[str, Any]) -> OverlayConfig:
    values = {
        key: _config_float(raw, key, spec)
        for key, spec in OVERLAY_FIELD_SPECS.items()
    }
    return OverlayConfig(**values)


def _config_float(
    raw: Mapping[str, Any],
    key: str,
    spec: OverlayFieldSpec,
) -> float:
    value = raw.get(key, spec.default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        logger.warning(
            "event=overlay_config_field_fallback key=%s fallback=%s",
            key,
            spec.default,
        )
        number = spec.default
    return max(spec.minimum, min(spec.maximum, number))


__all__ = [
    "MAX_UI_SCALE",
    "MIN_UI_SCALE",
    "OVERLAY_FIELD_SPECS",
    "OverlayConfig",
    "OverlayFieldSpec",
    "overlay_field_bounds",
    "resolve_overlay_config",
]
