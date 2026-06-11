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

from pydantic import BaseModel, Field


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


class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    character: CharacterConfig = Field(default_factory=CharacterConfig)
    stream: StreamConfig = Field(default_factory=StreamConfig)
    galgame: GalgameConfig = Field(default_factory=GalgameConfig)
    max_tool_rounds: int = 3
