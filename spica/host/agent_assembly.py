"""Backend assembly (Phase 6D).

Builds the ``AgentServices`` bundle (LLM client, memory, character profile, tool
functions, config dict) that the conversation core runs on. This is the
assembly half of the dissolved ``SimpleAgent`` and belongs to the host
(composition root); the driving / management half is ``ChatEngine``.

INVARIANT (CLAUDE.md #1 + #4): Qt-free; secrets come from the secrets loader.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from openai import OpenAI

from spica.conversation.character_loader import (
    DEFAULT_CHARACTER_NAME,
    DEFAULT_INTERLOCUTOR_NAME,
    build_character_profile,
    normalize_interlocutor_name,
)
from spica.runtime.services import AgentServices
from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions
from common.timing import log_timing
from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from spica.config.schema import AppConfig
from spica.config.secrets import Secrets

_REPO_ROOT = Path(__file__).resolve().parents[2]


def build_agent_services(
    config: AppConfig,
    secrets: Secrets,
    *,
    tts_adapter=None,
    visual_tool=None,
    character_package=None,
) -> AgentServices:
    api_key = secrets.openai_api_key
    if not api_key:
        raise ValueError("没有读取到 OPENAI_API_KEY，请检查 xiaosan.env")

    client = OpenAI(
        api_key=api_key,
        base_url=config.llm.base_url,
        http_client=httpx.Client(trust_env=False, timeout=15),
    )
    interlocutor_name = normalize_interlocutor_name(
        config.character.interlocutor_name or DEFAULT_INTERLOCUTOR_NAME
    )
    # Active character identity comes from the CharacterPackage (Phase 7);
    # falling back to Spica defaults when no package is supplied.
    if character_package is not None:
        character_id = character_package.character_id
        character_name = character_package.char_name
        skill_dir = character_package.skill_dir
    else:
        character_id = "spica"
        character_name = DEFAULT_CHARACTER_NAME
        skill_dir = config.character.skill_dir
    character_profile = build_character_profile(
        config.character.profile_override,
        skill_dir,
        interlocutor_name,
    )
    # Record the resolved character identity on the typed config so TurnDeps reads
    # the same normalized values the legacy services.config dict carries (C3b/C4).
    config.character.interlocutor_name = interlocutor_name
    config.character.character_id = character_id
    config.character.character_profile = character_profile
    config.character.character_name = character_name
    return AgentServices(
        llm_client=client,
        tts_adapter=tts_adapter,
        visual_tool=visual_tool,
        memory_store=SQLiteMemoryStore(_REPO_ROOT / "spica_data" / "memory.sqlite3"),
        recent_memory=RecentMemory(max_turns=config.memory.recent_memory_turns),
        config={
            "model": config.llm.model,
            "character_profile": character_profile,
            "interlocutor_name": interlocutor_name,
            "recent_context_limit": config.memory.recent_context_limit,
            "long_term_memory_limit": config.memory.long_term_memory_limit,
            "long_term_memory_budget_chars": config.memory.long_term_memory_budget_chars,
            "recent_turn_char_limit": config.memory.recent_turn_char_limit,
            "max_long_term_memories": config.memory.max_long_term_memories,
            "max_tool_rounds": config.max_tool_rounds,
            "play_unit_min_chars": config.stream.play_unit_min_chars,
            "play_unit_max_chars": config.stream.play_unit_max_chars,
            "visual_stream_workers": config.stream.visual_stream_workers,
            "character_id": character_id,
            "character_name": character_name,
        },
        logger=log_timing,
        tool_functions=default_tool_functions(),
        tool_schemas=TOOL_SCHEMAS,
    )
