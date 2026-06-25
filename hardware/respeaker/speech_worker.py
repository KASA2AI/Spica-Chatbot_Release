from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from .audio import (
    ReSpeakerNoSpeechError,
    ReSpeakerRecordingCancelled,
    record_respeaker_channel0_hardware_vad,
)


FATAL_SPEECH_ERROR_MARKERS = (
    "缺少 speech_recognition",
    "缺少 PyAudio",
    "Could not find PyAudio",
    "No Default Input Device",
    "Invalid input device",
    "无法打开 ReSpeaker",
    "ReSpeaker 硬件 VAD 不可用",
)


def is_fatal_speech_error(message: str) -> bool:
    return any(marker in message for marker in FATAL_SPEECH_ERROR_MARKERS)


class SpeechWorker(QThread):
    status_changed = Signal(str)
    recognized = Signal(str)
    failed = Signal(str)

    def __init__(self, parent=None, *, stt_port=None) -> None:
        super().__init__(parent)
        # Flipped True on THIS thread once the hardware VAD detects the user has
        # actually started speaking; stays True through recognition (so a barging
        # reaction never drops a just-finished utterance) and resets each run().
        # Read cross-thread (GUI + reaction worker) as a plain atomic bool -- a
        # best-effort "is the user mid-utterance?" hint for the P3 arbiter.
        self._capturing = False
        # Plan B: injected local STT (faster-whisper) -- a REFERENCE to AppHost's
        # resident singleton, NOT owned/loaded here (worker churn != model churn).
        # None -> the legacy in-worker recognize_google fallback (backend=google).
        self._stt = stt_port

    def is_capturing_user_speech(self) -> bool:
        return self._capturing

    def _mark_capturing(self) -> None:
        self._capturing = True

    def run(self) -> None:
        self._capturing = False
        try:
            self.status_changed.emit("正在等待 ReSpeaker 硬件 VAD...")
            pcm = record_respeaker_channel0_hardware_vad(
                should_stop=self.isInterruptionRequested,
                on_speech_start=self._mark_capturing,
            )
            if self.isInterruptionRequested():
                return
            if not pcm:
                self.failed.emit("没有检测到语音输入。")
                return
            self.status_changed.emit("识别中...")
            if self.isInterruptionRequested():
                return
            text = self._transcribe(pcm)  # local whisper by default; google only if unwired
        except ReSpeakerRecordingCancelled:
            return
        except ReSpeakerNoSpeechError:
            self.failed.emit("没有检测到语音输入。")
            return
        except Exception as exc:  # noqa: BLE001 -- non-fatal: loop resumes via finished
            # A clear cause reaches the dialog (e.g. faster-whisper model not loaded /
            # download failed -> _ensure_model raised with a diagnosable message).
            self.failed.emit(f"语音识别失败：{exc}")
            return

        text = (text or "").strip()
        if text:
            self.recognized.emit(text)
        else:
            self.failed.emit("没有识别到有效中文。")

    def _transcribe(self, pcm: bytes) -> str:
        """PCM (16-bit mono 16 kHz) -> text.

        Default: the injected local faster-whisper adapter -- LOCAL, no network, so
        it CANNOT hang (this is the fix: the old recognize_google had a timeout-less
        urlopen that froze the loop on "识别中"). The model is already resident in the
        adapter (loaded once at warmup), so this only transcribes -- never loads.

        Fallback (no stt_port -> backend=google or unwired): the legacy
        recognize_google. NOTE: still timeout-less (can hang); kept only as an
        explicit opt-out and never selected by default."""
        if self._stt is not None:
            return self._stt.transcribe(pcm, sample_rate=16000)
        import speech_recognition as sr

        try:
            return sr.Recognizer().recognize_google(sr.AudioData(pcm, 16000, 2), language="zh-CN")
        except sr.UnknownValueError:
            return ""  # not understood -> run() emits the standard "没有识别到有效中文"
