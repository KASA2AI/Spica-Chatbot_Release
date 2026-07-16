"""Compatibility adapter from ConversationCoordinator to the existing ChatEngine."""

from __future__ import annotations

import threading
from typing import Any, Iterator

from spica.core.events import RuntimeEvent, event_from_legacy
from spica.ports.conversation import ConversationRequest, ConversationTurnKind


class LegacyRunTurnAdapter:
    def __init__(self, chat_engine: Any) -> None:
        self._chat_engine = chat_engine

    def stream(
        self,
        request: ConversationRequest,
        *,
        cancelled: threading.Event,
    ) -> Iterator[RuntimeEvent]:
        if request.kind is ConversationTurnKind.USER:
            yield from self._chat_engine.stream_voice_runtime(
                request.content,
                conversation_id=request.requested_conversation_id,
                visual_overrides=dict(request.visual_overrides),
                include_user_time_context=request.include_user_time_context,
                interaction_mode="chat",
                screen_attachment=request.screen_attachment,
                cancelled=cancelled,
            )
            return
        if request.kind is ConversationTurnKind.SYSTEM:
            for event in self._chat_engine.stream_system_turn(
                request.content,
                conversation_id=request.requested_conversation_id,
                source=request.source,
                cancelled=cancelled,
                visual_overrides=dict(request.visual_overrides),
                include_user_time_context=request.include_user_time_context,
            ):
                yield event_from_legacy(event)
            return
        raise ValueError(f"unsupported conversation turn kind: {request.kind.value}")
