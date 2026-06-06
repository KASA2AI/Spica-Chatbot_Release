"""ChatEngine: the conversation core behind the Host's conversation surface.

Phase 6B introduced ChatEngine as the conversation *driver*. Phase 6D dissolves
``SimpleAgent`` into it: ChatEngine now owns both driving (run / stream) and the
character / memory management the UI calls (``set_interlocutor_name``,
``clear_memory``, ...), working directly off ``AgentServices``. Backend assembly
lives in ``spica/host/agent_assembly.py``; pipeline internals live in
``spica/runtime``.

INVARIANT (CLAUDE.md #1): Qt-free.
"""

from __future__ import annotations

from typing import Any

from agent.character_loader import (
    DEFAULT_INTERLOCUTOR_NAME,
    build_character_profile,
    normalize_interlocutor_name,
)
from agent.reply_parser import guess_emotion, normalize_emotion, parse_model_reply
from agent.runtime import run_voice_pipeline
from agent.state import AgentServices, AgentState
from spica.config.schema import AppConfig
from spica.core.events import event_from_legacy
from spica.runtime.orchestrator import stream_voice_events


class ChatEngine:
    def __init__(self, services: AgentServices, config: AppConfig) -> None:
        self.services = services
        self.config = config
        self.interlocutor_name = str(services.config.get("interlocutor_name") or DEFAULT_INTERLOCUTOR_NAME)
        self.model = services.config.get("model")

    # -- driving --------------------------------------------------------------
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

    # -- character / memory management (dissolved from SimpleAgent, Phase 6D) --
    def set_interlocutor_name(self, name: str) -> str:
        self.interlocutor_name = normalize_interlocutor_name(name)
        profile = build_character_profile(
            self.config.character.profile_override,
            self.config.character.skill_dir,
            self.interlocutor_name,
        )
        self.services.config["interlocutor_name"] = self.interlocutor_name
        self.services.config["character_profile"] = profile
        return self.interlocutor_name

    def set_visual_tool(self, visual_tool: Any | None) -> None:
        self.services.visual_tool = visual_tool

    def clear_memory(self, conversation_id: str = "default", clear_long_term: bool = False) -> dict[str, Any]:
        self.services.recent_memory.clear(conversation_id)
        cleared = {"recent_memory": True, "long_term_memory": False}
        if clear_long_term:
            self.services.memory_store.clear_memories(conversation_id)
            cleared["long_term_memory"] = True
        return {"ok": True, "conversation_id": conversation_id, "cleared": cleared}

    def list_memory(self, conversation_id: str = "default", limit: int = 50) -> list[dict[str, Any]]:
        return self.services.memory_store.list_memories(conversation_id, limit=limit)

    def remember(
        self,
        content: str,
        conversation_id: str = "default",
        scope: str = "user",
        importance: float = 0.8,
    ) -> int:
        return self.services.memory_store.upsert_memory(
            conversation_id=conversation_id,
            scope=scope,
            content=content,
            importance=importance,
            source="manual",
        )

    def forget_memory(self, memory_id: int) -> None:
        self.services.memory_store.delete_memory(memory_id)

    def parse_model_reply(self, output_text: str) -> dict[str, str]:
        return parse_model_reply(output_text)

    def normalize_emotion(self, emotion: str | None) -> str:
        return normalize_emotion(emotion)

    def guess_emotion(self, text: str) -> str:
        return guess_emotion(text)
