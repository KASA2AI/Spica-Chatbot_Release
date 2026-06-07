"""SQLite memory adapter (Phase 5).

Implements ``MemoryPort`` by delegating to the existing SQLite store and the
rule-based extraction in ``memory.control`` / ``memory.extractor``. Extraction is
therefore the *backend's* concern, not the pipeline's -- ``commit_turn`` runs it
internally. ``retrieve`` returns ``MemoryItem``s; the optional maintenance /
capability hooks are no-ops for SQLite (the rich surface exists so a future
generative-memory adapter is a drop-in, per the Phase 5 design note).
"""

from __future__ import annotations

from typing import Any

from agent.character_compat import DEFAULT_INTERLOCUTOR_NAME
from memory.control import save_extracted_memories
from spica.ports.memory import MemoryItem, MemoryScope

_SUPPORTED = {"commit_turn", "retrieve"}


def scoped_conversation_id(character_id: str, conversation_id: str | None) -> str:
    # The single definition of the long-term-memory namespace key (Phase 7): the
    # store key is namespaced by character_id so different characters never see
    # each other's memories. ChatEngine's manual remember/list/clear reuse this
    # instead of re-hardcoding the "::" format.
    return f"{character_id}::{conversation_id or 'default'}"


class SqliteMemoryAdapter:
    name = "sqlite"

    def __init__(self, store: Any, recent: Any | None = None, *, max_active_memories: int = 200) -> None:
        self.store = store
        self.recent = recent
        self.max_active_memories = max_active_memories

    def _scoped_conversation_id(self, scope: MemoryScope) -> str:
        return scoped_conversation_id(scope.character_id, scope.conversation_id)

    def commit_turn(
        self,
        scope: MemoryScope,
        user_text: str,
        assistant_text: str,
        meta: dict | None = None,
    ) -> dict:
        meta = meta or {}
        return save_extracted_memories(
            memory_store=self.store,
            conversation_id=self._scoped_conversation_id(scope),
            user_input=user_text,
            assistant_answer=assistant_text,
            max_active_memories=int(meta.get("max_active_memories", self.max_active_memories)),
            interlocutor_name=str(meta.get("interlocutor_name") or DEFAULT_INTERLOCUTOR_NAME),
        )

    def retrieve(self, scope: MemoryScope, query: str, limit: int) -> list[MemoryItem]:
        rows = self.store.search_memories(self._scoped_conversation_id(scope), query, limit=limit)
        items: list[MemoryItem] = []
        for row in rows:
            importance = row.get("importance")
            items.append(
                MemoryItem(
                    text=str(row.get("content", "")),
                    # _row_to_dict carries no "score" key; importance is the proxy.
                    score=float(importance or 0.0),
                    type=row.get("memory_type") or row.get("type"),
                    importance=importance,
                    scope=row.get("scope"),
                )
            )
        return items

    def get_context_block(self, scope: MemoryScope) -> str | None:
        # SQLite backend injects memories via the prompt's [LONG_TERM_MEMORY]
        # section already; no separate profile block.
        return None

    def run_maintenance(self, scope: MemoryScope, reason: str) -> None:
        return None

    def supports(self, capability: str) -> bool:
        return capability in _SUPPORTED
