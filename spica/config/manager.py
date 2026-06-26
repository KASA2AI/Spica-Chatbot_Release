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

from spica.config.env_roster import RESPEAKER_ENV_MAP, SCREEN_ENV_MAP
from spica.config.schema import AppConfig

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = _REPO_ROOT / "data" / "config" / "app.yaml"


def screen_env_overrides() -> dict[str, str | None]:
    """Raw env strings for the screen domain (P0b step 1, F6 收编).

    The env NAMES live in ``env_roster.SCREEN_ENV_MAP``. Values are returned
    RAW (None when unset) and read at CALL time -- no dotenv priming here,
    exactly matching the loader's old direct ``os.getenv`` behaviour (the
    entry point primes env first, CLAUDE.md #10). Step 2a layers the env-side
    coercion on top in ``screen_env_config_overrides``.
    """
    return {field: os.getenv(name) for field, name in SCREEN_ENV_MAP.items()}


_SCREEN_ENV_BOOL_FIELDS = (
    "enabled", "reasoning", "preload", "ocr_enabled", "log_timing", "debug_save_images",
)
_SCREEN_ENV_TRUE_WORDS = {"1", "true", "yes", "y", "on"}


def screen_env_config_overrides() -> dict[str, Any]:
    """Coerced, set-keys-only env overrides for the screen section (P0b 2a).

    Replicates the pre-2a loader's ENV-side semantics exactly (Layer B pins):
    - empty/whitespace-only env -> key OMITTED (falls through to file/default);
    - bools -> the 1/true/yes/y/on wordlist, case-insensitive; any other
      non-empty string coerces to False AND overrides the file value;
    - max_side -> included only when int()-parseable (an unparseable env int
      falls through to the file value -- unlike infer_timeout_sec, whose
      invalid env value IS included and coerces to the default downstream,
      skipping the file value: the pinned asymmetry);
    - strings -> whitespace-stripped (env values were always stripped; file
      values never are).

    The returned dict feeds both ``ScreenConfig`` resolution paths: the screen
    loader's merge (env > json > defaults) and ``_env_overrides`` below
    (env > app.yaml > defaults).
    """
    overrides: dict[str, Any] = {}
    for field, value in screen_env_overrides().items():
        cleaned = (value or "").strip()
        if not cleaned:
            continue
        if field in _SCREEN_ENV_BOOL_FIELDS:
            overrides[field] = cleaned.lower() in _SCREEN_ENV_TRUE_WORDS
        elif field == "max_side":
            try:
                int(cleaned)
            except ValueError:
                continue  # unparseable env int falls through to file (pinned)
            overrides[field] = cleaned
        else:
            overrides[field] = cleaned
    return overrides


def respeaker_env_overrides() -> dict[str, str | None]:
    """Raw env strings for the ReSpeaker hardware layer (P0b step 1, D2).

    Same contract as ``screen_env_overrides``: raw values, call-time reads,
    coercion stays at the consumer (``hardware/respeaker``).
    """
    return {field: os.getenv(name) for field, name in RESPEAKER_ENV_MAP.items()}


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
        if os.getenv("REASONING_EFFORT"):
            llm["reasoning_effort"] = os.getenv("REASONING_EFFORT")

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

        # Reaction-judge LLM endpoint (the ONLY galgame fields with env names): the
        # non-secret base_url + model halves of a swappable judge endpoint. The key
        # half is the secret JUDGE_API_KEY (secrets.py). Roster: env_roster.APP_ENV_MAP.
        galgame: dict[str, Any] = {}
        if os.getenv("JUDGE_MODEL"):
            galgame["reaction_judge_model"] = os.getenv("JUDGE_MODEL")
        if os.getenv("JUDGE_BASE_URL"):
            galgame["reaction_judge_base_url"] = os.getenv("JUDGE_BASE_URL")
        if os.getenv("JUDGE_REASONING_EFFORT"):
            galgame["reaction_judge_reasoning_effort"] = os.getenv("JUDGE_REASONING_EFFORT")

        overrides: dict[str, Any] = {}
        if llm:
            overrides["llm"] = llm
        if memory:
            overrides["memory"] = memory
        if character:
            overrides["character"] = character
        if stream:
            overrides["stream"] = stream
        if galgame:
            overrides["galgame"] = galgame
        # P0b 2a: the screen section folds env with the SCREEN coercion rules
        # (wordlist bools, clamp ints) -- NOT the loud int() the knobs above use.
        screen = screen_env_config_overrides()
        if screen:
            overrides["screen"] = screen
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
