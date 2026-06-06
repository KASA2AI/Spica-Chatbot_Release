"""ChatEngine: the conversation driver behind the Host's conversation surface (Phase 6B).

Takes over the run/stream *driving* role from ``SimpleAgent`` (which becomes a
compatibility shell that still assembles services and owns character / memory
management). ChatEngine drives a turn by building ``AgentState`` and invoking the
existing pipeline; unknown attributes are forwarded to the wrapped agent so the
UI keeps calling ``interlocutor_name`` / ``set_interlocutor_name`` / ``model``
etc. unchanged.

Pipeline internals are NOT decomposed here (that is Phase 6C). Streamed dicts are
adapted to ``RuntimeEvent`` at the boundary (Phase 6A), exactly as SimpleAgent did.

INVARIANT (CLAUDE.md #1): Qt-free.
"""

from __future__ import annotations

from typing import Any

from agent.runtime import run_voice_pipeline
from agent.state import AgentState
from agent.streaming_pipeline import stream_voice_events
from spica.core.events import event_from_legacy


class ChatEngine:
    def __init__(self, agent: Any, services: Any | None = None) -> None:
        self._agent = agent
        self.services = services if services is not None else agent.services

    def _build_state(
        self,
        user_input: str,
        conversation_id: str,
        emotion_override: str | None,
        tts_param_overrides: dict[str, Any] | None,
        visual_overrides: dict[str, Any] | None,
        include_user_time_context: bool,
        interaction_mode: str,
        screen_attachment: dict[str, Any] | None,
    ) -> AgentState:
        return AgentState(
            conversation_id=conversation_id or "default",
            user_input=user_input or "",
            include_user_time_context=include_user_time_context,
            interaction_mode=interaction_mode,
            emotion_override=emotion_override,
            tts_param_overrides=tts_param_overrides,
            visual_overrides=visual_overrides or {},
            screen_attachment=screen_attachment,
        )

    def run_voice(
        self,
        user_input: str,
        conversation_id: str = "default",
        emotion_override: str | None = None,
        tts_param_overrides: dict[str, Any] | None = None,
        visual_overrides: dict[str, Any] | None = None,
        include_user_time_context: bool = True,
        interaction_mode: str = "chat",
        screen_attachment: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self._build_state(
            user_input, conversation_id, emotion_override, tts_param_overrides,
            visual_overrides, include_user_time_context, interaction_mode, screen_attachment,
        )
        state = run_voice_pipeline(state, self.services)
        return state.response_payload

    def run(self, user_input: str, conversation_id: str = "default") -> str:
        return str(self.run_voice(user_input, conversation_id=conversation_id).get("answer") or "")

    def stream_voice_runtime(
        self,
        user_input: str,
        conversation_id: str = "default",
        emotion_override: str | None = None,
        tts_param_overrides: dict[str, Any] | None = None,
        visual_overrides: dict[str, Any] | None = None,
        include_user_time_context: bool = True,
        interaction_mode: str = "chat",
        screen_attachment: dict[str, Any] | None = None,
    ):
        """Drive a turn, yielding typed ``RuntimeEvent``s (Phase 6A boundary)."""
        state = self._build_state(
            user_input, conversation_id, emotion_override, tts_param_overrides,
            visual_overrides, include_user_time_context, interaction_mode, screen_attachment,
        )
        for event in stream_voice_events(state, self.services):
            yield event_from_legacy(event)

    def stream_voice(
        self,
        user_input: str,
        conversation_id: str = "default",
        emotion_override: str | None = None,
        tts_param_overrides: dict[str, Any] | None = None,
        visual_overrides: dict[str, Any] | None = None,
        include_user_time_context: bool = True,
        interaction_mode: str = "chat",
        screen_attachment: dict[str, Any] | None = None,
    ):
        """Drive a turn, yielding legacy dict events for the current UI."""
        for event in self.stream_voice_runtime(
            user_input,
            conversation_id=conversation_id,
            emotion_override=emotion_override,
            tts_param_overrides=tts_param_overrides,
            visual_overrides=visual_overrides,
            include_user_time_context=include_user_time_context,
            interaction_mode=interaction_mode,
            screen_attachment=screen_attachment,
        ):
            yield event.to_legacy_dict()

    def __getattr__(self, name: str) -> Any:
        # Only invoked when normal lookup fails. Forward character / memory /
        # misc methods (interlocutor_name, set_interlocutor_name, model,
        # clear_memory, ...) to the wrapped SimpleAgent shell.
        agent = self.__dict__.get("_agent")
        if agent is None:
            raise AttributeError(name)
        return getattr(agent, name)
