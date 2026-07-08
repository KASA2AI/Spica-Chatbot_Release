from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# P0b step 2a: ONE coercion implementation. Env-side semantics (strip, bool
# wordlist, unparseable-int fallthrough) live in manager.screen_env_config_
# overrides; file-side semantics (falsy->default, truthy bools, clamp) live in
# ScreenConfig's validator. This loader is just the merge: env > json > defaults
# (the LEGACY chain; resolve_effective_screen_config below switches between it
# and the app.yaml chain by legacy-file existence -- P0b step 3, D6).
from spica.config.manager import ConfigManager, screen_env_config_overrides
from spica.config.schema import AppConfig, ScreenConfig


BASE_DIR = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = BASE_DIR / "config" / "screen_vision_config.json"

_LOCAL_CONFIG_KEYS = set(ScreenConfig.model_fields)

logger = logging.getLogger(__name__)


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


def resolve_effective_screen_config(
    config: AppConfig | None = None,
    legacy_path: str | Path | None = None,
) -> ScreenPipelineConfig:
    """P0b step 3 (D6): the carrier switch -- one WHOLE chain or the other,
    selected by legacy-file existence, never a merge of both.

    - legacy json present -> the OLD chain entirely (env > json > defaults via
      load_screen_config) + a migration WARNING;
    - absent -> the NEW chain entirely (env > app.yaml > defaults via
      AppConfig.screen; step 2a pinned both entry points equal).

    ``config``/``legacy_path`` are injectable so the Layer A snapshot's
    three-pass differential can point both carriers at nonexistent paths.
    """
    path = Path(legacy_path) if legacy_path is not None else DEFAULT_CONFIG_PATH
    if path.exists():
        logger.warning(
            "screen 配置仍由旧载体 %s 生效(整条旧链 env>json>defaults)；"
            "已迁移至 data/config/app.yaml 体系，请运行 scripts/migrate_config_p0b.py，"
            "下一版本停读旧 json",
            path,
        )
        return load_screen_config(path)
    if config is None:
        config = ConfigManager().load()
    return ScreenPipelineConfig(**config.screen.model_dump())
