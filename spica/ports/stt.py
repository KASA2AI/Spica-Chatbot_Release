"""Speech-to-text capability port (Plan B).

A seam over "PCM bytes -> recognized text" so the voice loop never hard-codes a
recognizer. The default adapter is local faster-whisper (no network, so the old
``recognize_google`` freeze -- a hung, timeout-less ``urlopen`` -- cannot recur).

The model is HEAVY and MUST be loaded once and kept resident: an adapter holds it
as a singleton and reuses it across calls (see ``FasterWhisperAdapter``). The
voice worker is recreated per utterance but only ever receives a REFERENCE to the
already-loaded adapter, so worker churn never reloads the model.

INVARIANT (CLAUDE.md #1): Qt-free -- the adapter lives under ``spica/``; the Qt
``SpeechWorker`` (hardware/) receives this port by injection from ``ui/``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SpeechToTextPort(Protocol):
    name: str

    def transcribe(self, pcm: bytes, *, sample_rate: int = 16000) -> str:
        """Transcribe a single VAD-segmented utterance (16-bit mono PCM at
        ``sample_rate``) to text. Synchronous + blocking on the caller's worker
        thread (same contract the old ``recognize_google`` had), but LOCAL -- no
        network, so it cannot hang on connectivity. The model is loaded lazily on
        the first call and reused thereafter (never reloaded per call)."""
        ...

    def warmup(self) -> dict[str, Any]:
        """Load the model once + run one tiny dummy inference to warm CUDA kernels,
        so the first real utterance has no load/compile lag. Returns a result dict
        ``{"ok": bool, "duration_ms": float, "error"?: str}`` mirroring the TTS
        warmup contract (so ``spica.host.warmup.run_warmup`` can drive it the same
        way). Best-effort: a failure is reported, never raised."""
        ...
