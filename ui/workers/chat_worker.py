from __future__ import annotations

import threading
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal

from ui.models.stream import StreamToken


class ChatWorker(QThread):
    stream_event = Signal(str, dict)
    failed = Signal(str)

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
    ) -> None:
        super().__init__(parent)
        self.agent = agent
        self.message = message
        self.conversation_id = conversation_id
        self.visual_overrides = visual_overrides
        self.include_user_time_context = include_user_time_context
        self.interaction_mode = interaction_mode
        self.screen_attachment = screen_attachment
        self.token: StreamToken | None = None
        # #1 ghost-producer cancellation: the turn-level cancel flag handed to
        # stream_voice. cancel() (called from the controller's _retire_chat_worker
        # on user cancel / proactive preemption) sets it so the BACKEND producer
        # stops at its side-effect checkpoints -- isInterruptionRequested below only
        # stops THIS consumer thread, never the producer (the ghost).
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        """Signal the backend producer to stop at its side-effect checkpoints.

        Paired with requestInterruption (which only stops this consumer thread):
        together they retire both halves of a stream when it is preempted."""
        self.cancel_event.set()

    def run(self) -> None:
        try:
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
