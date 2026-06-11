"""Typed application configuration (Phase 3).

Pydantic models for every tunable knob the conversation core reads. Defaults
match the historical ``os.getenv(...) or N`` fallbacks exactly, so building an
``AppConfig`` with no env and no file reproduces today's behaviour.

INVARIANT (CLAUDE.md #1 + #4): this layer is Qt-free and -- together with
``manager.py`` / ``secrets.py`` -- is the only place allowed to source
configuration. It must NOT import the ``agent`` package: agent-specific defaults
(interlocutor name, skill dir) are applied in ``agent`` so this layer stays
character-agnostic and there is no ``agent -> spica.config -> agent`` cycle.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class LLMConfig(BaseModel):
    provider: str = "openai_compatible"
    model: str = "gpt-4.1-mini"
    base_url: str | None = None


class MemoryConfig(BaseModel):
    provider: str = "sqlite"
    recent_memory_turns: int = 3
    recent_context_limit: int = 3
    long_term_memory_limit: int = 5
    long_term_memory_budget_chars: int = 1200
    recent_turn_char_limit: int = 360
    max_long_term_memories: int = 200


class CharacterConfig(BaseModel):
    # All optional. When unset, the agent layer applies DEFAULT_INTERLOCUTOR_NAME
    # / DEFAULT_SPICA_SKILL_DIR, keeping this layer character-agnostic.
    interlocutor_name: str | None = None
    profile_override: str | None = None
    skill_dir: str | None = None
    package_dir: str | None = None  # active CharacterPackage dir (Phase 7); None -> Spica
    # Resolved active character id (from the CharacterPackage); None -> "spica".
    # Set by the host after package load so the typed deps namespace memory by it.
    character_id: str | None = None
    # Resolved at assembly time (C4): the built persona text and display name the
    # prompt builder uses. Host writes these back so the turn reads them off
    # deps.config instead of the legacy services.config dict. None -> the prompt
    # builder's DEFAULT_CHARACTER_PROFILE / DEFAULT_CHARACTER_NAME fallback.
    character_profile: str | None = None
    character_name: str | None = None


class StreamConfig(BaseModel):
    play_unit_min_chars: int = 18
    play_unit_max_chars: int = 96
    visual_stream_workers: int = 2


class GalgameConfig(BaseModel):
    # Phase 8: galgame story summarization. ``summary_model`` is a dedicated config
    # slot for the summary LLM; None -> fall back to the dialogue model (config.llm),
    # so a future split onto a different model needs no code change. The same
    # endpoint/client is reused either way.
    summary_model: str | None = None
    summary_trigger_chars: int = 2000  # background summary fires ~every this many unsummarized chars
    # OCR sampling interval (seconds) the companion controller hands the OCR loop.
    # 0.3 (not 1.0) so fast page-turns are still sampled often enough to settle a line.
    ocr_interval_seconds: float = 0.3


# -- screen section coercion helpers (P0b step 2a) -----------------------------
# Moved VERBATIM from agent_tools/function_tools/screen/config.py so the typed
# section below is the ONE coercion implementation (the screen loader routes
# through ScreenConfig.model_validate; Layer B pins every branch).


def _normalize_dtype(value: str) -> str:
    normalized = (value or "").strip().lower()
    return normalized if normalized in {"bfloat16", "float16", "float32", "auto"} else "auto"


def _normalize_capture_format(value: str) -> str:
    normalized = (value or "").strip().lower()
    return normalized if normalized in {"png"} else "png"


def _positive_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


_SCREEN_STRING_FIELDS = ("provider", "model_id", "revision", "device", "ocr_engine")
_SCREEN_BOOL_FIELDS = (
    "enabled", "reasoning", "preload", "ocr_enabled", "log_timing", "debug_save_images",
)


class ScreenConfig(BaseModel):
    """Typed screen-pipeline section (P0b step 2a).

    Defaults match the pre-2a ``_DEFAULTS`` dict verbatim. The before-validator
    replicates the legacy loader's FILE-side coercion exactly (env-side coercion
    -- the bool wordlist, the unparseable-int fallthrough -- happens in
    ``manager.screen_env_config_overrides`` BEFORE values reach this model, so
    the env/file asymmetries pinned by test_resolved_config_equivalence hold):

    - falsy strings -> field default (``raw.get(k) or DEFAULT`` semantics);
      values are NOT whitespace-stripped here (only env values were stripped);
    - bools -> plain ``bool()`` truthiness (json ``"no"`` -> True, as before --
      the 1/true/yes wordlist applied ONLY to env strings);
    - max_side -> ``int()`` then clamp to [128, 4096]; unparseable -> default;
    - infer_timeout_sec -> ``_positive_float`` (invalid/non-positive -> 30.0);
    - dtype / capture_format normalized through their whitelists.
    """

    enabled: bool = True
    provider: str = "moondream_local"
    model_id: str = "vikhyatk/moondream2"
    revision: str = "2025-06-21"
    device: str = "cuda"
    dtype: str = "bfloat16"
    max_side: int = 768
    reasoning: bool = False
    preload: bool = False
    ocr_enabled: bool = True
    ocr_engine: str = "rapidocr"
    capture_format: str = "png"
    infer_timeout_sec: float = 30.0
    log_timing: bool = True
    debug_save_images: bool = False

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_semantics(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for key in _SCREEN_STRING_FIELDS:
            if key in out:
                if not out[key]:
                    out.pop(key)  # falsy file value -> default (`or` semantics)
                else:
                    out[key] = str(out[key])
        if "dtype" in out:
            if not out["dtype"]:
                out.pop("dtype")
            else:
                out["dtype"] = _normalize_dtype(str(out["dtype"]))
        if "capture_format" in out:
            if not out["capture_format"]:
                out.pop("capture_format")
            else:
                out["capture_format"] = _normalize_capture_format(str(out["capture_format"]))
        for key in _SCREEN_BOOL_FIELDS:
            if key in out:
                out[key] = bool(out[key])
        if "max_side" in out:
            try:
                out["max_side"] = max(128, min(4096, int(out["max_side"])))
            except (TypeError, ValueError):
                out.pop("max_side")  # unparseable file value -> default
        if "infer_timeout_sec" in out:
            out["infer_timeout_sec"] = _positive_float(out["infer_timeout_sec"], default=30.0)
        return out


class PluginEntryConfig(BaseModel):
    """One plugin manifest entry (P0b step 3). Mirrors plugins/manifest.py's
    PluginEntry semantics; the str shorthand ("name" == enabled entry) is
    normalized by AppConfig's plugins validator below."""

    name: str
    enabled: bool = True


class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    character: CharacterConfig = Field(default_factory=CharacterConfig)
    stream: StreamConfig = Field(default_factory=StreamConfig)
    galgame: GalgameConfig = Field(default_factory=GalgameConfig)
    screen: ScreenConfig = Field(default_factory=ScreenConfig)
    # P0b step 3 (D-3a): the song section is intentionally UNTYPED -- it is the
    # override dict layered over song/config.py's DEFAULT_CONFIG by the same
    # deep-merge engine the legacy json used (voices are an open name->config
    # map; pydantic-izing that engine in the highest-risk step was rejected).
    # Typed-ization is tracked as debt in GALGAME_FINDINGS.
    song: dict[str, Any] = Field(default_factory=dict)
    plugins: list[PluginEntryConfig] = Field(default_factory=list)
    max_tool_rounds: int = 3

    @field_validator("plugins", mode="before")
    @classmethod
    def _normalize_plugin_entries(cls, value: Any) -> Any:
        # Same tolerant semantics as plugins/manifest.py: str shorthand becomes
        # an enabled entry; blank/invalid items are dropped, not errors.
        if not isinstance(value, list):
            return []
        normalized: list[Any] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                normalized.append({"name": item.strip()})
            elif isinstance(item, dict) and item.get("name"):
                normalized.append(item)
            elif isinstance(item, PluginEntryConfig):
                normalized.append(item)
        return normalized
