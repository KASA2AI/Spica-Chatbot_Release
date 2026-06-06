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


class StreamConfig(BaseModel):
    play_unit_min_chars: int = 18
    play_unit_max_chars: int = 96
    visual_stream_workers: int = 2


class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    character: CharacterConfig = Field(default_factory=CharacterConfig)
    stream: StreamConfig = Field(default_factory=StreamConfig)
    max_tool_rounds: int = 3
