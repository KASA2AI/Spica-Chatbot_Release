"""Parity harness (LOCAL_RUNTIME_PLAN §6) -- the head-of-phase infrastructure.

The golden event tests pin stream/structure, NOT real model quality: after a
TRT/ORT swap her voice could degrade or OCR mis-read and golden stays green.
Parity is the only backstop against that silent regression. It is built and
self-verified (old-vs-old ~0 diff) in the FIRST cut and reused by all four.

Model-agnostic core (``harness.run_parity``) + pluggable comparators
(``comparators``: ``text_diff`` for OCR, ``audio_diff`` for TTS) + a serializable
report (``report.ParityReport``) that is the SOLE gate for switching the default
provider / deleting old / removing fallback (§6.1).
"""

from spica.local_runtime.parity.comparators import audio_diff, audio_metrics, text_diff
from spica.local_runtime.parity.harness import run_parity
from spica.local_runtime.parity.report import ParityInput, ParityReport

__all__ = ["text_diff", "audio_diff", "audio_metrics", "run_parity", "ParityInput", "ParityReport"]
