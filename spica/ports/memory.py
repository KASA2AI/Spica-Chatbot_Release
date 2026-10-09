"""One personal memory entrance: evidence, verified records and their lifecycle.

The core owns service lifetime. Adapters own persistence and retrieval; business
tasks and group/game memory retain their existing owners. This module is types
only, including the capabilities now used by restore and channel receipts.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterator, Sequence
from threading import Event
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from spica.ports.model import BoundModel


class EvidenceAdmissionError(ValueError):
    """A replay conflicts with an existing or withdrawn admitted message."""


@dataclass(frozen=True)
class MemoryScope:
    character_id: str
    user_id: str
    conversation_id: str | None = None


@dataclass(frozen=True)
class MemorySourceQuote:
    evidence_id: int
    quote: str
    kind: str
    source: str
    modality: str
    receipt: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class MemorySourceTime:
    evidence_id: int
    recorded_at: float
    occurred_at: float | None = None


@dataclass(frozen=True)
class MemoryItem:
    text: str
    score: float
    type: str | None = None
    ts: float | None = None
    importance: float | None = None  # reserved; SQLite may leave None
    scope: str | None = None  # which bucket the item came from (user/character/...)
    id: int | None = None
    source_ids: tuple[int, ...] = ()
    revision: int = 1
    revision_reason: str | None = None
    status: str = "active"
    valid_from: float | None = None
    valid_until: float | None = None
    # Only retained fragments visible within this role; shared facts expose
    # their derived text and locators, never another role's original dialogue.
    source_quotes: tuple[MemorySourceQuote, ...] = ()
    # Source dates survive raw retention and do not substitute consolidation time.
    source_times: tuple[MemorySourceTime, ...] = ()


@dataclass(frozen=True)
class MemoryRecall(Sequence[MemoryItem]):
    items: tuple[MemoryItem, ...] = ()
    status: Literal["matched", "no_match", "unavailable"] = "no_match"

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index):
        return self.items[index]

    def __iter__(self) -> Iterator[MemoryItem]:
        return iter(self.items)


@runtime_checkable
class MemoryPort(Protocol):
    def record_evidence(
        self, scope: MemoryScope, *, event_id: str, kind: str, content: str,
        source: str, modality: str = "text", occurred_at: float | None = None,
        metadata: dict | None = None,
        reject_withdrawn: bool = False,
    ) -> int:
        """Synchronously retain a sourced observation, independent of delivery."""
        ...

    def evidence(self, scope: MemoryScope, *, ids: list[int] | None = None, limit: int = 100) -> list[dict]:
        """Read original support inside the same authenticated character scope."""
        ...

    def remember(self, scope: MemoryScope, content: str, *, category: str = "user", importance: float = 0.8) -> int:
        ...

    def list_memory(self, scope: MemoryScope, *, limit: int = 50, include_history: bool = False,
                    before_id: int | None = None) -> list[dict]:
        ...

    def forget(self, scope: MemoryScope, memory_id: int) -> None:
        ...

    def revise(self, scope: MemoryScope, memory_id: int, content: str, *, reason: str = "correction") -> int:
        """Correct an error or record a real change; only the latter keeps true history."""
        ...

    def working_context(self, scope: MemoryScope, query: str, *, exclude_turn_id: str | None = None,
                        token_budget: int | None = None, event_binding=None,
                        cancelled: Event | None = None) -> dict:
        ...

    def context_is_current(self, scope: MemoryScope, dependencies: dict) -> bool:
        """Recheck selected sources before a new model request, without scope-wide invalidation."""
        ...

    def clear_context(self, scope: MemoryScope, *, clear_long_term: bool = False) -> None:
        """Reset working context without deleting long-term facts or original evidence."""
        ...

    def expire_raw(self, *, now: float | None = None) -> int:
        ...

    def start_maintenance(self, model: BoundModel, *, idle_seconds: float = 30.0) -> None:
        ...

    def maintenance_status(self, scope: MemoryScope) -> dict:
        """Report local maintenance and bounded automatic-processing state."""
        ...

    def shutdown(self, timeout: float = 1.5) -> bool:
        ...

    def export_archive(self, *, character_id: str | None = None) -> dict:
        ...

    def restore_archive(self, archive: dict, *, character_id: str) -> dict:
        ...

    def record_presentation(self, scope: MemoryScope, turn_id: str, *, outcome: str, endpoint: str,
                            audio_units: int = 0) -> int | None:
        ...

    def commit_turn(
        self,
        scope: MemoryScope,
        user_text: str,
        assistant_text: str,
        meta: dict | None = None,
    ) -> dict:
        """Persist one conversation turn. Extraction is the backend's concern."""
        ...

    def retrieve(self, scope: MemoryScope, query: str, limit: int) -> MemoryRecall:
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
