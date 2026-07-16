"""Qt-free contracts for coordinating one desktop conversation turn."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterator, Protocol

from spica.core.events import RuntimeEvent


class ConversationTurnKind(str, Enum):
    USER = "user"
    SYSTEM = "system"


class AdmissionDecision(str, Enum):
    ACCEPTED = "accepted"
    BUSY = "busy"
    CANCELLED = "cancelled"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"


class PresentationTerminalOutcome(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class ConversationCancellation:
    """One token that orders cancellation against Coordinator admission.

    The Coordinator-only ``_publish_if_not_cancelled`` operation holds this
    token's tiny state lock while an accepted record is published. Cancellation
    therefore either wins before publication, or sets the exact event handed to
    the admitted producer; it never waits for request copying, fingerprinting,
    or producer execution.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._reason: str | None = None

    @property
    def event(self) -> threading.Event:
        return self._event

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def cancel(self, reason: str) -> bool:
        with self._lock:
            if not self._event.is_set():
                self._reason = str(reason or "cancelled")
                self._event.set()
            return True

    def _publish_if_not_cancelled(self, publish: Callable[[], None]) -> bool:
        """Coordinator-owned atomic check-and-publish; not a caller API."""

        with self._lock:
            if self._event.is_set():
                return False
            publish()
            return True


@dataclass(frozen=True)
class ConversationRequest:
    request_id: str
    requested_conversation_id: str
    kind: ConversationTurnKind
    content: str
    source: str = ""
    visual_overrides: dict[str, Any] = field(default_factory=dict)
    include_user_time_context: bool = True
    screen_attachment: dict[str, Any] | None = None


@dataclass(frozen=True)
class ConversationEventEnvelope:
    requested_conversation_id: str
    turn_id: str
    sequence: int
    event: RuntimeEvent


class AcceptedTurnPort(Protocol):
    request_id: str
    turn_id: str

    def events(self) -> Iterator[ConversationEventEnvelope]: ...

    def cancel(self, reason: str) -> bool: ...


@dataclass(frozen=True)
class Admission:
    decision: AdmissionDecision
    request_id: str
    turn_id: str | None = None
    accepted_turn: AcceptedTurnPort | None = None


class ConversationExecutorPort(Protocol):
    def stream(
        self,
        request: ConversationRequest,
        *,
        cancelled: threading.Event,
    ) -> Iterator[RuntimeEvent]: ...
