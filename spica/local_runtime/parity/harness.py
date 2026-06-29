"""Model-agnostic parity harness core (LOCAL_RUNTIME_PLAN §6.2).

"Run two providers on a fixed reference input set, produce a comparison report."
Model-agnostic: the per-model logic lives entirely in the two ``run_*`` callables
(what to feed a provider and what comparable value to pull back) and the
``comparator`` (how to score old vs new). OCR binds
``run=lambda img: adapter.recognize(img).text`` + ``comparator=text_diff``.

Determinism (workflow/runtime discipline): timing uses an injectable ``clock``
(default ``perf_counter``) and the timestamp for the archived report is supplied
by the CALLER, never read inside the library -- so the harness is reproducible
and side-effect-free.
"""

from __future__ import annotations

from time import perf_counter
from typing import Any, Callable, Iterable

from spica.local_runtime.parity.report import (
    DEFAULT_THRESHOLD,
    ParityInput,
    ParityReport,
    aggregate_metrics,
    decide_verdict,
)


def run_parity(
    reference_inputs: Iterable[Any],
    run_old: Callable[[Any], Any],
    run_new: Callable[[Any], Any],
    comparator: Callable[[Any, Any], tuple[bool, float]],
    *,
    model: str,
    provider_old: str,
    provider_new: str,
    threshold: dict[str, float] | None = None,
    clock: Callable[[], float] = perf_counter,
) -> ParityReport:
    """Run both providers over the inputs, compare per-input, aggregate, decide.

    ``run_old`` / ``run_new`` each take one reference input and return the
    comparable value (already extracted -- e.g. OCR text). ``comparator`` returns
    ``(match, error_value)``. The report's verdict is decided against
    ``threshold`` (default: strict equivalence)."""
    thresholds = dict(threshold or DEFAULT_THRESHOLD)
    per_input: list[ParityInput] = []
    for idx, item in enumerate(reference_inputs):
        t0 = clock()
        old_value = run_old(item)
        t1 = clock()
        new_value = run_new(item)
        t2 = clock()
        match, error_value = comparator(old_value, new_value)
        per_input.append(
            ParityInput(
                idx=idx,
                old=old_value,
                new=new_value,
                match=bool(match),
                error_value=float(error_value),
                old_ms=round((t1 - t0) * 1000.0, 3),
                new_ms=round((t2 - t1) * 1000.0, 3),
            )
        )
    aggregate = aggregate_metrics(per_input)
    return ParityReport(
        model=model,
        provider_old=provider_old,
        provider_new=provider_new,
        per_input=per_input,
        aggregate=aggregate,
        threshold=thresholds,
        verdict=decide_verdict(aggregate, thresholds),
    )
