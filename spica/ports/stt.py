"""Shared speech-to-text capability with an explicitly selected backend.

The host owns one adapter and its lifecycle. Local mode never uploads audio;
cloud mode uploads admitted utterances only, with no implicit provider fallback.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# Includes admission/serialization, a cold local model load, and recognition.
# Callers leave five seconds beyond this budget for child cleanup and delivery.
STT_REQUEST_TIMEOUT_SECONDS = 110.0


@runtime_checkable
class SpeechToTextPort(Protocol):
    name: str

    def transcribe(self, pcm: bytes, *, sample_rate: int = 16000) -> str:
        """Transcribe a single VAD-segmented utterance (16-bit mono PCM at
        ``sample_rate``) to text. Synchronous + blocking on the caller's worker
        thread with a bounded request and no implicit backend fallback. Local
        models are reused; cloud implementations do not load local ASR models."""
        ...

    def warmup(self) -> dict[str, Any]:
        """Prepare local resources; cloud implementations only check local state.

        Returns {"ok": bool, "duration_ms": float, "error"?: str}. Cloud
        preparation is not evidence of a successful remote transcription.
        """
        ...

    def close(self) -> None:
        """Idempotently reject new requests and interrupt active recognition.

        The host can call this before draining consumers; plugin cleanup may
        call it again after the remaining business writers finish.
        """
        ...
