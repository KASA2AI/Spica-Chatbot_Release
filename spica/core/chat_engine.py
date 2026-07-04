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

import logging
import threading
from typing import Any, Callable

from spica.conversation.character_loader import (
    DEFAULT_INTERLOCUTOR_NAME,
    build_character_profile,
    normalize_interlocutor_name,
)
from spica.conversation.reply_parser import guess_emotion, normalize_emotion, parse_model_reply
from spica.core.proactive import compose_system_directive_message
from spica.runtime.services import AgentServices
from spica.config.schema import AppConfig
from spica.runtime.context import (
    DomainTurnBinding,
    GameTurnBinding,
    TurnContext,
    TurnRequest,
    is_domain_conversation,
)
from spica.runtime.deps import TurnDeps
from spica.runtime.exec_strategy import Inline
from spica.runtime.fold import fold_events
from spica.runtime.scope import MemoryScopeStrategy
from spica.runtime.turn import run_turn

logger = logging.getLogger(__name__)


class ChatEngine:
    def __init__(self, services: AgentServices, config: AppConfig) -> None:
        self.services = services
        self.config = config
        # Typed deps (C3a): the runtime uses deps.tools; ports/config are wired in
        # by later stages. Built from the host-assembled (port-resolved) services.
        self.deps = TurnDeps.from_services(services, config)
        # Phase 2: ONE strategy instance over this same AppConfig object -- its
        # methods live-read config.character, so set_interlocutor_name's in-place
        # rename is visible to every later scope resolution.
        self._memory_scope = MemoryScopeStrategy(config)
        self.interlocutor_name = str(services.config.get("interlocutor_name") or DEFAULT_INTERLOCUTOR_NAME)
        self.model = services.config.get("model")
        # Path B stage 2 / Phase 8-c1: when a domain is live, the host-injected
        # provider (the ActiveDomainRouter's ``current`` -- the ONE injector, D6)
        # returns its binding: GameTurnBinding (galgame legacy lane) or
        # DomainTurnBinding (generic lane). None (never set / nothing live) ->
        # _request builds the exact same TurnRequest as before, byte for byte.
        self._game_binding_provider: Callable[
            [], GameTurnBinding | DomainTurnBinding | None
        ] | None = None

    # -- driving --------------------------------------------------------------
    def set_game_binding_provider(
        self, provider: Callable[[], GameTurnBinding | DomainTurnBinding | None] | None
    ) -> None:
        """Inject the domain turn-binding provider (Phase 8-c1).

        The host wires this to ``ActiveDomainRouter.current`` -- the ONE
        injector (D6). ``None`` (or a provider returning ``None``) keeps every
        turn a plain chat turn, byte-identical to before. A ``GameTurnBinding``
        rides the galgame legacy lane (``game_context_request``); a
        ``DomainTurnBinding`` rides the generic lane
        (``domain_context_requests``).
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
        cancelled: threading.Event | None = None,
    ) -> TurnRequest:
        binding = self._game_binding_provider() if self._game_binding_provider else None
        # Double-wrap guard (Phase 8-c1: registry-based): a caller already
        # addressing ANY registered domain conversation (manual/debug path) is
        # taken as-is, never rewritten. The registry holds exactly galgame
        # today, so this is byte-identical to the old single-prefix test.
        if binding is not None and not is_domain_conversation(conversation_id or ""):
            if isinstance(binding, GameTurnBinding):
                # galgame legacy lane (permanent facade): the turn moves into the
                # galgame conversation (recent-memory isolation + the active gate)
                # while memory_conversation_id keeps the caller's ORIGINAL
                # conversation, so long-term character memory stays continuous (§27①).
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
                    cancelled=cancelled,
                )
            if isinstance(binding, DomainTurnBinding):
                # Generic lane (Phase 8, 设计裁决 2): same conversation move +
                # §27① memory continuity, but the gate input rides the generic
                # tuple -- game_context_request stays None (galgame-only slot).
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
                    domain_context_requests=(binding.context_request,),
                    cancelled=cancelled,
                )
            # Unknown binding shape: fail-open to plain chat (a wiring bug must
            # not crash or misroute a user's turn into a domain namespace) --
            # but LOUDLY (review NEW-4): silence would hide the wiring bug.
            logger.warning(
                "unknown domain binding shape %s -- falling open to plain chat",
                type(binding).__name__,
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
            cancelled=cancelled,
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
        cancelled: threading.Event | None = None,
    ):
        """Drive a turn, yielding typed ``RuntimeEvent``s via the run_turn entry."""
        req = self._request(
            user_input, conversation_id, emotion_override, tts_param_overrides,
            visual_overrides, include_user_time_context, interaction_mode, screen_attachment,
            cancelled=cancelled,
        )
        yield from run_turn(self._context_from_request(req), self.services, deps=self.deps)

    def stream_system_turn(
        self,
        directive: str,
        *,
        conversation_id: str | None = None,
        source: str = "",
        cancelled: threading.Event | None = None,
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
            cancelled=cancelled,
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
        cancelled: threading.Event | None = None,
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
            cancelled=cancelled,
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
        # Long-term store is namespaced by character so it matches commit_turn /
        # retrieve. Phase 2: derivation lives in MemoryScopeStrategy (live-read of
        # the typed config -- agent_assembly keeps it in sync with the legacy dict
        # this used to read); "::" is still defined once, in scoped_conversation_id.
        # The old TODO here is RESOLVED: recent memory now keys on the same
        # character namespace (stages read / memory_commit write / clear below).
        return self._memory_scope.clear_targets(conversation_id)[1]

    def clear_memory(self, conversation_id: str = "default", clear_long_term: bool = False) -> dict[str, Any]:
        recent_key, ltm_conversation_id = self._memory_scope.clear_targets(conversation_id)
        # Phase 2: recent clear targets the character-scoped bucket, symmetric with
        # the scoped write key (previously it cleared the bare conversation_id
        # while the long-term side below was already scoped -- the asymmetry).
        self.services.recent_memory.clear(recent_key)
        cleared = {"recent_memory": True, "long_term_memory": False}
        if clear_long_term:
            self.services.memory_store.clear_memories(ltm_conversation_id)
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
