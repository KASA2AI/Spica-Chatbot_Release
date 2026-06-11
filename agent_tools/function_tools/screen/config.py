from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# P0b step 1: env reads live in the config layer (CLAUDE.md #4). This loader
# gets RAW strings keyed by field name and keeps every coercion below
# unchanged; it no longer knows any env variable name.
from spica.config.manager import screen_env_overrides


BASE_DIR = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = BASE_DIR / "config" / "screen_vision_config.json"

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "provider": "moondream_local",
    "model_id": "vikhyatk/moondream2",
    "revision": "2025-06-21",
    "device": "cuda",
    "dtype": "bfloat16",
    "max_side": 768,
    "reasoning": False,
    "preload": False,
    "ocr_enabled": True,
    "ocr_engine": "rapidocr",
    "capture_format": "png",
    "infer_timeout_sec": 30,
    "log_timing": True,
    "debug_save_images": False,
}

_LOCAL_CONFIG_KEYS = set(_DEFAULTS)


@dataclass(frozen=True)
class ScreenPipelineConfig:
    enabled: bool
    provider: str
    model_id: str
    revision: str
    device: str
    dtype: str
    max_side: int
    reasoning: bool
    preload: bool
    ocr_enabled: bool
    ocr_engine: str
    capture_format: str
    infer_timeout_sec: float
    log_timing: bool
    debug_save_images: bool


def load_screen_config(path: str | Path | None = None) -> ScreenPipelineConfig:
    raw = dict(_DEFAULTS)
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
        if isinstance(loaded, dict):
            raw.update(_local_config_items(loaded))
    env = screen_env_overrides()
    revision = _clean_env(env["revision"]) or str(raw.get("revision") or _DEFAULTS["revision"])

    return ScreenPipelineConfig(
        enabled=_env_bool(env["enabled"], default=bool(raw.get("enabled", True))),
        provider=_clean_env(env["provider"])
        or str(raw.get("provider") or _DEFAULTS["provider"]),
        model_id=_clean_env(env["model_id"])
        or str(raw.get("model_id") or _DEFAULTS["model_id"]),
        revision=revision,
        device=_clean_env(env["device"])
        or str(raw.get("device") or _DEFAULTS["device"]),
        dtype=_normalize_dtype(
            _clean_env(env["dtype"]) or str(raw.get("dtype") or _DEFAULTS["dtype"])
        ),
        max_side=_bounded_int(
            env["max_side"],
            raw.get("max_side"),
            default=768,
            minimum=128,
            maximum=4096,
        ),
        reasoning=_env_bool(env["reasoning"], default=bool(raw.get("reasoning", False))),
        preload=_env_bool(env["preload"], default=bool(raw.get("preload", False))),
        ocr_enabled=_env_bool(
            env["ocr_enabled"],
            default=bool(raw.get("ocr_enabled", True)),
        ),
        ocr_engine=_clean_env(env["ocr_engine"])
        or str(raw.get("ocr_engine") or _DEFAULTS["ocr_engine"]),
        capture_format=_normalize_capture_format(
            _clean_env(env["capture_format"])
            or str(raw.get("capture_format") or _DEFAULTS["capture_format"])
        ),
        infer_timeout_sec=_positive_float(
            _clean_env(env["infer_timeout_sec"]) or raw.get("infer_timeout_sec"),
            default=30.0,
        ),
        log_timing=_env_bool(env["log_timing"], default=bool(raw.get("log_timing", True))),
        debug_save_images=_env_bool(
            env["debug_save_images"],
            default=bool(raw.get("debug_save_images", False)),
        ),
    )


def _clean_env(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _local_config_items(raw: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in raw.items() if key in _LOCAL_CONFIG_KEYS}


def _normalize_dtype(value: str) -> str:
    normalized = (value or "").strip().lower()
    return normalized if normalized in {"bfloat16", "float16", "float32", "auto"} else "auto"


def _normalize_capture_format(value: str) -> str:
    normalized = (value or "").strip().lower()
    return normalized if normalized in {"png"} else "png"


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
