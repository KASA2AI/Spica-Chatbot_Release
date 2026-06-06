"""Commit one conversation turn to memory (Phase 6C).

Moved from agent/streaming_pipeline.py. This layer is deliberately ignorant of
HOW memory is extracted or stored: it appends the recent-context turn and calls
``MemoryPort.commit_turn`` -- extraction/dedup live entirely inside the memory
adapter (Phase 5). There is NO extraction logic here (CLAUDE.md Phase 6C
acceptance). Qt-free.
"""

from __future__ import annotations

from typing import Any

from agent.character_loader import DEFAULT_INTERLOCUTOR_NAME
from agent.time_context import format_local_time_for_prompt
from agent_tools.function_tools.screen.schema import screen_observation_context_for_next_turn
from spica.adapters.memory import SqliteMemoryAdapter
from spica.ports.memory import MemoryScope


def memory_adapter(services: Any) -> Any:
    return services.memory_adapter or SqliteMemoryAdapter(services.memory_store, services.recent_memory)


def save_stream_memory(state: Any, services: Any) -> None:
    try:
        services.recent_memory.append_turn(
            state.conversation_id,
            state.user_input,
            state.answer or "",
            user_local_time=(
                format_local_time_for_prompt(state.user_local_time)
                if state.include_user_time_context
                else None
            ),
            interaction_mode=state.interaction_mode,
            screen_observation_context=screen_observation_context_for_next_turn(state.screen_observation),
        )
        interlocutor = str(services.config.get("interlocutor_name") or DEFAULT_INTERLOCUTOR_NAME)
        result = memory_adapter(services).commit_turn(
            MemoryScope(
                character_id=str(services.config.get("character_id") or "spica"),
                user_id=interlocutor,
                conversation_id=state.conversation_id,
            ),
            state.user_input,
            state.answer or "",
            meta={
                "interlocutor_name": interlocutor,
                "max_active_memories": int(services.config.get("max_long_term_memories", 200)),
            },
        )
        state.metadata.update(result)
    except Exception as exc:
        state.metadata["memory_error"] = str(exc)
