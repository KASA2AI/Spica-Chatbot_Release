from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = BASE_DIR / "config" / "screen_vision_config.json"

_DEFAULTS: dict[str, Any] = {
    "provider": "openai_compatible",
    "api_key_env": "SPICA_SCREEN_API_KEY",
    "base_url_env": "SPICA_SCREEN_BASE_URL",
    "model_env": "SPICA_SCREEN_MODEL",
    "default_base_url": "https://api.openai.com/v1",
    "default_model": "gpt-4.1-mini",
    "max_long_edge": 1536,
    "jpeg_quality": 75,
    "image_detail": "low",
    "request_timeout_seconds": 30,
    "debug_save_images": False,
}


@dataclass(frozen=True)
class ScreenVisionConfig:
    provider: str
    api_key_env: str
    base_url_env: str
    model_env: str
    default_base_url: str
    default_model: str
    api_key: str | None
    base_url: str
    model: str
    max_long_edge: int
    jpeg_quality: int
    image_detail: str
    request_timeout_seconds: float
    debug_save_images: bool


def load_screen_vision_config(path: str | Path | None = None) -> ScreenVisionConfig:
    raw = dict(_DEFAULTS)
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
        if isinstance(loaded, dict):
            raw.update(loaded)

    api_key_env = str(raw.get("api_key_env") or _DEFAULTS["api_key_env"])
    base_url_env = str(raw.get("base_url_env") or _DEFAULTS["base_url_env"])
    model_env = str(raw.get("model_env") or _DEFAULTS["model_env"])

    api_key = _clean_env(os.getenv(api_key_env))
    base_url = _clean_env(os.getenv(base_url_env)) or str(raw.get("default_base_url") or _DEFAULTS["default_base_url"])
    model = _clean_env(os.getenv(model_env)) or str(raw.get("default_model") or _DEFAULTS["default_model"])

    return ScreenVisionConfig(
        provider=str(raw.get("provider") or _DEFAULTS["provider"]),
        api_key_env=api_key_env,
        base_url_env=base_url_env,
        model_env=model_env,
        default_base_url=str(raw.get("default_base_url") or _DEFAULTS["default_base_url"]),
        default_model=str(raw.get("default_model") or _DEFAULTS["default_model"]),
        api_key=api_key,
        base_url=base_url,
        model=model,
        max_long_edge=_bounded_int(
            os.getenv("SPICA_SCREEN_MAX_LONG_EDGE"),
            raw.get("max_long_edge"),
            default=1536,
            minimum=256,
            maximum=4096,
        ),
        jpeg_quality=_bounded_int(
            os.getenv("SPICA_SCREEN_JPEG_QUALITY"),
            raw.get("jpeg_quality"),
            default=75,
            minimum=30,
            maximum=95,
        ),
        image_detail=_clean_env(os.getenv("SPICA_SCREEN_IMAGE_DETAIL"))
        or str(raw.get("image_detail") or "low"),
        request_timeout_seconds=_positive_float(raw.get("request_timeout_seconds"), default=30.0),
        debug_save_images=_env_bool(
            os.getenv("SPICA_SCREEN_DEBUG_SAVE"),
            default=bool(raw.get("debug_save_images", False)),
        ),
    )


def _clean_env(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _bounded_int(env_value: str | None, config_value: Any, *, default: int, minimum: int, maximum: int) -> int:
    for candidate in (env_value, config_value, default):
        try:
            value = int(candidate)
            return max(minimum, min(maximum, value))
        except (TypeError, ValueError):
            continue
    return default


def _positive_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _env_bool(value: str | None, *, default: bool) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
