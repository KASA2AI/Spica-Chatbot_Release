"""Streaming turn entry point (core C1.5).

``run_turn`` is THE streaming entry: it drives one turn and yields typed
``RuntimeEvent``s. For now it is a thin facade over the existing streaming
orchestrator (``stream_voice_events``), converting that orchestrator's legacy
``{"event", "data"}`` dicts to ``RuntimeEvent`` at the boundary -- exactly the
adapter that used to live inline in ``ChatEngine.stream_voice_runtime``.

C1.5 is deliberately zero-behaviour-change: the dict <-> RuntimeEvent round-trip
is lossless (locked by tests/test_turn_contract.py), so the event stream is
identical to before, only typed. Later stages evolve this seam:

- C2 folds the synchronous path onto ``run_turn`` + ``fold_events``;
- C3a retypes the signature to ``run_turn(req: TurnRequest, deps: TurnDeps)``.

Until then it takes the current ``AgentState`` + ``AgentServices`` (typed ``Any``
here to avoid adding a new spica -> agent import edge ahead of the C4 relayer).

INVARIANT (CLAUDE.md #1 + #7): Qt-free; cross-boundary events are dataclasses.
"""

from __future__ import annotations

from typing import Any, Iterator

from spica.core.events import RuntimeEvent, event_from_legacy
from spica.runtime.orchestrator import stream_voice_events


def run_turn(state: Any, services: Any, exec_strategy: Any = None) -> Iterator[RuntimeEvent]:
    """Drive one streaming turn, yielding typed ``RuntimeEvent``s in order.

    ``exec_strategy`` (C2) is the injected concurrency policy: ``None`` -> the
    orchestrator's default ``Threaded`` pools (streaming); ``Inline()`` -> every
    lane runs synchronously, which the fold-based sync path uses.
    """
    for legacy in stream_voice_events(state, services, exec_strategy):
        yield event_from_legacy(legacy)
