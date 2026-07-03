"""Model port v2 -- the text family (OO migration Phase 6a).

``TextModel`` is the ADAPTER-side contract: ``complete``/``stream`` take the
prompt and the model name, and the request dict is assembled INSIDE the
adapter (the depth v1 lacks -- ``LLMPort.iter_response_text`` makes callers
build the request themselves). ``BoundModel`` pairs an adapter with a model
name so turn-external text consumers (summarizer / reaction judge) hold ONE
object and never resolve endpoints or model names per call.

v1 ``LLMPort`` (``spica/ports/llm.py``) stays frozen: the sync museum chain
(``sync_chain.py`` / ``call_llm_node``) and the pre-Phase-7 production chain
are its permanent users. ``spica/galgame`` + ``spica/host`` must not grow new
v1 consumers -- pinned by ``tests/test_no_new_v1_llm_consumers.py``.

BoundModel deliberately has NO ``complete_text`` compatibility shim: the v2
path calls ``adapter.complete`` / ``adapter.stream`` only (structurally pinned
by ``tests/test_text_model_contract.py``). A v1-shaped fake must be updated to
the adapter-side v2 shape, never papered over here.

Qt-free (铁律 #1); pure types, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Protocol


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
class BoundModel:
    """An adapter bound to a model name -- the one object text consumers hold.

    Callers cannot re-pick the model per call (``complete``/``stream`` take no
    ``model`` parameter): the bound value is injected on every request.
    """

    adapter: TextModel
    model: str

    def complete(self, prompt: str) -> str:
        return self.adapter.complete(prompt, model=self.model)

    def stream(self, prompt: str, state: Any) -> Iterator[str]:
        return self.adapter.stream(prompt, model=self.model, state=state)
