from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer

from hardware.respeaker.speech_worker import SpeechWorker, is_fatal_speech_error


class VoiceInputController(QObject):
    def __init__(
        self,
        parent: QObject,
        set_voice_active: Callable[[bool], None],
        set_busy: Callable[[bool], None],
        is_conversation_busy: Callable[[], bool],
        set_dialogue_text: Callable[[str], None],
        on_recognized_text: Callable[[str], None],
        backend_ready: Callable[[], bool],
    ) -> None:
        super().__init__(parent)
        self.set_voice_active = set_voice_active
        self.set_busy = set_busy
        self.is_conversation_busy = is_conversation_busy
        self.set_dialogue_text = set_dialogue_text
        self.on_recognized_text = on_recognized_text
        self.backend_ready = backend_ready

        self.speech_worker: SpeechWorker | None = None
        self.voice_mode_active = False
        self.voice_session_id = 0
        # Plan B: the resident local STT adapter (faster-whisper), injected AFTER
        # host.initialize() via set_stt_port (this controller is built before the
        # host). Passed by reference into each SpeechWorker; None -> google fallback.
        self._stt_port = None
        # W3: resolved mic recorder backend STRING (host.effective_mic_backend),
        # injected after host.initialize() like the STT port. Default keeps the
        # pre-W3 hardware path when unwired.
        self._mic_backend = "respeaker"

    def set_on_recognized_text(self, on_recognized_text: Callable[[str], None]) -> None:
        self.on_recognized_text = on_recognized_text

    def set_stt_port(self, stt_port) -> None:
        """Inject the resident STT adapter (delayed: the host builds it during
        initialize(), after this controller is constructed). Each subsequent
        SpeechWorker gets this same instance by reference -- no per-worker reload."""
        self._stt_port = stt_port

    def set_mic_backend(self, mic_backend: str) -> None:
        """Inject the resolved mic recorder backend (W3; same delayed-wiring shape
        as set_stt_port). Each subsequent SpeechWorker dispatches on this string."""
        self._mic_backend = mic_backend

    def start(self) -> None:
        if not self.backend_ready():
            self.set_voice_active(False)
            self.set_dialogue_text("后端未初始化，请检查 OPENAI_API_KEY 和本地依赖。")
            return

        self.voice_mode_active = True
        self.voice_session_id += 1
        self.set_voice_active(True)
        self.set_busy(self.is_conversation_busy())
        self.maybe_start_recording(self.voice_session_id)

    def stop(self) -> None:
        self.voice_mode_active = False
        self.voice_session_id += 1
        self.set_voice_active(False)
        if self.speech_worker and self.speech_worker.isRunning():
            self.speech_worker.requestInterruption()
        self.set_dialogue_text("语音模式已关闭。")
        self.set_busy(self.is_conversation_busy())

    def toggle(self) -> None:
        if self.voice_mode_active:
            self.stop()
            return
        self.start()

    def maybe_start_recording(self, session_id: int | None = None) -> None:
        if session_id is not None and session_id != self.voice_session_id:
            return
        if not self.voice_mode_active:
            return
        if self.speech_worker and self.speech_worker.isRunning():
            return
        if self.is_conversation_busy():
            return
        self._start_speech_worker()

    def schedule_next_recording(self, delay_ms: int = 320) -> None:
        if not self.voice_mode_active:
            return
        session_id = self.voice_session_id
        QTimer.singleShot(delay_ms, lambda sid=session_id: self.maybe_start_recording(sid))

    def shutdown(self, wait_ms: int = 1500) -> None:
        self.voice_mode_active = False
        self.voice_session_id += 1
        if self.speech_worker and self.speech_worker.isRunning():
            self.speech_worker.requestInterruption()
            self.speech_worker.quit()
            self.speech_worker.wait(wait_ms)
        if self.speech_worker is not None:
            try:
                self.speech_worker.deleteLater()
            except Exception:
                pass
            self.speech_worker = None

    def interrupt_current_recording(self) -> None:
        self.voice_session_id += 1
        if self.speech_worker and self.speech_worker.isRunning():
            self.speech_worker.requestInterruption()

    def is_capturing_user_speech(self) -> bool:
        """True only while a SpeechWorker has actually detected the user speaking
        (hardware VAD started), NOT while it idly waits for speech. The P3 arbiter
        treats this -- not the mere presence of a running worker -- as "busy", so a
        proactive reaction can fire during the (common) idle-listen gaps yet never
        cuts off a half-spoken sentence (option A: preempt idle, never active)."""
        worker = self.speech_worker
        return bool(
            worker is not None
            and worker.isRunning()
            and worker.is_capturing_user_speech()
        )

    def handle_speech_status(self, message: str, session_id: int) -> None:
        if session_id == self.voice_session_id and self.voice_mode_active:
            self.set_dialogue_text(message)

    def handle_recognized(self, text: str, session_id: int) -> None:
        if session_id != self.voice_session_id or not self.voice_mode_active:
            return
        text = (text or "").strip()
        if not text:
            self.schedule_next_recording(600)
            return
        self.on_recognized_text(text)

    def handle_error(self, message: str, session_id: int) -> None:
        if session_id != self.voice_session_id or not self.voice_mode_active:
            return
        self.set_dialogue_text(message)
        if is_fatal_speech_error(message):
            self.voice_mode_active = False
            self.voice_session_id += 1
            self.set_voice_active(False)
            self.set_busy(False)

    def handle_finished(self, session_id: int) -> None:
        if self.speech_worker and not self.speech_worker.isRunning():
            self.speech_worker.deleteLater()
            self.speech_worker = None
        if session_id != self.voice_session_id:
            return
        if not self.voice_mode_active:
            self.set_busy(self.is_conversation_busy())
            return
        if self.is_conversation_busy():
            self.set_busy(True)
            return
        self.set_busy(False)
        self.schedule_next_recording(650)

    def _start_speech_worker(self) -> None:
        self.set_busy(True)
        session_id = self.voice_session_id
        self.speech_worker = SpeechWorker(self, stt_port=self._stt_port, mic_backend=self._mic_backend)
        self.speech_worker.status_changed.connect(
            lambda message, sid=session_id: self.handle_speech_status(message, sid)
        )
        self.speech_worker.recognized.connect(
            lambda text, sid=session_id: self.handle_recognized(text, sid)
        )
        self.speech_worker.failed.connect(
            lambda message, sid=session_id: self.handle_error(message, sid)
        )
        self.speech_worker.finished.connect(lambda sid=session_id: self.handle_finished(sid))
        self.speech_worker.start()


class ReactionVoiceDuckGate:
    """Real ``VoiceInputGate`` (the full-duplex seam proactive.py reserves): when
    she starts a SYSTEM turn (galgame reaction / song report) it ducks the idly
    listening mic so her own TTS is not captured as the user speaking.

    Resume is FREE: a system turn's completion always runs ``on_chat_done`` ->
    ``schedule_next_recording`` (``_end_stream_playback`` is reached for a played
    AND a swallowed NO_COMMENT turn; an errored turn resumes via ``on_error``), so
    this gate only needs the duck -- adding a resume here would double-start the
    mic. No-op outside voice mode (text mode never holds the mic).

    Duck-typed to ``spica.core.proactive.VoiceInputGate``. ``before_system_speech``
    runs on the reaction engine's worker thread -- exactly like the existing
    ``start_turn`` call on that path -- and only touches ``QThread.requestInterruption``
    (atomic) plus a monotonic session-id bump, so no widget is touched off-GUI.
    """

    def __init__(self, voice_input_controller: VoiceInputController | None) -> None:
        self._vc = voice_input_controller

    def before_system_speech(self) -> None:
        vc = self._vc
        if vc is not None and vc.voice_mode_active:
            vc.interrupt_current_recording()

    def after_system_speech(self) -> None:
        return None  # resume rides the existing on_chat_done path (see class docstring)
