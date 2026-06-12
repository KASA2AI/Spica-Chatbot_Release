"""Commit one conversation turn to memory (Phase 6C; C6: long-term backgrounded).

This layer is deliberately ignorant of HOW memory is extracted or stored: it
appends the recent-context turn and calls ``MemoryPort.commit_turn`` --
extraction/dedup live entirely inside the memory adapter (Phase 5).

C6 splits the two writes by latency requirement:
- recent_memory append stays SYNCHRONOUS -- the next turn's recent context needs
  it before this turn's ``done`` (N4-memory).
- the long-term ``commit_turn`` is fire-and-forget via the injected ``JobRunner``
  (``deps.jobs``) so it never blocks the hot path. Inline in tests + the sync path
  (so cross-turn retrieval sees it); threaded in streaming (the orchestrator drains
  it before the stream closes). A failure lands in metadata + a WARNING log
  (review #6: silent loss is how memories vanish unnoticed) -- it must never
  touch the event stream.

Qt-free.
"""

from __future__ import annotations

import logging
from typing import Any

from spica.conversation.character_loader import DEFAULT_INTERLOCUTOR_NAME
from spica.conversation.time_context import format_local_time_for_prompt
from agent_tools.function_tools.screen.schema import screen_observation_context_for_next_turn
from spica.ports.memory import MemoryScope
from spica.runtime.context import TurnContext
from spica.runtime.deps import TurnDeps

logger = logging.getLogger(__name__)


def save_stream_memory(ctx: TurnContext, services: Any, deps: Any = None) -> None:
    deps = deps or TurnDeps.from_legacy_services(services)
    answer_text = (ctx.answer.answer if ctx.answer else None) or ""

    # recent_memory append is SYNCHRONOUS and must complete before `done` (N4-memory).
    try:
        services.recent_memory.append_turn(
            ctx.request.conversation_id,
            ctx.user_input,
            answer_text,
            user_local_time=(
                format_local_time_for_prompt(ctx.user_local_time)
                if ctx.request.include_user_time_context
                else None
            ),
            interaction_mode=ctx.request.interaction_mode,
            screen_observation_context=screen_observation_context_for_next_turn(ctx.screen_observation),
        )
    except Exception as exc:
        logger.warning("memory commit failed (recent append): %s", exc, exc_info=True)
        ctx.metadata["memory_error"] = str(exc)

    # Long-term commit is fire-and-forget via the injected JobRunner (C6).
    interlocutor = str(deps.config.character.interlocutor_name or DEFAULT_INTERLOCUTOR_NAME)
    # §27① write-side symmetry (stage 2): commit under the same effective id the
    # retrieve node reads (stages.retrieve_long_term_memory_node), so a galgame
    # turn's extracted memories land in the caller's ORIGINAL conversation scope,
    # not the galgame namespace. Plain chat turns leave memory_conversation_id
    # unset -> effective == the raw conversation_id -> byte-identical to before.
    scope = MemoryScope(
        character_id=str(deps.config.character.character_id or "spica"),
        user_id=interlocutor,
        conversation_id=ctx.request.effective_memory_conversation_id,
    )
    meta = {
        "interlocutor_name": interlocutor,
        "max_active_memories": deps.config.memory.max_long_term_memories,
    }

    def _commit_long_term() -> None:
        try:
            result = deps.memory.commit_turn(scope, ctx.user_input, answer_text, meta=meta)
            ctx.metadata.update(result)
        except Exception as exc:
            logger.warning("memory commit failed (long-term): %s", exc, exc_info=True)
            ctx.metadata["memory_error"] = str(exc)

    deps.jobs.submit(_commit_long_term)
