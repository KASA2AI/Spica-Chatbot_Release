"""Memory capability port (Phase 5).

Deliberately a *superset* shaped for generative / external memory systems
(Mem0 / EverOS / supermemory style), NOT a copy of today's SQLite
``retrieve/upsert``. Rationale (see REFACTOR_PLAN Phase 5 design note):

1. Who extracts: such backends extract facts from raw turns themselves, so the
   write side takes a whole turn (``commit_turn``), not a structured record.
2. What ``retrieve`` returns: scored items + an optional profile/context block.
3. Sync vs async + scope: backends carry latency and split by
   (character, user, conversation), so ``MemoryScope`` keys every call and must
   align with Phase 7's CharacterPackage identity.

Phase 5 only fixes the signatures and leaves optional capabilities as no-op
hooks (``run_maintenance`` / ``supports``); the SQLite adapter implements the
subset. Do NOT implement sleep-consolidation / archival / file-space here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class MemoryScope:
    character_id: str
    user_id: str
    conversation_id: str | None = None


@dataclass(frozen=True)
class MemoryItem:
    text: str
    score: float
    type: str | None = None
    ts: float | None = None
    importance: float | None = None  # reserved; SQLite may leave None
    scope: str | None = None  # which bucket the item came from (user/character/...)


@runtime_checkable
class MemoryPort(Protocol):
    def commit_turn(
        self,
        scope: MemoryScope,
        user_text: str,
        assistant_text: str,
        meta: dict | None = None,
    ) -> dict:
        """Persist one conversation turn. Extraction is the backend's concern."""
        ...

    def retrieve(self, scope: MemoryScope, query: str, limit: int) -> list[MemoryItem]:
        ...

    def get_context_block(self, scope: MemoryScope) -> str | None:
        """Optional profile/preamble to inject; SQLite may return None."""
        ...

    # -- reserved optional extension points (Phase 5: hooks only) -------------
    def run_maintenance(self, scope: MemoryScope, reason: str) -> None:
        """Idle/sleep consolidation, archival, expiry. Backend decides; may no-op."""
        ...

    def supports(self, capability: str) -> bool:
        """Declare optional capabilities, e.g. 'file_space' / 'sleep_consolidation'."""
        ...
