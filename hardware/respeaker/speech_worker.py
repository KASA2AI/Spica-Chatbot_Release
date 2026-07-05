from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from .audio import (
    ReSpeakerAudioError,
    ReSpeakerNoSpeechError,
    ReSpeakerRecordingCancelled,
    record_respeaker_channel0_hardware_vad,
    resolve_end_silence_seconds,
)


FATAL_SPEECH_ERROR_MARKERS = (
    "缺少 speech_recognition",
    "缺少 PyAudio",
    "Could not find PyAudio",
    "No Default Input Device",
    "Invalid input device",
    "无法打开 ReSpeaker",
    "ReSpeaker 硬件 VAD 不可用",
    # W3 (P2-3): the generic-mic backend wraps EVERY open failure -- no device,
    # privacy/permission denial, unsupported params, unknown mic_backend -- in
    # this envelope, so a mic-less machine stops the loop instead of retrying
    # forever. Mid-take read failures deliberately do NOT carry it (transient).
    "无法打开麦克风",
)


def is_fatal_speech_error(message: str) -> bool:
    return any(marker in message for marker in FATAL_SPEECH_ERROR_MARKERS)


class SpeechWorker(QThread):
    status_changed = Signal(str)
    recognized = Signal(str)
    failed = Signal(str)

    def __init__(self, parent=None, *, stt_port=None, mic_backend: str = "respeaker") -> None:
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
        # W3: which mic RECORDER records the utterance -- a resolved STRING from
        # AppHost (resolve_mic_backend), dispatched in _record(). Default
        # "respeaker" == the pre-W3 hardware path (byte-equivalent when unwired).
        self._mic_backend = mic_backend

    def is_capturing_user_speech(self) -> bool:
        return self._capturing

    def _mark_capturing(self) -> None:
        self._capturing = True

    def run(self) -> None:
        self._capturing = False
        try:
            self.status_changed.emit("等待说话中....")
            pcm = self._record(
                should_stop=self.isInterruptionRequested,
                on_speech_start=self._mark_capturing,
                end_silence_seconds=resolve_end_silence_seconds(),
            )
            if self.isInterruptionRequested():
                return
            if not pcm:
                self.failed.emit("没有检测到语音输入。")
                return
            self.status_changed.emit("...")
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

    def _record(self, **kwargs) -> bytes:
        """Dispatch to the resolved mic backend (W3). Both lanes share one call
        face (the W3-a recorder contract). The respeaker lane resolves through
        the MODULE namespace (tests monkeypatch it there); the generic lane is
        imported lazily so a respeaker-only environment never needs webrtcvad's
        import chain at worker-construction time."""
        if self._mic_backend == "respeaker":
            return record_respeaker_channel0_hardware_vad(**kwargs)
        if self._mic_backend == "generic":
            from hardware.audio_input.generic_mic import record_generic_mic_software_vad

            return record_generic_mic_software_vad(**kwargs)
        # A mis-wired backend cannot open any mic: use the FATAL envelope so the
        # voice loop stops instead of retrying forever (P2-3).
        raise ReSpeakerAudioError(f"无法打开麦克风：未知 mic_backend {self._mic_backend!r}。")

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
