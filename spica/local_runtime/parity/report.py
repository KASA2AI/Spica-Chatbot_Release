"""Parity report data structures (LOCAL_RUNTIME_PLAN §6.4).

Structured + JSON-serializable so a script can decide pass/fail. The report is
the SOLE gate (§6.1) for switching the default provider / deleting old /
removing fallback -- "no report or below threshold -> all four forbidden".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Default pass bar: strict equivalence. Right for old-vs-old self-verify (must be
# identical) and the conservative default for old-vs-new until each model sets
# its own threshold (§15 open question -- OCR threshold decided per cut).
DEFAULT_THRESHOLD: dict[str, float] = {"min_match_rate": 1.0, "max_error": 0.0}


@dataclass(frozen=True)
class ParityInput:
    idx: int
    old: Any
    new: Any
    match: bool
    error_value: float
    old_ms: float
    new_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "idx": self.idx,
            "old": self.old,
            "new": self.new,
            "match": self.match,
            "error_value": self.error_value,
            "old_ms": self.old_ms,
            "new_ms": self.new_ms,
        }


@dataclass(frozen=True)
class ParityReport:
    model: str
    provider_old: str
    provider_new: str
    per_input: list[ParityInput] = field(default_factory=list)
    aggregate: dict[str, float] = field(default_factory=dict)
    threshold: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLD))
    verdict: str = "fail"

    @property
    def is_pass(self) -> bool:
        return self.verdict == "pass"

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider_old": self.provider_old,
            "provider_new": self.provider_new,
            "per_input": [item.to_dict() for item in self.per_input],
            "aggregate": dict(self.aggregate),
            "threshold": dict(self.threshold),
            "verdict": self.verdict,
        }


def aggregate_metrics(per_input: list[ParityInput]) -> dict[str, float]:
    """match_rate / mean_error / max_error / mean timings (§6.4)."""
    n = len(per_input)
    if n == 0:
        return {
            "count": 0,
            "match_rate": 0.0,
            "mean_error": 0.0,
            "max_error": 0.0,
            "mean_old_ms": 0.0,
            "mean_new_ms": 0.0,
        }
    matches = sum(1 for item in per_input if item.match)
    errors = [item.error_value for item in per_input]
    return {
        "count": n,
        "match_rate": matches / n,
        "mean_error": sum(errors) / n,
        "max_error": max(errors),
        "mean_old_ms": sum(item.old_ms for item in per_input) / n,
        "mean_new_ms": sum(item.new_ms for item in per_input) / n,
    }


def decide_verdict(aggregate: dict[str, float], threshold: dict[str, float]) -> str:
    """pass iff match_rate >= min_match_rate AND max_error <= max_error."""
    min_match_rate = threshold.get("min_match_rate", DEFAULT_THRESHOLD["min_match_rate"])
    max_error = threshold.get("max_error", DEFAULT_THRESHOLD["max_error"])
    ok = aggregate.get("match_rate", 0.0) >= min_match_rate and aggregate.get("max_error", 1.0) <= max_error
    return "pass" if ok else "fail"
