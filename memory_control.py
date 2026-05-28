from __future__ import annotations

from typing import Any

from character_loader import DEFAULT_INTERLOCUTOR_NAME
from memory_extractor import MemoryCandidate, extract_candidate_memories


def save_extracted_memories(
    memory_store: Any,
    conversation_id: str,
    user_input: str,
    assistant_answer: str,
    max_active_memories: int | None = None,
    interlocutor_name: str = DEFAULT_INTERLOCUTOR_NAME,
) -> dict[str, Any]:
    candidates = extract_candidate_memories(user_input, assistant_answer, interlocutor_name=interlocutor_name)
    memory_ids: list[int] = []
    errors: list[str] = []

    for candidate in candidates:
        try:
            memory_ids.append(_save_candidate(memory_store, conversation_id, candidate))
        except Exception as exc:
            errors.append(str(exc))

    pruned_count = 0
    if max_active_memories and hasattr(memory_store, "prune_memories"):
        try:
            pruned_count = int(memory_store.prune_memories(conversation_id, max_active_memories))
        except Exception as exc:
            errors.append(str(exc))

    result: dict[str, Any] = {
        "memory_candidates": len(candidates),
        "saved_memory_ids": memory_ids,
        "pruned_memories": pruned_count,
    }
    if errors:
        result["memory_errors"] = errors
    return result


def _save_candidate(memory_store: Any, conversation_id: str, candidate: MemoryCandidate) -> int:
    payload = {
        "conversation_id": conversation_id,
        "scope": candidate.scope,
        "content": candidate.content,
        "importance": candidate.importance,
        "memory_key": candidate.memory_key,
        "memory_type": candidate.memory_type,
        "source": candidate.source,
        "confidence": candidate.confidence,
        "pinned": candidate.pinned,
    }
    if hasattr(memory_store, "upsert_memory"):
        return int(memory_store.upsert_memory(**payload))

    fallback_payload = {
        "conversation_id": conversation_id,
        "scope": candidate.scope,
        "content": candidate.content,
        "importance": candidate.importance,
    }
    return int(memory_store.add_memory(**fallback_payload))
