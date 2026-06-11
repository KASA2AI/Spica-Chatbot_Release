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

from typing import Any, Callable

from spica.conversation.character_loader import (
    DEFAULT_INTERLOCUTOR_NAME,
    build_character_profile,
    normalize_interlocutor_name,
)
from spica.conversation.reply_parser import guess_emotion, normalize_emotion, parse_model_reply
from spica.core.proactive import compose_system_directive_message
from spica.runtime.services import AgentServices
from spica.adapters.memory.sqlite import scoped_conversation_id
from spica.config.schema import AppConfig
from spica.runtime.context import (
    GALGAME_CONVERSATION_PREFIX,
    GameTurnBinding,
    TurnContext,
    TurnRequest,
)
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
        # Path B stage 2: when companion play is active, the host-injected provider
        # returns a GameTurnBinding and _request auto-fills the galgame turn fields.
        # None (never set / not playing) -> _request builds the exact same
        # TurnRequest as before, byte for byte.
        self._game_binding_provider: Callable[[], GameTurnBinding | None] | None = None

    # -- driving --------------------------------------------------------------
    def set_game_binding_provider(
        self, provider: Callable[[], GameTurnBinding | None] | None
    ) -> None:
        """Inject the companion-play binding provider (Path B stage 2).

        The host wires this to its companion controller's published snapshot.
        ``None`` (or a provider returning ``None``) keeps every turn a plain chat
        turn -- the construction path is then identical to before.
        """
        self._game_binding_provider = provider

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
        binding = self._game_binding_provider() if self._game_binding_provider else None
        # Double-wrap guard: a caller already addressing a galgame conversation
        # (manual/debug path) is taken as-is, never rewritten.
        if binding is not None and not (conversation_id or "").startswith(GALGAME_CONVERSATION_PREFIX):
            # Companion play is active: the turn moves into the galgame conversation
            # (recent-memory isolation + the active gate) while memory_conversation_id
            # keeps the caller's ORIGINAL conversation, so long-term character memory
            # stays continuous (§27①).
            return TurnRequest(
                user_input=user_input or "",
                conversation_id=binding.conversation_id,
                emotion_override=emotion_override,
                interaction_mode=interaction_mode,
                include_user_time_context=include_user_time_context,
                screen_attachment=screen_attachment,
                tts_param_overrides=tts_param_overrides,
                visual_overrides=visual_overrides or {},
                memory_conversation_id=conversation_id or "default",
                game_context_request=binding.game_context_request,
            )
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
    def _context_from_request(req: TurnRequest) -> TurnContext:
        # The runtime drives a TurnContext (C3c): it just wraps the frozen
        # request; validate_input derives the normalized working fields from it.
        return TurnContext(request=req)

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
        events = list(run_turn(self._context_from_request(req), self.services,
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
        yield from run_turn(self._context_from_request(req), self.services, deps=self.deps)

    def stream_system_turn(
        self,
        directive: str,
        *,
        conversation_id: str | None = None,
        source: str = "",
    ):
        """P3: a SYSTEM-initiated turn (proactive speech). Mode-agnostic: the
        caller (song report today, galgame tease / video commentary later) only
        authors the directive text. Rides the ONE dialogue path -- the framed
        directive goes through stream_voice with interaction_mode="system"
        (typed marker; tool supply is hard-off on that mode, see tool_round) --
        run_turn / orchestrator / stages never fork."""
        del source  # telemetry label for callers/logs; no behavioural branch
        yield from self.stream_voice(
            compose_system_directive_message(directive),
            conversation_id=conversation_id or "default",
            interaction_mode="system",
        )

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
        # Keep the typed config in sync so deps.config.character (and memory
        # namespacing via deps) reflects the rename (C3b/C4). deps.config is this
        # same AppConfig object, so the in-place update is visible downstream --
        # the rebuilt profile too, which C4's build_prompt stage reads off deps.
        self.config.character.interlocutor_name = self.interlocutor_name
        self.config.character.character_profile = profile
        return self.interlocutor_name

    def set_visual_tool(self, visual_tool: Any | None) -> None:
        self.services.visual_tool = visual_tool

    def _ltm_conversation_id(self, conversation_id: str) -> str:
        # Long-term store is namespaced by character (Phase 7) so it matches
        # commit_turn / retrieve. "::" is defined once, in scoped_conversation_id.
        #
        # TODO(Phase 7 多角色): short-term recent_memory still uses the BARE
        # conversation_id -- it is NOT namespaced by character_id. Switching
        # characters within one conversation would cross-contaminate the short-term
        # context (recent turns of character A leaking into character B). When Phase 7
        # wires runtime character switching, recent_memory must key on the same
        # character namespace as the long-term store (scoped_conversation_id).
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
