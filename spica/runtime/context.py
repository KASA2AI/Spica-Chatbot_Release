"""Typed turn request + context (core C3a / C3c).

``TurnRequest`` (C3a) is the typed entry for one turn -- the raw fields a caller
used to hand ``ChatEngine`` positionally, frozen into one object.

``TurnContext`` (C3c) replaces the ``AgentState`` god-object blackboard. Instead
of ~25 flat fields any stage could read or write, the per-stage *outputs* are
typed sub-objects that are ``None`` until their stage runs:

    recent      <- load_recent + retrieve_long_term_memory
    screen_observation <- analyze_screen_attachment
    prompt      <- build_prompt
    answer      <- the generate phase (stream / call_llm -> parse -> visual/tts)
    error       <- any stage

Because ``ctx.answer`` is ``None`` during the prep stages, a prep stage literally
*cannot* read a field the generate stage has not written yet -- the dependency is
made explicit by the type, which is the whole point of dismantling the blackboard.

The cross-cutting accumulators (``timing`` / ``metadata`` / ``tools`` /
``response_payload``) stay flat for now; C5 turns ``timing`` into an injected
``TurnObserver``. The normalized working input (``user_input`` /
``user_local_time``) is filled by ``validate_input`` from the frozen request.

Pure: no ``agent`` import, Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True)
class GameContextRequest:
    """Typed gate input for galgame context injection (PLAN §5.4).

    Phase 1 only defines the shape; the gated ``retrieve_game_context_node`` that
    consumes it lands in Phase 3. ``mode`` selects which sections (if any) the
    stage injects; ``"none"`` (or a ``None`` request) means inject nothing.
    """

    mode: str = "none"  # active | offline | none
    game_id: str | None = None
    playthrough_id: str = "default"
    # B1: the live companion session id. Scopes the [CURRENT_LINE] pending-row read
    # to THIS play, so a crash-residue PENDING_CURRENT row left by an already-ended
    # session (dangling recovery does NOT reconcile pending rows) can never be
    # mistaken for the current on-screen line. None (manual/debug path) -> not read.
    session_id: str | None = None


# The galgame conversation-id namespace prefix. Defined HERE (typed-request layer)
# so ChatEngine's double-wrap guard can read it without importing spica.galgame.
# context_contributor._GALGAME_CONVERSATION_PREFIX (the gate's copy -- moved out
# of stages in OO migration Phase 3) and models.game_conversation_id carry the
# same literal -- deliberately NOT deduped: the gate code is untouchable (stage-2
# guardrail), see GALGAME_FINDINGS.md #9.
GALGAME_CONVERSATION_PREFIX = "galgame::"


@dataclass(frozen=True, kw_only=True)
class DomainContextRequest:
    """Generic per-domain gate input (OO migration Phase 8, 设计裁决 2).

    The typed half of the request landing point for domain #2+: a contributor's
    ``mode(request)`` gate finds its own domain's entry in
    ``TurnRequest.domain_context_requests``. Domains SUBCLASS this with their
    own typed fields (``kw_only=True`` so subclass non-default fields never hit
    the dataclass field-order trap). ``GameContextRequest`` stays galgame's
    PERMANENT dedicated slot (never migrated into this type -- pinned by the
    Phase 8 amendment; do not put non-galgame context into GameContextRequest,
    and do not put galgame context here).
    """

    domain: str
    mode: str = "none"  # active | offline | none -- same vocabulary as galgame


# Immutable domain conversation-prefix registry (Phase 8, 设计裁决 2 前缀半):
# every domain claims ONE conversation-id prefix; a system turn carries its
# domain identity AS its conversation_id (``source`` stays telemetry-only).
# MappingProxyType so no caller can mutate the registry at runtime; galgame is
# the only entry today -- co-watch etc. register here (one line) when they land.
DOMAIN_CONVERSATION_PREFIXES: Mapping[str, str] = MappingProxyType(
    {"galgame": GALGAME_CONVERSATION_PREFIX}
)


def is_domain_conversation(conversation_id: str) -> bool:
    """True iff the conversation id already lives in ANY registered domain
    namespace -- the double-wrap guard's test (a caller already addressing a
    domain conversation is taken as-is, never rewritten)."""
    cid = conversation_id or ""
    return any(cid.startswith(prefix) for prefix in DOMAIN_CONVERSATION_PREFIXES.values())


@dataclass(frozen=True)
class DomainTurnBinding:
    """Generic domain turn binding (Phase 8, 设计裁决 2) -- what a NON-galgame
    domain publishes to the ActiveDomainRouter. ``ChatEngine._request`` routes
    it down the generic lane (``domain_context_requests`` tuple); galgame keeps
    publishing ``GameTurnBinding`` (the legacy lane, permanent facade -- never
    force-fitted into this type)."""

    conversation_id: str
    context_request: DomainContextRequest


@dataclass(frozen=True)
class GameTurnBinding:
    """The per-play binding a companion controller publishes for turn auto-fill
    (Path B stage 2). When companion play is active, ``ChatEngine._request``
    applies it while building the ``TurnRequest``: ``conversation_id`` moves the
    turn into the galgame namespace (recent-memory isolation + the active gate
    trigger), ``game_context_request`` is the explicit gate input (double
    insurance -- no string parsing needed).

    Deliberately NO ``memory_conversation_id`` field: that value is "the caller's
    original conversation" (§27①), which only exists at ``_request`` time -- the
    controller cannot know it at ``start()`` time, so ``_request`` derives it from
    its own ``conversation_id`` parameter.
    """

    conversation_id: str
    game_context_request: GameContextRequest


@dataclass(frozen=True)
class TurnRequest:
    """Everything a caller specifies to drive one turn."""

    user_input: str
    conversation_id: str = "default"
    emotion_override: str | None = None
    interaction_mode: str = "chat"
    include_user_time_context: bool = True
    screen_attachment: dict[str, Any] | None = None
    tts_param_overrides: dict[str, Any] | None = None
    visual_overrides: dict[str, Any] = field(default_factory=dict)
    # -- galgame turn inputs (Phase 1: typed fields + defaults only; no consumer
    # logic until the Phase 3 gated stage). Appended at the end so positional
    # construction of the existing fields is unaffected.
    #
    # memory_conversation_id decouples long-term character-memory retrieval from
    # the turn's conversation_id (§27①): it falls back to conversation_id, so a
    # plain chat turn (leaving it None) is byte-identical to today. A galgame turn
    # sets conversation_id = galgame::<game_id>::playthrough::<id> (for recent
    # memory continuity) while keeping memory_conversation_id = "default" (so
    # Spica still reads her existing long-term memory about the user).
    memory_conversation_id: str | None = None
    command_intent: str | None = None  # canonical CommandIntent enum lands with Phase 4 commands.py
    game_context_request: GameContextRequest | None = None
    # Phase 8 (设计裁决 2): the GENERIC domain gate inputs -- filled by the
    # DomainTurnBinding lane in ChatEngine._request; () (the default) keeps
    # every existing turn byte-identical. galgame stays on game_context_request
    # above (permanent facade), never in this tuple.
    domain_context_requests: tuple[DomainContextRequest, ...] = ()
    # #1 ghost-producer cancellation: a turn-level cancel flag the UI (ChatWorker)
    # sets when its stream is retired -- user cancel OR proactive/P5 preemption, both
    # via stop_current. The producer thread checks it at its three side-effect points
    # (tool execution / memory write / LLM delta loop) and short-circuits, so a
    # cancelled turn cannot ghost-execute tools, write ghost memory, or burn tokens
    # after the consumer stopped reading. compare=False so it never affects request
    # equality; None / unset -> is_turn_cancelled is False -> every checkpoint stays
    # byte-identical to before (the deadline guarantee).
    cancelled: threading.Event | None = field(default=None, compare=False)

    @property
    def effective_memory_conversation_id(self) -> str:
        """The conversation_id to namespace long-term *character* memory by.

        The single source of truth for the §27① fallback. No production caller
        reads this in Phase 1 (the Phase 3 gated stage / retrieve node will);
        defined now so the fallback semantics are typed and unit-tested.
        """
        return self.memory_conversation_id or self.conversation_id


def is_turn_cancelled(request: TurnRequest) -> bool:
    """True iff this turn was cancelled (its cancel Event exists and is set).

    The single predicate shared by the producer's three side-effect checkpoints
    (tool_round._run_tool_calls and orchestrator's save_stream_memory / delta loop).
    Defensive ``getattr`` + the None/unset short-circuit mean a request without a
    live cancel Event -- every non-UI caller: run_voice / sync_chain / all tests --
    returns False, so those paths stay byte-identical (the #1 deadline guarantee).
    """
    ev = getattr(request, "cancelled", None)
    return ev is not None and ev.is_set()


@dataclass
class RetrievedContext:
    """Output of the load-recent + retrieve-long-term-memory stages."""

    recent_context: list[dict[str, str]] = field(default_factory=list)
    long_term_memories: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PromptBundle:
    """Output of the build-prompt stage: the assembled model input."""

    prompt_input: str | list[Any] | dict[str, Any] | None = None


@dataclass
class StreamedAnswer:
    """Output of the generate phase: the model reply and everything derived.

    ``visual`` / ``tts_result`` are only filled by the synchronous node path
    (run_voice_pipeline); the streaming path produces visual/audio per play unit,
    not on the turn answer. ``tts_result`` is typed ``Any`` to keep this module
    free of an ``agent_tools`` import.

    ``response_id`` is NOT here: it is write-only telemetry the LLM *port* sets on
    the turn object (like ``timing``), so it lives flat on ``TurnContext`` and the
    port does not need to navigate this sub-object. (C5 moves both to the observer.)
    """

    raw_model_output: str | None = None
    parsed_reply: dict[str, str] | None = None
    answer: str | None = None
    emotion: str | None = None
    visual: dict[str, Any] | None = None
    tts_result: Any = None


@dataclass(frozen=True)
class TurnError:
    """A turn failure. Serialized to the legacy dict only at the two boundaries
    below (``turn_error_to_legacy_dict``); stages set ``ctx.error`` directly."""

    code: str
    message: str


def turn_error_to_legacy_dict(error: TurnError) -> dict[str, str]:
    """The ONE place a ``TurnError`` becomes the legacy ``{"code", "message"}``.

    Exactly two boundaries serialize an error: ``response_payload["error"]`` (the
    full dict -- here) and the error ``RuntimeEvent`` (message only, read as
    ``error.message``). No stage hand-writes a ``{"code": ..., "message": ...}``.
    """
    return {"code": error.code, "message": error.message}


@dataclass
class TurnContext:
    """Per-turn working context: the frozen request + typed per-stage outputs.

    Replaces ``AgentState``. Sub-objects are ``None`` until their stage runs so a
    stage cannot depend on a field a later stage writes.
    """

    request: TurnRequest
    # Normalized working input (filled by validate_input from the request).
    user_input: str = ""
    user_local_time: dict[str, str] = field(default_factory=dict)
    # Per-stage outputs -- None before their stage.
    recent: RetrievedContext | None = None
    screen_observation: dict[str, Any] | None = None
    prompt: PromptBundle | None = None
    answer: StreamedAnswer | None = None
    error: TurnError | None = None
    # Write-only telemetry the LLM port sets on the turn object (read back via
    # ``or``), so it must default to None and live flat -- same lifecycle as timing.
    response_id: str | None = None
    # Cross-cutting accumulators (C5 turns timing into an injected TurnObserver).
    timing: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    tools: list[dict[str, Any]] = field(default_factory=list)
    response_payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Default the working input to the request's raw input; validate_input
        # normalizes it in place. Keeps ctx.user_input from ever being the wrong
        # "" if something reads it before validate (nothing does today).
        if not self.user_input:
            self.user_input = self.request.user_input
