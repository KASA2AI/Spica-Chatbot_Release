"""Model port v2 -- the text family (OO migration Phase 6a).

``TextModel`` is the ADAPTER-side contract: ``complete``/``stream`` take the
prompt and the model name, and the request dict is assembled INSIDE the
adapter (the depth v1 lacks -- ``LLMPort.iter_response_text`` makes callers
build the request themselves). ``BoundModel`` pairs an adapter with a model
name so turn-external text consumers (summarizer / reaction judge) hold ONE
object and never resolve endpoints or model names per call.

Since Phase 7 the PRODUCTION chain runs entirely on this v2 surface: the
orchestrator's final stream and tool_round's probe family go through
``deps.model.stream/probe/probe_stream``. v1 ``LLMPort``
(``spica/ports/llm.py``) stays frozen for the sync museum chain ONLY
(``sync_chain.py`` / ``call_llm_node``) plus the adapter's own v1 method
tests. ``spica/galgame`` + ``spica/host`` must not grow new v1 consumers
(``tests/test_no_new_v1_llm_consumers.py``); the flipped runtime files must
not touch the v1 surface or its carriers at all
(``tests/test_no_v1_llm_in_runtime.py``).

BoundModel deliberately has NO ``complete_text`` compatibility shim: the v2
path calls ``adapter.complete`` / ``adapter.stream`` only (structurally pinned
by ``tests/test_text_model_contract.py``). A v1-shaped fake must be updated to
the adapter-side v2 shape, never papered over here.

Qt-free (铁律 #1); pure types, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterator, Protocol


class TextModel(Protocol):
    """Adapter-side text capability (v2). Implemented by OpenAICompatibleAdapter."""

    def complete(self, prompt: str, *, model: str) -> str:
        """One-shot completion; returns the assistant text."""
        ...

    def stream(self, prompt: str, *, model: str, state: Any) -> Iterator[str]:
        """Stream assistant text deltas; request assembly and endpoint
        fallbacks live inside the adapter."""
        ...


@dataclass(frozen=True)
class ToolProbeResult:
    """One non-streaming tool probe's outcome (ToolCallingModel v2, Phase 7-c2).

    ``calls`` are normalized ``{"name", "arguments"}`` dicts (arguments = raw
    JSON string). ``usage`` carries the provider usage object ONLY on the
    Responses family (the runtime records it via its observer, today's
    semantics); the chat family records usage inside the adapter (``state``)
    and leaves ``usage=None`` -- never both (the no-double-accounting ruling).
    """

    calls: list[dict[str, str]]
    text: str
    response_id: str | None = None
    usage: Any = None


class ToolProbeStream:
    """A LAZY streaming tool probe handle (chat family).

    Contract (Phase 7 ruling, pinned by tests/test_tool_calling_model_contract):
    - constructing this object performs NO client I/O; the underlying stream is
      created only when ``deltas`` is first iterated;
    - ``calls`` is readable ONLY after ``deltas`` was NORMALLY exhausted --
      reading it early, after a cancel abandoned the iterator, or after a
      mid-stream exception raises RuntimeError (loud, never undefined data);
    - a probe cancelled mid-stream therefore never yields STREAM_RESET and
      never executes tools (the caller structurally cannot reach ``calls``).

    ``open_stream`` receives the calls sink and returns the delta iterator
    (adapter side: a generator over ``chat.completions.create(stream=True)``,
    itself lazy until first ``next()``).
    """

    def __init__(self, open_stream: Callable[[list[dict[str, str]]], Iterator[str]]) -> None:
        self._sink: list[dict[str, str]] = []
        self._exhausted = False
        self._deltas = self._consume(open_stream)

    def _consume(self, open_stream: Callable[[list[dict[str, str]]], Iterator[str]]) -> Iterator[str]:
        for delta in open_stream(self._sink):
            yield delta
        # Reached ONLY on normal exhaustion -- an exception or an abandoned
        # generator never sets it, so .calls stays locked.
        self._exhausted = True

    @property
    def deltas(self) -> Iterator[str]:
        return self._deltas

    @property
    def calls(self) -> list[dict[str, str]]:
        if not self._exhausted:
            raise RuntimeError(
                "ToolProbeStream.calls read before deltas were normally exhausted "
                "(early read / cancelled / errored stream) -- the contract forbids it."
            )
        return list(self._sink)


class ToolCallingModel(Protocol):
    """Adapter-side tool-probe capability (v2). The endpoint family choice
    (Responses vs Chat Completions) is INTERNAL: ``probe_stream`` returning
    ``None`` is the family signal (this provider does not stream probes) --
    the runtime never reads traits."""

    def probe(self, prompt: str, tools: list[dict[str, Any]], *, model: str, state: Any) -> ToolProbeResult:
        """One-shot tool probe; returns normalized calls + text."""
        ...

    def probe_stream(
        self, prompt: str, tools: list[dict[str, Any]], *, model: str, state: Any
    ) -> ToolProbeStream | None:
        """Streaming tool probe handle (LAZY, zero I/O at construction), or
        ``None`` when this provider's probes do not stream."""
        ...


@dataclass(frozen=True)
class BoundModel:
    """An adapter bound to a model name -- the one object text consumers hold.

    Callers cannot re-pick the model per call (no method takes a ``model``
    parameter): the bound value is injected on every request. Phase 7-c2 adds
    the tool-probe forwards; adapters that implement ToolCallingModel get them
    for free through the same binding.
    """

    adapter: TextModel
    model: str

    def complete(self, prompt: str) -> str:
        return self.adapter.complete(prompt, model=self.model)

    def stream(self, prompt: str, state: Any) -> Iterator[str]:
        return self.adapter.stream(prompt, model=self.model, state=state)

    def probe(self, prompt: str, tools: list[dict[str, Any]], state: Any) -> ToolProbeResult:
        return self.adapter.probe(prompt, tools, model=self.model, state=state)

    def probe_stream(
        self, prompt: str, tools: list[dict[str, Any]], state: Any
    ) -> ToolProbeStream | None:
        return self.adapter.probe_stream(prompt, tools, model=self.model, state=state)
