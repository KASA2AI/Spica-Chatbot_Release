from __future__ import annotations

import time
from hardware.audio_input.speech_errors import is_fatal_speech_error

from PySide6.QtCore import QThread, Signal

from hardware.respeaker.audio import (
    ReSpeakerAudioError,
    ReSpeakerCaptureReleaseError,
    ReSpeakerNoSpeechError,
    ReSpeakerRecordingCancelled,
    record_respeaker_channel0_hardware_vad,
    resolve_end_silence_seconds,
)


class SpeechWorker(QThread):
    capture_ready = Signal(float)
    capture_finished = Signal()
    status_changed = Signal(str)
    recognized = Signal(str)
    failed = Signal(str)

    def __init__(self, parent=None, *, stt_port=None, mic_backend: str = "generic", input_device: str = "") -> None:
        super().__init__(parent)
        # Flipped True on THIS thread once the hardware VAD detects the user has
        # actually started speaking; stays True through recognition (so a barging
        # reaction never drops a just-finished utterance) and resets each run().
        # Read cross-thread (GUI + reaction worker) as a plain atomic bool -- a
        # best-effort "is the user mid-utterance?" hint for the P3 arbiter.
        self._capturing = False
        self.speech_started_at = None
        self.capture_ready_at = None
        self.capture_released = False
        self.capture_release_error = None
        # Injected, explicitly selected STT -- a REFERENCE to AppHost's
        # resident singleton, NOT owned/loaded here (worker churn != model churn).
        # An unwired port is an explicit failure; audio never goes to a fallback.
        self._stt = stt_port
        # W3: which mic RECORDER records the utterance -- a resolved STRING from
        # AppHost (resolve_mic_backend), dispatched in _record(). Default
        # "respeaker" == the pre-W3 hardware path (byte-equivalent when unwired).
        self._mic_backend = mic_backend
        self._input_device = input_device

    def is_capturing_user_speech(self) -> bool:
        return self._capturing

    def _mark_capturing(self) -> None:
        self._capturing = True
        self.speech_started_at = time.monotonic()

    def _mark_ready(self) -> None:
        self.capture_ready_at = time.monotonic()
        self.capture_ready.emit(self.capture_ready_at)

    def run(self) -> None:
        self._capturing = False
        try:
            if self._stt is None or getattr(self._stt, "name", "") == "stt_unavailable":
                self.capture_released = True
                raise RuntimeError("语音识别未配置，请先在设置中选择并配置识别后端。")
            self.status_changed.emit("等待说话中....")
            try:
                pcm = self._record(
                    should_stop=self.isInterruptionRequested,
                    on_speech_start=self._mark_capturing,
                    on_ready=self._mark_ready,
                    end_silence_seconds=resolve_end_silence_seconds(),
                )
            except ReSpeakerCaptureReleaseError as exc:
                self.capture_release_error = exc
                raise
            finally:
                self.capture_released = self.capture_release_error is None
                self.capture_finished.emit()
            if self.isInterruptionRequested():
                return
            if not pcm:
                self.failed.emit("没有检测到语音输入。")
                return
            self.status_changed.emit("...")
            if self.isInterruptionRequested():
                return
            text = self._transcribe(pcm)
        except ReSpeakerRecordingCancelled:
            return
        except ReSpeakerNoSpeechError:
            self.failed.emit("没有检测到语音输入。")
            return
        except Exception as exc:  # noqa: BLE001 -- non-fatal: loop resumes via finished
            # A clear cause reaches the dialog; the voice loop owns recovery.
            if not self.isInterruptionRequested() or self.capture_release_error is not None:
                self.failed.emit(f"语音识别失败：{exc}")
            return

        if self.isInterruptionRequested():
            return
        text = (text or "").strip()
        if text:
            self.recognized.emit(text)
        else:
            self.failed.emit("没有识别到有效中文。")

    def _record(self, **kwargs) -> bytes:
        """Dispatch to the resolved mic backend (W3). Both lanes share one call
        face (the W3-a recorder contract). The respeaker lane resolves through
        the MODULE namespace (tests monkeypatch it there); the generic lane is
        imported lazily so a respeaker-only environment never needs webrtcvad's
        import chain at worker-construction time."""
        if self._input_device:
            kwargs["input_device"] = self._input_device
        if self._mic_backend == "respeaker":
            return record_respeaker_channel0_hardware_vad(**kwargs)
        if self._mic_backend == "generic":
            from hardware.audio_input.generic_mic import record_generic_mic_software_vad

            return record_generic_mic_software_vad(**kwargs)
        # A mis-wired backend cannot open any mic: use the FATAL envelope so the
        # voice loop stops instead of retrying forever (P2-3).
        raise ReSpeakerAudioError(f"无法打开麦克风：未知 mic_backend {self._mic_backend!r}。")

    def _transcribe(self, pcm: bytes) -> str:
        """PCM -> the selected recognizer; no implicit fallback."""
        if self._stt is None:
            raise RuntimeError("语音识别未配置；请检查所选识别后端的配置。")
        return self._stt.transcribe(pcm, sample_rate=16000)
