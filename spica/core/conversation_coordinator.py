"""Single-process owner for desktop foreground conversation admission."""

from __future__ import annotations

import copy
import hashlib
import queue
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterator

from spica.ports.conversation import (
    Admission,
    AdmissionDecision,
    ConversationCancellation,
    ConversationEventEnvelope,
    ConversationExecutorPort,
    ConversationRequest,
    PresentationTerminalOutcome,
)


@dataclass(frozen=True)
class _ProducerEnd:
    error: Exception | None = None


@dataclass
class _TurnRecord:
    request_id: str
    request: ConversationRequest | None
    turn_id: str
    cancellation: ConversationCancellation = field(
        default_factory=ConversationCancellation
    )
    event_queue: queue.Queue[ConversationEventEnvelope | _ProducerEnd] = field(
        default_factory=queue.Queue
    )
    events_claimed: bool = False
    producer_terminal: bool = False
    presentation_terminal: bool = False
    presentation_outcome: PresentationTerminalOutcome | None = None
    cancel_reason: str | None = None

    @property
    def cancel_event(self) -> threading.Event:
        return self.cancellation.event


class AcceptedTurn:
    def __init__(self, coordinator: "ConversationCoordinator", record: _TurnRecord) -> None:
        self._coordinator = coordinator
        self._record = record
        self.request_id = record.request_id
        self.turn_id = record.turn_id

    def events(self) -> Iterator[ConversationEventEnvelope]:
        return self._coordinator._events(self._record)

    def cancel(self, reason: str) -> bool:
        return self._coordinator._cancel(self._record, reason)


class ConversationCoordinator:
    def __init__(self, executor: ConversationExecutorPort) -> None:
        self._executor = executor
        self._lock = threading.Lock()
        self._active_record: _TurnRecord | None = None
        self._request_ledger: dict[str, tuple[bytes, Admission]] = {}
        self._turn_records: dict[str, _TurnRecord] = {}

    def submit(
        self,
        request: ConversationRequest,
        *,
        cancellation: ConversationCancellation | None = None,
    ) -> Admission:
        request_snapshot = copy.deepcopy(request)
        request_fingerprint = _request_fingerprint(request_snapshot)
        cancellation = cancellation or ConversationCancellation()
        with self._lock:
            previous = self._request_ledger.get(request.request_id)
            if previous is not None:
                if previous[0] == request_fingerprint:
                    return previous[1]
                return Admission(
                    decision=AdmissionDecision.IDEMPOTENCY_CONFLICT,
                    request_id=request.request_id,
                    turn_id=previous[1].turn_id,
                )
            if self._active_record is not None:
                admission = Admission(
                    decision=AdmissionDecision.BUSY,
                    request_id=request.request_id,
                )
                self._request_ledger[request.request_id] = (
                    request_fingerprint,
                    admission,
                )
                return admission
            record_to_start = _TurnRecord(
                request_id=request.request_id,
                request=request_snapshot,
                turn_id=uuid.uuid4().hex,
                cancellation=cancellation,
            )
            accepted_turn = AcceptedTurn(self, record_to_start)
            admission = Admission(
                decision=AdmissionDecision.ACCEPTED,
                request_id=request.request_id,
                turn_id=accepted_turn.turn_id,
                accepted_turn=accepted_turn,
            )

            def publish_admission() -> None:
                self._active_record = record_to_start
                self._turn_records[record_to_start.turn_id] = record_to_start
                self._request_ledger[request.request_id] = (
                    request_fingerprint,
                    admission,
                )

            if not self._linearize_admission(cancellation, publish_admission):
                cancelled_admission = Admission(
                    decision=AdmissionDecision.CANCELLED,
                    request_id=request.request_id,
                )
                self._request_ledger[request.request_id] = (
                    request_fingerprint,
                    cancelled_admission,
                )
                return cancelled_admission
            try:
                # The cancel/admission point above covers only record publication.
                # Thread construction/start stay outside the cancellation-token
                # lock, so a slow runtime call cannot block GUI stop. Concurrent
                # submit/replay still waits on the Coordinator lock and cannot
                # observe an orphan; either failure uses this same rollback.
                producer = threading.Thread(
                    target=self._run_producer,
                    args=(record_to_start,),
                    name=f"conversation-turn-{record_to_start.turn_id[:8]}",
                    daemon=True,
                )
                producer.start()
            except Exception:
                if self._active_record is record_to_start:
                    self._active_record = None
                self._turn_records.pop(record_to_start.turn_id, None)
                current = self._request_ledger.get(request.request_id)
                if current is not None and current[1] is admission:
                    self._request_ledger.pop(request.request_id, None)
                raise
        return admission

    def _linearize_admission(
        self,
        cancellation: ConversationCancellation,
        publish: Callable[[], None],
    ) -> bool:
        """The single cancel/admission linearization point.

        Tests may subclass this one seam to pause immediately before or after
        the point; production ownership remains entirely inside Coordinator.
        """

        return cancellation._publish_if_not_cancelled(publish)

    def is_busy(self) -> bool:
        with self._lock:
            return self._active_record is not None

    def report_presentation_terminal(
        self,
        turn_id: str | None,
        outcome: PresentationTerminalOutcome,
    ) -> bool:
        if turn_id is None:
            return False
        with self._lock:
            record = self._turn_records.get(turn_id)
            if record is None:
                return False
            if not record.presentation_terminal:
                record.presentation_terminal = True
                record.presentation_outcome = outcome
                self._maybe_release_locked(record)
            return True

    def _cancel(self, record: _TurnRecord, reason: str) -> bool:
        with self._lock:
            if self._turn_records.get(record.turn_id) is not record:
                return False
            if record.cancel_reason is None:
                record.cancel_reason = str(reason or "cancelled")
            record.cancellation.cancel(reason)
            return True

    def _run_producer(self, record: _TurnRecord) -> None:
        sequence = 0
        error: Exception | None = None
        request = record.request
        if request is None:
            raise RuntimeError("conversation request payload was released before execution")
        try:
            for event in self._executor.stream(
                request,
                cancelled=record.cancel_event,
            ):
                sequence += 1
                record.event_queue.put(
                    ConversationEventEnvelope(
                        requested_conversation_id=request.requested_conversation_id,
                        turn_id=record.turn_id,
                        sequence=sequence,
                        event=event,
                    )
                )
        except Exception as exc:
            error = exc
        finally:
            record.event_queue.put(_ProducerEnd(error=error))
            request = None
            with self._lock:
                record.request = None
                record.producer_terminal = True
                self._maybe_release_locked(record)

    def _events(self, record: _TurnRecord) -> Iterator[ConversationEventEnvelope]:
        with self._lock:
            if record.events_claimed:
                raise RuntimeError("accepted turn events may only be consumed once")
            record.events_claimed = True
        while True:
            item = record.event_queue.get()
            if isinstance(item, _ProducerEnd):
                if item.error is not None:
                    raise item.error
                return
            yield item

    def _maybe_release_locked(self, record: _TurnRecord) -> None:
        if (
            self._active_record is record
            and record.producer_terminal
            and record.presentation_terminal
        ):
            self._active_record = None


def _request_fingerprint(request: ConversationRequest) -> bytes:
    """Retain idempotency identity without retaining screenshot-sized payloads."""
    frozen = _freeze_for_fingerprint(
        (
            request.request_id,
            request.requested_conversation_id,
            request.kind,
            request.content,
            request.source,
            request.visual_overrides,
            request.include_user_time_context,
            request.screen_attachment,
        )
    )
    return hashlib.sha256(repr(frozen).encode("utf-8")).digest()


def _freeze_for_fingerprint(value: Any) -> Any:
    if isinstance(value, Enum):
        return (
            "enum",
            value.__class__.__module__,
            value.__class__.__qualname__,
            _freeze_for_fingerprint(value.value),
        )
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return ("bytes", len(raw), hashlib.sha256(raw).digest())
    if isinstance(value, dict):
        items = [
            (_freeze_for_fingerprint(key), _freeze_for_fingerprint(item))
            for key, item in value.items()
        ]
        return ("dict", tuple(sorted(items, key=repr)))
    if isinstance(value, list):
        return ("list", tuple(_freeze_for_fingerprint(item) for item in value))
    if isinstance(value, tuple):
        return ("tuple", tuple(_freeze_for_fingerprint(item) for item in value))
    if isinstance(value, (set, frozenset)):
        items = [_freeze_for_fingerprint(item) for item in value]
        return ("set", tuple(sorted(items, key=repr)))
    if value is None or isinstance(value, (bool, int, float, str)):
        return (value.__class__.__name__, value)
    return (
        "object",
        value.__class__.__module__,
        value.__class__.__qualname__,
        repr(value),
    )
