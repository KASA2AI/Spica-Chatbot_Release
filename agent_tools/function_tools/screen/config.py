from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# P0b step 2a: ONE coercion implementation. Env-side semantics (strip, bool
# wordlist, unparseable-int fallthrough) live in manager.screen_env_config_
# overrides; file-side semantics (falsy->default, truthy bools, clamp) live in
# ScreenConfig's validator. This loader is just the merge: env > json > defaults
# (the json file remains authoritative until P0b step 3 folds it into app.yaml).
from spica.config.manager import screen_env_config_overrides
from spica.config.schema import ScreenConfig


BASE_DIR = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = BASE_DIR / "config" / "screen_vision_config.json"

_LOCAL_CONFIG_KEYS = set(ScreenConfig.model_fields)


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
    raw: dict[str, Any] = {}
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
        if isinstance(loaded, dict):
            raw.update(_local_config_items(loaded))
    raw.update(screen_env_config_overrides())
    model = ScreenConfig.model_validate(raw)
    return ScreenPipelineConfig(**model.model_dump())


def _local_config_items(raw: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in raw.items() if key in _LOCAL_CONFIG_KEYS}
