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

from dataclasses import dataclass, field
from typing import Any


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
