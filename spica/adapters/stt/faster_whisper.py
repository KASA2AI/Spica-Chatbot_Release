"""Local faster-whisper STT adapter (Plan B): the model is loaded ONCE and kept
resident; every ``transcribe`` reuses it. Replaces the network ``recognize_google``
(whose timeout-less ``urlopen`` hung the voice loop forever on a slow connection) --
this path is fully local, so it cannot hang on connectivity.

Residency (the load-bearing guarantee):
  - the ``WhisperModel`` lives in THIS instance, built by ``AppHost`` ONCE and
    injected by reference into every ``SpeechWorker``;
  - ``SpeechWorker`` is recreated per utterance but only holds the reference --
    worker churn never reloads the model;
  - ``_ensure_model`` is a double-checked-locked lazy load -> exactly one
    ``WhisperModel(...)`` construction for the process lifetime.

Concurrency: ``_lock`` guards the one-time load; ``_infer_lock`` serializes every
inference (the ``model.transcribe`` call PLUS the full lazy-generator drain).
The single-SpeechWorker architecture already keeps utterances serial, but startup
``warmup()`` runs on the warmup-worker thread while the mic is ALREADY live -- a
first utterance can otherwise decode concurrently with the warmup decode on the
same CTranslate2 model, which is not assumed safe here.

INVARIANT (CLAUDE.md #1): Qt-free.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import numpy as np

from common.timing import log_timing, now_ms

logger = logging.getLogger(__name__)


class FasterWhisperAdapter:
    name = "faster_whisper"

    def __init__(
        self,
        *,
        model: str = "large-v3-turbo",
        device: str = "cuda",
        compute_type: str = "float16",
        language: str = "zh",
        beam_size: int = 5,
        vad_filter: bool = False,
        download_root: str | None = None,
    ) -> None:
        # Construction is CHEAP: the heavy WhisperModel is NOT built here, so this
        # never blocks app startup. The model loads on first warmup()/transcribe().
        self._model_name = model
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._beam_size = beam_size
        self._vad_filter = vad_filter
        self._download_root = download_root
        self._model: Any = None
        self._lock = threading.Lock()
        # Serializes INFERENCE (transcribe call + full generator drain): startup
        # warmup and a live first utterance run on different threads against the
        # same CTranslate2 model (see module docstring).
        self._infer_lock = threading.Lock()

    def _ensure_model(self) -> Any:
        """Load the WhisperModel exactly once (double-checked lock). Subsequent
        calls return the cached instance with no work -> no per-call reload."""
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is None:  # double-checked: another thread may have loaded
                from faster_whisper import WhisperModel

                start = now_ms()
                logger.info(
                    "loading faster-whisper model=%s device=%s compute_type=%s "
                    "download_root=%s (one-time; reused for the process lifetime)",
                    self._model_name, self._device, self._compute_type, self._download_root,
                )
                try:
                    self._model = WhisperModel(
                        self._model_name,
                        device=self._device,
                        compute_type=self._compute_type,
                        download_root=self._download_root or None,
                    )
                except Exception as exc:  # noqa: BLE001 -- surface a CLEAR cause
                    # Caveat #1: a missing/undownloaded model or a bad device must
                    # produce a diagnosable log, not a silent "no text comes out".
                    logger.error(
                        "faster-whisper model load FAILED (model=%s device=%s "
                        "download_root=%s): %s -- STT will not transcribe until this "
                        "is fixed (check the model is downloaded / device available)",
                        self._model_name, self._device, self._download_root, exc,
                        exc_info=True,
                    )
                    raise
                log_timing("stt_model_load", now_ms() - start, model=self._model_name, device=self._device)
                logger.info("faster-whisper model loaded in %.0fms", now_ms() - start)
        return self._model

    def warmup(self) -> dict[str, Any]:
        """Load the model + one tiny dummy inference to warm CUDA kernels. Driven by
        ``run_warmup`` at startup (mirrors the TTS warmup contract). Best-effort."""
        start = now_ms()
        try:
            model = self._ensure_model()
            # 1s of silence -> compiles/warms the decode path without needing audio.
            # transcribe() is LAZY (segments is a generator; encode/decode only run
            # while iterating) -- the drain below is what actually executes the
            # decode path. Without it this "warmup" only loaded the weights.
            # _infer_lock: the mic is already live during startup warmup, so a
            # first utterance must not decode concurrently with this.
            with self._infer_lock:
                segments, _info = model.transcribe(
                    np.zeros(16000, dtype=np.float32), language=self._language
                )
                for _segment in segments:
                    pass
            duration_ms = now_ms() - start
            log_timing("stt_warmup", duration_ms, model=self._model_name)
            logger.info("faster-whisper warmup ok in %.0fms", duration_ms)
            return {"ok": True, "duration_ms": duration_ms}
        except Exception as exc:  # noqa: BLE001 -- warmup never raises (mirrors TTS)
            logger.error("faster-whisper warmup failed: %s", exc, exc_info=True)
            return {"ok": False, "duration_ms": now_ms() - start, "error": str(exc)}

    def transcribe(self, pcm: bytes, *, sample_rate: int = 16000) -> str:
        """PCM (16-bit mono) -> text. Local, no network. Model already resident
        after warmup/first call (``_ensure_model`` is a no-op thereafter)."""
        start = now_ms()
        model = self._ensure_model()  # no-op after the first load
        # 16-bit signed PCM -> float32 in [-1, 1). ReSpeaker channel 0 is already
        # 16 kHz mono, so no resampling (whisper wants 16 kHz too).
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        # _infer_lock covers the transcribe call AND the join (the generator is
        # lazy -- decoding happens while iterating), so warmup/utterance decodes
        # never overlap on the shared CTranslate2 model.
        with self._infer_lock:
            segments, _info = model.transcribe(
                audio,
                language=self._language,
                beam_size=self._beam_size,
                vad_filter=self._vad_filter,
            )
            text = "".join(segment.text for segment in segments).strip()
        log_timing(
            "stt_transcribe", now_ms() - start,
            chars=len(text), audio_ms=round(len(audio) / sample_rate * 1000),
            text=text[:80],  # so recognition accuracy is verifiable from the log (local debug)
        )
        return text
