"""Pluggable parity comparators (LOCAL_RUNTIME_PLAN §6.2).

Each comparator maps ``(old_value, new_value) -> (match: bool, error_value: float)``:
- ``match`` is exact equivalence (the strict, default pass bar).
- ``error_value`` is a normalized [0, 1] distance so aggregates (mean/max) and
  thresholds are model-comparable.

OCR ships ``text_diff``. TTS's ``audio_diff`` (mel/waveform) lands with the
second cut -- the harness core never changes, only the comparator plugged in.
"""

from __future__ import annotations


def _levenshtein(a: str, b: str) -> int:
    """Edit distance (insert/delete/substitute). Pure-python, no deps -- CI must
    run without numpy/extra packages on the parity path (§6.5)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            insert = current[j - 1] + 1
            delete = previous[j] + 1
            substitute = previous[j - 1] + (0 if ca == cb else 1)
            current.append(min(insert, delete, substitute))
        previous = current
    return previous[-1]


def text_diff(old: object, new: object) -> tuple[bool, float]:
    """OCR comparator: character-level text equivalence.

    ``error_value`` = normalized edit distance = ``levenshtein / max(len)`` in
    [0, 1]. Two empty strings are a perfect match (0.0). Non-str inputs are
    coerced via ``str(...)`` so a provider returning ``None`` is comparable
    rather than crashing the harness."""
    old_text = "" if old is None else str(old)
    new_text = "" if new is None else str(new)
    if old_text == new_text:
        return True, 0.0
    distance = _levenshtein(old_text, new_text)
    denom = max(len(old_text), len(new_text), 1)
    return False, distance / denom
