from __future__ import annotations

import time
import uuid
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal

from spica.ports.conversation import (
    AdmissionDecision,
    ConversationCancellation,
    ConversationRequest,
    ConversationTurnKind,
)
from ui.models.stream import StreamToken


class ChatWorker(QThread):
    stream_event = Signal(str, dict)
    failed = Signal(str)
    admission_rejected = Signal(str)
    turn_admitted = Signal(str)

    def __init__(
        self,
        agent: Any,
        message: str,
        conversation_id: str,
        visual_overrides: dict[str, Any],
        include_user_time_context: bool,
        interaction_mode: str,
        parent: QObject | None = None,
        screen_attachment: dict[str, Any] | None = None,
        conversation_coordinator: Any | None = None,
        request_id: str | None = None,
        system_directive: str | None = None,
        system_source: str = "",
    ) -> None:
        super().__init__(parent)
        self.agent = agent
        self.message = message
        self.conversation_id = conversation_id
        self.visual_overrides = visual_overrides
        self.include_user_time_context = include_user_time_context
        self.interaction_mode = interaction_mode
        self.screen_attachment = screen_attachment
        self.conversation_coordinator = conversation_coordinator
        self.request_id = request_id
        self.system_directive = system_directive
        self.system_source = system_source
        self.token: StreamToken | None = None
        self.accepted_turn: Any | None = None
        self.cancellation = ConversationCancellation()
        # #1 ghost-producer cancellation: the turn-level cancel flag handed to
        # stream_voice. cancel() (called from the controller's _retire_chat_worker
        # on user cancel / proactive preemption) sets it so the BACKEND producer
        # stops at its side-effect checkpoints -- isInterruptionRequested below only
        # stops THIS consumer thread, never the producer (the ghost).
        self.cancel_event = self.cancellation.event

    def cancel(self) -> None:
        """Signal the backend producer to stop at its side-effect checkpoints.

        Paired with requestInterruption (which only stops this consumer thread):
        together they retire both halves of a stream when it is preempted."""
        # This never waits for the worker's submit call.  If cancellation wins
        # before the Coordinator's internal linearization point, submit returns
        # CANCELLED without an active record or producer.  If admission wins,
        # this is the exact event already handed to that producer.
        self.cancellation.cancel("desktop_stop")

    def run(self) -> None:
        try:
            if self.conversation_coordinator is not None:
                self._run_coordinated()
                return
            for event in self.agent.stream_voice(
                self.message,
                conversation_id=self.conversation_id,
                visual_overrides=self.visual_overrides,
                screen_attachment=self.screen_attachment,
                include_user_time_context=self.include_user_time_context,
                interaction_mode=self.interaction_mode,
                cancelled=self.cancel_event,
            ):
                if self.isInterruptionRequested():
                    return
                if not isinstance(event, dict):
                    continue
                event_name = str(event.get("event") or "message")
                data = event.get("data") if isinstance(event.get("data"), dict) else {}
                self.stream_event.emit(event_name, data)
        except Exception as exc:
            self.failed.emit(str(exc))

    def _run_coordinated(self) -> None:
        kind = (
            ConversationTurnKind.SYSTEM
            if self.interaction_mode == "system"
            else ConversationTurnKind.USER
        )
        request = ConversationRequest(
            request_id=self.request_id or uuid.uuid4().hex,
            requested_conversation_id=self.conversation_id,
            kind=kind,
            content=(
                self.system_directive
                if kind is ConversationTurnKind.SYSTEM and self.system_directive is not None
                else self.message
            ),
            source=self.system_source if kind is ConversationTurnKind.SYSTEM else "",
            visual_overrides=dict(self.visual_overrides),
            include_user_time_context=self.include_user_time_context,
            screen_attachment=self.screen_attachment,
        )
        # A replacement user turn preserves the existing desktop preemption
        # experience: stop_current cancels the old turn, then this worker waits
        # until that producer has really terminated. submit() remains the final
        # atomic admission. Disposable SYSTEM/proactive turns never wait here.
        if kind is ConversationTurnKind.USER:
            while self.conversation_coordinator.is_busy():
                if self.isInterruptionRequested():
                    return
                time.sleep(0.01)
        if self.cancel_event.is_set() or self.isInterruptionRequested():
            return
        admission = self.conversation_coordinator.submit(
            request,
            cancellation=self.cancellation,
        )
        if admission.decision is AdmissionDecision.ACCEPTED:
            self.accepted_turn = admission.accepted_turn
        if admission.decision is AdmissionDecision.CANCELLED:
            return
        if admission.decision is not AdmissionDecision.ACCEPTED:
            self.admission_rejected.emit(admission.decision.value)
            return
        self.turn_admitted.emit(self.accepted_turn.turn_id)
        for envelope in self.accepted_turn.events():
            if self.isInterruptionRequested():
                continue
            legacy = envelope.event.to_legacy_dict()
            event_name = str(legacy.get("event") or "message")
            data = legacy.get("data") if isinstance(legacy.get("data"), dict) else {}
            self.stream_event.emit(event_name, data)
