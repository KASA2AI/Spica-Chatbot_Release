"""Pluggable parity comparators (LOCAL_RUNTIME_PLAN §6.2).

Each comparator maps ``(old_value, new_value) -> (match: bool, error_value: float)``:
- ``match`` is exact equivalence (the strict, default pass bar).
- ``error_value`` is a normalized [0, 1] distance so aggregates (mean/max) and
  thresholds are model-comparable.

OCR ships ``text_diff``. TTS ships ``audio_diff`` (waveform + mel) -- the harness
core never changes, only the comparator plugged in.
"""

from __future__ import annotations

from typing import Any


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


# ---- TTS audio comparator (LOCAL_RUNTIME_PLAN cut 2 / §6.2, decision D3) --------
# Per-waveform RMSE + log-mel error (max/mean). NOT byte-identical (TTS sampling is
# stochastic) -- a faithful re-driver run with a FIXED RNG seed should be near-zero;
# human spot-check stays the final ear (§6.2). Pass thresholds are tunable per the
# real-machine reference set; the defaults here are strict (near-determinism).

DEFAULT_WAVEFORM_RMSE_TOL = 1e-3  # normalized RMSE on [-1,1] audio
DEFAULT_LEN_RATIO_TOL = 0.02  # new/old length within 2% (a big drift = divergence)


def _coerce_audio(value: Any) -> tuple[int | None, "Any"]:
    """Accept ``(sample_rate, ndarray)`` or a bare array. int PCM (e.g. int16, what
    get_tts_wav yields) is normalized to float [-1, 1]."""
    import numpy as np

    sr: int | None = None
    if isinstance(value, tuple) and len(value) == 2:
        sr, value = value
    arr = np.asarray(value)
    if np.issubdtype(arr.dtype, np.integer):
        arr = arr.astype(np.float64) / 32768.0
    else:
        arr = arr.astype(np.float64)
    return (int(sr) if sr is not None else None), arr


def audio_metrics(old: Any, new: Any) -> dict[str, Any]:
    """Full per-pair metrics (the report stores THIS, never the raw audio arrays).

    waveform_rmse / waveform_max over the overlapping span (both normalized to
    [-1,1]); len_ratio (length divergence is itself a parity signal); log-mel
    mean/max error in dB when librosa is importable (skipped -> None otherwise, so
    the pure-numpy waveform path always works in CI)."""
    import numpy as np

    sr_old, a = _coerce_audio(old)
    sr_new, b = _coerce_audio(new)
    len_old, len_new = int(a.shape[0]), int(b.shape[0])
    n = min(len_old, len_new)
    if n == 0:
        return {
            "waveform_rmse": 1.0, "waveform_max": 1.0, "len_old": len_old,
            "len_new": len_new, "len_ratio": 0.0, "sr_old": sr_old, "sr_new": sr_new,
            "mel_mean_db": None, "mel_max_db": None,
        }
    diff = a[:n] - b[:n]
    metrics: dict[str, Any] = {
        "waveform_rmse": float(np.sqrt(np.mean(diff * diff))),
        "waveform_max": float(np.max(np.abs(diff))),
        "len_old": len_old,
        "len_new": len_new,
        "len_ratio": float(len_new / len_old) if len_old else 0.0,
        "sr_old": sr_old,
        "sr_new": sr_new,
        "mel_mean_db": None,
        "mel_max_db": None,
    }
    try:
        import librosa

        sr = int(sr_old or sr_new or 32000)

        def _logmel(y):
            spec = librosa.feature.melspectrogram(y=y.astype(np.float32), sr=sr, n_mels=80)
            return librosa.power_to_db(spec, ref=1.0)

        lo, ln = _logmel(a), _logmel(b)
        k = min(lo.shape[1], ln.shape[1])
        if k:
            md = np.abs(lo[:, :k] - ln[:, :k])
            metrics["mel_mean_db"] = float(np.mean(md))
            metrics["mel_max_db"] = float(np.max(md))
    except Exception:  # noqa: BLE001 -- librosa absent -> waveform-only metrics
        pass
    return metrics


def audio_diff(old: Any, new: Any) -> tuple[bool, float]:
    """TTS comparator: ``(match, error_value)`` where error_value = waveform RMSE.

    match requires near-zero waveform RMSE AND near-equal length (a length drift
    means the synthesis diverged even if the overlap matches)."""
    m = audio_metrics(old, new)
    error = float(m["waveform_rmse"])
    match = error <= DEFAULT_WAVEFORM_RMSE_TOL and abs(m["len_ratio"] - 1.0) <= DEFAULT_LEN_RATIO_TOL
    return bool(match), error
