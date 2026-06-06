"""Configuration manager (Phase 3).

Builds a validated :class:`AppConfig` from defaults, an optional
``data/config/app.yaml`` file, and environment overrides (env wins, so behaviour
is identical to today when no file is present). This module and ``secrets.py``
are the only places in business code permitted to read ``os.getenv`` (CLAUDE.md
#4); a guard test (``tests/test_no_getenv.py``) enforces that.

The legacy ``tts_config.json`` / ``visual_config.json`` consolidation into YAML
is intentionally deferred (see ``migrate``); their existing loaders are untouched
in this phase.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from spica.config.schema import AppConfig

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = _REPO_ROOT / "data" / "config" / "app.yaml"


class ConfigManager:
    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH

    def load(self) -> AppConfig:
        """defaults -> optional yaml file -> env overrides -> validate."""
        self._ensure_env_loaded()
        data: dict[str, Any] = {}
        data = self.merge(data, self._read_yaml(self.config_path))
        data = self.merge(data, self._env_overrides())
        data = self.migrate(data)
        return self.validate(data)

    # -- sources --------------------------------------------------------------

    @staticmethod
    def _ensure_env_loaded() -> None:
        load_dotenv(_REPO_ROOT / "xiaosan.env")
        load_dotenv(_REPO_ROOT.parent / "xiaosan.env", override=False)

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}

    @staticmethod
    def _env_overrides() -> dict[str, Any]:
        """Map the historical env knobs onto the AppConfig shape.

        Only keys whose env var is set to a non-empty value are included, so an
        unset/empty var falls through to the file/default -- exactly matching the
        old ``int(os.getenv(X) or N)`` / ``os.getenv(X) or default`` behaviour.
        """
        llm: dict[str, Any] = {}
        if os.getenv("MODEL"):
            llm["model"] = os.getenv("MODEL")
        if os.getenv("OPENAI_BASE_URL"):
            llm["base_url"] = os.getenv("OPENAI_BASE_URL")

        memory: dict[str, Any] = {}
        for env_key, field in (
            ("RECENT_MEMORY_TURNS", "recent_memory_turns"),
            ("RECENT_CONTEXT_LIMIT", "recent_context_limit"),
            ("LONG_TERM_MEMORY_LIMIT", "long_term_memory_limit"),
            ("LONG_TERM_MEMORY_BUDGET_CHARS", "long_term_memory_budget_chars"),
            ("RECENT_TURN_CHAR_LIMIT", "recent_turn_char_limit"),
            ("MAX_LONG_TERM_MEMORIES", "max_long_term_memories"),
        ):
            value = os.getenv(env_key)
            if value:
                memory[field] = int(value)

        character: dict[str, Any] = {}
        if os.getenv("SPICA_USER_NAME"):
            character["interlocutor_name"] = os.getenv("SPICA_USER_NAME")
        if os.getenv("SPICA_CHARACTER_PROFILE"):
            character["profile_override"] = os.getenv("SPICA_CHARACTER_PROFILE")
        if os.getenv("SPICA_SKILL_DIR"):
            character["skill_dir"] = os.getenv("SPICA_SKILL_DIR")

        stream: dict[str, Any] = {}
        for env_key, field in (
            ("PLAY_UNIT_MIN_CHARS", "play_unit_min_chars"),
            ("PLAY_UNIT_MAX_CHARS", "play_unit_max_chars"),
            ("VISUAL_STREAM_WORKERS", "visual_stream_workers"),
        ):
            value = os.getenv(env_key)
            if value:
                stream[field] = int(value)

        overrides: dict[str, Any] = {}
        if llm:
            overrides["llm"] = llm
        if memory:
            overrides["memory"] = memory
        if character:
            overrides["character"] = character
        if stream:
            overrides["stream"] = stream
        if os.getenv("MAX_TOOL_ROUNDS"):
            overrides["max_tool_rounds"] = int(os.getenv("MAX_TOOL_ROUNDS"))
        return overrides

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        """Recursive dict merge; ``override`` wins. Inputs are not mutated."""
        result = dict(base)
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = ConfigManager.merge(result[key], value)
            else:
                result[key] = value
        return result

    def migrate(self, data: dict[str, Any]) -> dict[str, Any]:
        # Phase 3: the tunable knobs were env-only, so there is no legacy on-disk
        # schema to migrate yet. Passthrough placeholder; legacy tts/visual JSON
        # consolidation is deferred to a later phase.
        return data

    @staticmethod
    def validate(data: dict[str, Any]) -> AppConfig:
        return AppConfig.model_validate(data)

    def save(self, config: AppConfig, path: str | Path | None = None) -> None:
        target = Path(path) if path else self.config_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            yaml.safe_dump(config.model_dump(), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
