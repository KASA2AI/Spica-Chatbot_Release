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
from agent.state import AgentServices, AgentState
from spica.adapters.memory.sqlite import scoped_conversation_id
from spica.config.schema import AppConfig
from spica.runtime.context import TurnRequest
from spica.runtime.deps import TurnDeps
from spica.runtime.exec_strategy import Inline
from spica.runtime.fold import fold_events
from spica.runtime.turn import run_turn


class ChatEngine:
    def __init__(self, services: AgentServices, config: AppConfig) -> None:
        self.services = services
        self.config = config
        # Typed deps (C3a): the runtime uses deps.tools; ports/config are wired in
        # by later stages. Built from the host-assembled (port-resolved) services.
        self.deps = TurnDeps.from_services(services, config)
        self.interlocutor_name = str(services.config.get("interlocutor_name") or DEFAULT_INTERLOCUTOR_NAME)
        self.model = services.config.get("model")

    # -- driving --------------------------------------------------------------
    def _request(
        self,
        user_input: str,
        conversation_id: str,
        emotion_override: str | None,
        tts_param_overrides: dict[str, Any] | None,
        visual_overrides: dict[str, Any] | None,
        include_user_time_context: bool,
        interaction_mode: str,
        screen_attachment: dict[str, Any] | None,
    ) -> TurnRequest:
        return TurnRequest(
            user_input=user_input or "",
            conversation_id=conversation_id or "default",
            emotion_override=emotion_override,
            interaction_mode=interaction_mode,
            include_user_time_context=include_user_time_context,
            screen_attachment=screen_attachment,
            tts_param_overrides=tts_param_overrides,
            visual_overrides=visual_overrides or {},
        )

    @staticmethod
    def _state_from_request(req: TurnRequest) -> AgentState:
        # Bridge the typed request to the (still AgentState-based) runtime; C3c
        # dismantles AgentState and the runtime takes TurnRequest/TurnContext.
        return AgentState(
            conversation_id=req.conversation_id,
            user_input=req.user_input,
            include_user_time_context=req.include_user_time_context,
            interaction_mode=req.interaction_mode,
            emotion_override=req.emotion_override,
            tts_param_overrides=req.tts_param_overrides,
            visual_overrides=dict(req.visual_overrides),
            screen_attachment=req.screen_attachment,
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
        req = self._request(
            user_input, conversation_id, emotion_override, tts_param_overrides,
            visual_overrides, include_user_time_context, interaction_mode, screen_attachment,
        )
        # Sync path (C2) = drive run_turn with Inline (no thread pools), collect
        # the typed events, and fold them into the response payload.
        events = list(run_turn(self._state_from_request(req), self.services,
                               exec_strategy=Inline(), deps=self.deps))
        return fold_events(events, conversation_id=req.conversation_id)

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
        """Drive a turn, yielding typed ``RuntimeEvent``s via the run_turn entry."""
        req = self._request(
            user_input, conversation_id, emotion_override, tts_param_overrides,
            visual_overrides, include_user_time_context, interaction_mode, screen_attachment,
        )
        yield from run_turn(self._state_from_request(req), self.services, deps=self.deps)

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

    def _ltm_conversation_id(self, conversation_id: str) -> str:
        # Long-term store is namespaced by character (Phase 7) so it matches
        # commit_turn / retrieve; short-term recent_memory stays on the bare
        # conversation_id. "::" is defined once, in scoped_conversation_id.
        return scoped_conversation_id(
            str(self.services.config.get("character_id") or "spica"),
            conversation_id,
        )

    def clear_memory(self, conversation_id: str = "default", clear_long_term: bool = False) -> dict[str, Any]:
        self.services.recent_memory.clear(conversation_id)
        cleared = {"recent_memory": True, "long_term_memory": False}
        if clear_long_term:
            self.services.memory_store.clear_memories(self._ltm_conversation_id(conversation_id))
            cleared["long_term_memory"] = True
        return {"ok": True, "conversation_id": conversation_id, "cleared": cleared}

    def list_memory(self, conversation_id: str = "default", limit: int = 50) -> list[dict[str, Any]]:
        return self.services.memory_store.list_memories(self._ltm_conversation_id(conversation_id), limit=limit)

    def remember(
        self,
        content: str,
        conversation_id: str = "default",
        scope: str = "user",
        importance: float = 0.8,
    ) -> int:
        return self.services.memory_store.upsert_memory(
            conversation_id=self._ltm_conversation_id(conversation_id),
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
