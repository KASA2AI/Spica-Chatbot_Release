from __future__ import annotations

import logging
import math
import time
import uuid
from collections import deque
from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer, Qt, Signal

from hardware.audio_input.speech_errors import is_fatal_speech_error
from ui.workers.speech_worker import SpeechWorker
from hardware.audio_input.keyword_spotter import StreamingKeywordSpotter
from ui.workers.wake_word_worker import WakeWordWorker


logger = logging.getLogger(__name__)


class VoiceInputController(QObject):
    wake_enabled_changed = Signal(bool)
    manual_mute_changed = Signal(bool)
    capture_state_changed = Signal()
    daily_ended = Signal(str)

    def __init__(
        self,
        parent: QObject,
        set_voice_active: Callable[[bool], None],
        set_busy: Callable[[bool], None],
        is_conversation_busy: Callable[[], bool],
        set_dialogue_text: Callable[[str], None],
        on_recognized_text: Callable[[str], None],
        backend_ready: Callable[[], bool],
        capture_allowed: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.set_voice_active = set_voice_active
        self.set_busy = set_busy
        self.is_conversation_busy = is_conversation_busy
        self.set_dialogue_text = set_dialogue_text
        self.on_recognized_text = on_recognized_text
        self.backend_ready = backend_ready
        # C1b: a persistent capture permission is injected after construction.  The
        # default preserves the ordinary desktop behaviour byte-for-byte.
        self._capture_allowed = capture_allowed or (lambda: True)

        self.speech_worker: SpeechWorker | WakeWordWorker | None = None
        self._worker_session_id: int | None = None
        self._wake_detector = StreamingKeywordSpotter()
        self.voice_mode_active = False
        self.wake_enabled = False
        self.wake_words: tuple[str, ...] = ()
        self.on_wake_word: Callable[[str], None] | None = None
        self._wake_reply_session: int | None = None
        self._response_until = 0.
        self._response_seconds = 0.
        self._response_deadline = 0.
        self._response_scope_id = ''
        self._response_kind = ''
        self._response_generation = 0
        self._response_preparing = False
        self.capture_ready = False
        self.wake_ready = False
        self.manual_muted = False
        self._suspended = False
        self.capture_error = ''
        self._closed_response_scopes = deque(maxlen=64)
        self._response_pending = False
        self._response_finishing = False
        self._finishing_response_session: int | None = None
        self._response_timer = QTimer(self)
        self._response_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._response_timer.setSingleShot(True)
        self._response_timer.timeout.connect(self._expire_response_window)
        self.voice_session_id = 0
        # Only an explicit idle resume may replace an interrupted worker after
        # it exits (presence arrival, mode change or completed playback).
        self._resume_after_stale_worker = False
        # The resident local STT adapter, injected AFTER
        # host.initialize() via set_stt_port (this controller is built before the
        # host). Passed by reference into each SpeechWorker; None -> explicit initialization failure.
        self._stt_port = None
        # W3: resolved mic recorder backend STRING (host.effective_mic_backend),
        # injected after host.initialize() like the STT port. Default keeps the
        # pre-W3 hardware path when unwired.
        self._mic_backend = "generic"
        self._input_device = ""
        self._capture_release_error = None
        self._device_testing = False
        self._bound_reply = None
        self.daily_ended.connect(self.stop_daily_reply)

    @property
    def suspended(self):
        return self._suspended

    @property
    def capture_allowed(self):
        return self._is_capture_allowed()

    @property
    def preparing_response(self):
        return self._response_preparing

    @property
    def capture_released(self):
        worker = self.speech_worker
        return worker is None or bool(getattr(worker, 'capture_released', not worker.isRunning()))

    def bind_reply(self, chat, *, reply_key=None, wake_session=None):
        """Input owns scope authorization; chat only supplies presentation facts."""
        if chat is None:
            return
        scope = reply_key[0] if reply_key is not None else self._response_scope_id
        self._bound_reply = (chat, chat.stream_session_id, scope)
        if wake_session is not None:
            chat.notify_on_current_reply_result(lambda result:
                self.finish_wake_reply(wake_session, result.allows_response))
        elif reply_key is not None:
            chat.notify_on_current_reply_result(lambda result:
                self.finish_response_reply(result.allows_response, reply_key=reply_key))

    def stop_daily_reply(self, scope):
        binding, self._bound_reply = self._bound_reply, None
        if binding is not None:
            chat, generation, bound_scope = binding
            if scope == bound_scope and chat.stream_session_id == generation:
                chat.stop_current()

    def bind_business_reply(self, chat, *, seconds, deadline, scope_id):
        if not self.arm_response_scope(scope_id):
            return
        session = self.voice_session_id
        chat.notify_on_current_reply_result(lambda result:
            self.begin_response_window(seconds, deadline=deadline, scope_id=scope_id)
            if result.allows_response and self.voice_session_id == session else None)

    def set_on_recognized_text(self, on_recognized_text: Callable[[str], None]) -> None:
        self.on_recognized_text = on_recognized_text

    @property
    def listening_enabled(self) -> bool:
        return (self.voice_mode_active or self.wake_enabled or self._response_input_active()
                or self._finishing_response_session is not None)

    def _response_input_active(self):
        return (self._response_preparing or self._response_finishing
                or time.monotonic() < self._response_until or self._daily_utterance_started())

    def _daily_utterance_started(self):
        # The worker can already have returned with its recognized signal queued.
        # Admission follows the captured onset, not GUI timer/signal ordering.
        started = getattr(self.speech_worker, 'speech_started_at', None)
        return (self.daily_active and self._response_until > 0 and started is not None
                and started <= self._response_until)

    @property
    def daily_active(self):
        return self._response_kind == 'daily' and bool(self._response_scope_id)

    @property
    def conversation_active(self):
        """Presentation may remain visible through a granted response window."""
        return (self.daily_active or self.voice_mode_active or self._response_input_active()
                or self._response_pending or self._wake_reply_session is not None)

    @property
    def response_reply_key(self):
        return (self._response_scope_id, self._response_generation) if self._response_pending else None

    def _set_capture_ready(self, ready):
        self.capture_ready = bool(ready)
        self.capture_state_changed.emit()

    def set_suspended(self, suspended):
        self._suspended = bool(suspended)
        if suspended:
            self.end_daily_conversation(stop_reply=False)
            self.interrupt_current_recording()
        else:
            self.schedule_next_recording()

    def set_manual_muted(self, muted: bool):
        muted = bool(muted)
        if muted == self.manual_muted:
            return
        # Revoke admission before notifying the independent device-test owner.
        self.manual_muted = muted
        if muted:
            self.end_daily_conversation(stop_reply=False)
            self.voice_mode_active = False
            self._clear_response_window()
            self._response_scope_id = self._response_kind = ''
            self.interrupt_current_recording()
            self._resume_after_stale_worker = False
            self.set_voice_active(False)
        self.manual_mute_changed.emit(muted)
        self.capture_state_changed.emit()
        if not muted:
            self.schedule_next_recording()

    def end_daily_conversation(self, *, stop_reply=True):
        if not self.daily_active and not self.voice_mode_active:
            return False
        scope = self._response_scope_id if self.daily_active else ''
        if scope:
            self._closed_response_scopes.append(scope)
        self._clear_response_window()
        self._response_scope_id = self._response_kind = ''
        self.voice_mode_active = False
        self.interrupt_current_recording()
        self.set_voice_active(False)
        if stop_reply:
            self.daily_ended.emit(scope)
        self.schedule_next_recording()
        return True

    def use_short_conversation(self):
        """Convert explicit continuous input without discarding its current take."""
        if not self.voice_mode_active:
            return
        self.voice_mode_active = False
        self.arm_response_scope('daily:' + uuid.uuid4().hex, kind='daily')
        self._response_seconds = 8.
        self._response_deadline = None
        if self.is_conversation_busy():
            self._response_pending = True
        elif self.is_capturing_user_speech() or (
            self._worker_session_id == self.voice_session_id
            and getattr(self.speech_worker, 'speech_started_at', None) is not None
        ):
            # A completed worker can still have its recognition queued for the
            # GUI. Keep this take's admission, without reviving a stale session.
            self._response_finishing = True
        elif self.capture_ready:
            self._response_until = time.monotonic() + 8.
            self._response_timer.start(8000)
        else:
            self.begin_response_window(8, scope_id=self._response_scope_id)

    def _clear_response_window(self):
        active = (self._response_input_active() or self._response_pending
                  or self._finishing_response_session is not None)
        self._response_timer.stop()
        self._response_generation += 1
        self._response_preparing = False
        self._response_until = 0.
        self._response_pending = self._response_finishing = False
        self._finishing_response_session = None
        if active and not self.voice_mode_active:
            self.set_voice_active(False)

    def arm_response_scope(self, scope_id, *, kind='business'):
        if scope_id and scope_id in self._closed_response_scopes:
            return False
        if self.daily_active and scope_id != self._response_scope_id:
            self._closed_response_scopes.append(self._response_scope_id)
        self._clear_response_window()
        self._response_scope_id = scope_id
        self._response_kind = kind
        if kind == 'business':
            self.voice_mode_active = False
        return True

    def end_response_scope(self, scope_id, *, preserve_inflight=False):
        if scope_id and scope_id not in self._closed_response_scopes:
            self._closed_response_scopes.append(scope_id)
        if self._response_scope_id != scope_id:
            return
        finishing = (preserve_inflight and self._response_input_active()
                     and self.is_capturing_user_speech())
        self._clear_response_window()
        self._response_scope_id = ''
        self._response_kind = ''
        if not self.voice_mode_active:
            if finishing:
                # Keep exactly this utterance, without giving its reply the
                # authority to reopen the now-closed business response scope.
                self._finishing_response_session = self.voice_session_id
                self.set_voice_active(True)
            else:
                self.interrupt_current_recording()
                self.schedule_next_recording()

    def begin_response_window(self, seconds, *, deadline=None, scope_id=None):
        """Temporary ASR after confirmed speech; keep the user's voice preference."""
        if scope_id is not None and (scope_id != self._response_scope_id or scope_id in self._closed_response_scopes):
            return
        if not math.isfinite(seconds) or not 0 < seconds <= 60:
            return
        self._clear_response_window()
        if deadline is not None:
            if not math.isfinite(deadline) or deadline <= time.monotonic():
                return
            seconds = min(seconds, deadline-time.monotonic())
        if not self.backend_ready() or not self._is_capture_allowed():
            return
        self.interrupt_current_recording()
        self._response_seconds = seconds
        self._response_deadline = deadline
        if self.daily_active:
            self._response_preparing = True
            # Same preparation bound as the existing recorder's first-speech wait.
            self._response_timer.start(4000)
        else:
            self._response_until = time.monotonic()+seconds
            self._response_timer.start(round(seconds*1000))
            self.set_voice_active(True)
        self.schedule_next_recording(0)

    def _expire_response_window(self):
        if self._response_preparing:
            self.capture_error = '麦克风准备超时，未开始本次接话。'
            self.set_dialogue_text(self.capture_error)
            self.end_daily_conversation(stop_reply=False)
            return
        remaining = self._response_until - time.monotonic()
        if remaining > 0:
            self._response_timer.start(max(1, math.ceil(remaining * 1000)))
            return
        daily_sentence = self._daily_utterance_started()
        deadline = self._response_until
        self._response_until = 0.
        if self.voice_mode_active:
            return
        started = getattr(self.speech_worker, 'speech_started_at', None)
        if daily_sentence or (self.is_capturing_user_speech() and (started is None or started <= deadline)
                and (self._response_deadline is None or time.monotonic() < self._response_deadline)):
            self._response_finishing = True  # Let an already-started sentence reach ASR.
            if self._response_deadline is not None:
                self._response_timer.start(max(1, round((self._response_deadline-time.monotonic())*1000)))
            return
        if self.daily_active:
            self.end_daily_conversation(stop_reply=False)
            return
        self._response_finishing = False
        self.interrupt_current_recording()
        self.set_voice_active(False)
        self.schedule_next_recording()

    def set_response_reply_window(self, seconds):
        """Use the business owner's one chosen gap for this pending reply."""
        if self._response_pending and math.isfinite(seconds) and 0 < seconds <= 60:
            self._response_seconds = seconds

    def finish_response_reply(self, completed=True, *, reply_key=None):
        if reply_key is not None and reply_key != self.response_reply_key:
            return
        if not completed:
            if self.daily_active:
                self.end_daily_conversation(stop_reply=False)
                self.set_dialogue_text('本次语音未完整播放，已结束接话。')
            else:
                self._clear_response_window()
            return
        if self._response_pending:
            self.begin_response_window(self._response_seconds, deadline=self._response_deadline,
                                       scope_id=self._response_scope_id)

    def configure_wake(self, enabled: bool, words: tuple[str, ...]) -> bool:
        if enabled and (not self.backend_ready() or not words):
            self.set_dialogue_text("请等待对话后端就绪，并填写当前角色的唤醒词。")
            return False
        self._clear_response_window()
        self.interrupt_current_recording()
        self._response_scope_id = self._response_kind = ''
        self.capture_error = ''
        self.wake_enabled = bool(enabled)
        self.wake_words = tuple(words)
        self.wake_enabled_changed.emit(self.wake_enabled)
        self.schedule_next_recording()
        return True

    def wait_for_wake_reply(self) -> int:
        self.arm_response_scope('daily:' + uuid.uuid4().hex, kind='daily')
        self._wake_reply_session = self.voice_session_id
        return self.voice_session_id

    def finish_wake_reply(self, session_id: int, completed: bool) -> None:
        if self._wake_reply_session != session_id or session_id != self.voice_session_id:
            return
        self._wake_reply_session = None
        # The admitted wake reply also covers an explicit click-to-wake. That
        # one short conversation does not require enabling passive KWS.
        if completed and self._is_capture_allowed():
            self.begin_response_window(8, scope_id=self._response_scope_id)
            return
        self.end_daily_conversation(stop_reply=False)
        if not completed:
            self.set_dialogue_text('唤醒应答未完整播放，未开启接话。')
        self.schedule_next_recording()

    def set_stt_port(self, stt_port) -> None:
        """Inject the resident STT adapter (delayed: the host builds it during
        initialize(), after this controller is constructed). Each subsequent
        SpeechWorker gets this same instance by reference -- no per-worker reload."""
        self._stt_port = stt_port

    def set_mic_backend(self, mic_backend: str, *, input_device: str = "") -> None:
        """Inject the resolved mic recorder backend (W3; same delayed-wiring shape
        as set_stt_port). Each subsequent SpeechWorker dispatches on this string."""
        self._mic_backend = mic_backend
        self._input_device = input_device

    def set_device_testing(self, active: bool) -> None:
        self._device_testing = active
        if active:
            self.interrupt_current_recording()
        else:
            self.schedule_next_recording(0)

    def set_capture_allowed_provider(
        self, capture_allowed: Callable[[], bool]
    ) -> None:
        """Install the persistent desktop-presence microphone gate.

        This is deliberately a provider rather than a copied boolean: every
        delayed timer and worker callback re-checks the current presence state.
        """
        self._capture_allowed = capture_allowed

    def _capture_is_blocked(self) -> bool:
        error = getattr(self.speech_worker, "capture_release_error", None)
        if error is not None:
            self._capture_release_error = error
        if self._capture_release_error is not None:
            released = getattr(self._capture_release_error, "released", None)
            if callable(released) and released():
                self._capture_release_error = None
        if self._capture_release_error is None:
            return False
        self.voice_mode_active = False
        self.set_voice_active(False)
        self.set_busy(self.is_conversation_busy())
        self.set_dialogue_text(str(self._capture_release_error))
        return True


    def set_input_device(self, identity: str) -> None:
        self._input_device = identity

    def start(self) -> None:
        if self._capture_is_blocked():
            return
        if self.manual_muted:
            self.set_dialogue_text('麦克风已完全禁用，请先恢复采音。')
            return
        if not self.backend_ready():
            self.set_voice_active(False)
            self.set_dialogue_text("后端未初始化，请检查 OPENAI_API_KEY 和本地依赖。")
            return

        self.interrupt_current_recording()
        self.voice_mode_active = True
        self.set_voice_active(True)
        self.set_busy(self.is_conversation_busy())
        self.maybe_start_recording(self.voice_session_id)

    def stop(self) -> None:
        self._clear_response_window()
        if self.daily_active:
            self._closed_response_scopes.append(self._response_scope_id)
        self._response_scope_id = self._response_kind = ''
        self.voice_mode_active = False
        self.interrupt_current_recording()
        self._resume_after_stale_worker = False
        self.set_voice_active(False)
        self.set_dialogue_text("连续语音已关闭，仍可用唤醒词呼叫。" if self.wake_enabled else "语音模式已关闭。")
        self.set_busy(self.is_conversation_busy())
        self.schedule_next_recording()

    def toggle(self) -> None:
        if (self.voice_mode_active or self._response_input_active() or self._response_pending
                or self._finishing_response_session is not None):
            self.stop()
            return
        self.start()

    def maybe_start_recording(self, session_id: int | None = None) -> None:
        if self._capture_is_blocked():
            return
        if session_id is not None and session_id != self.voice_session_id:
            return
        if not self.listening_enabled or self._wake_reply_session is not None:
            return
        if self._finishing_response_session is not None:
            return
        if not self._is_capture_allowed():
            return
        if self.is_conversation_busy():
            return
        if self.defer_recording_until_current_worker_finishes():
            return
        self._start_speech_worker()

    def schedule_next_recording(self, delay_ms: int = 320) -> None:
        if not self.listening_enabled:
            return
        if not self._is_capture_allowed():
            return
        session_id = self.voice_session_id
        QTimer.singleShot(delay_ms, lambda sid=session_id: self.maybe_start_recording(sid))

    def shutdown(self, wait_ms: int = 1500) -> bool:
        self._capture_is_blocked()
        self._clear_response_window()
        self.voice_mode_active = False
        self.wake_enabled = False
        self._wake_reply_session = None
        self.voice_session_id += 1
        self._resume_after_stale_worker = False
        worker = self.speech_worker
        if worker and worker.isRunning():
            worker.requestInterruption()
            worker.quit()
            if not worker.wait(max(0, int(wait_ms))):
                return False
        self._capture_is_blocked()
        if worker is not None:
            if not getattr(worker, 'capture_released', True):
                return False
            try:
                worker.deleteLater()
            except Exception:
                pass
            self.speech_worker = None
        self._wake_detector = None
        return True

    def interrupt_current_recording(self) -> None:
        self.wake_ready = False
        self._set_capture_ready(False)
        if not self._is_capture_allowed():
            if self.daily_active:
                self._closed_response_scopes.append(self._response_scope_id)
                self._response_scope_id = self._response_kind = ''
            self._clear_response_window()
        self.voice_session_id += 1
        self._finishing_response_session = None
        self._wake_reply_session = None
        self._resume_after_stale_worker = False
        if self.speech_worker and self.speech_worker.isRunning():
            self.speech_worker.requestInterruption()

    def defer_recording_until_current_worker_finishes(self) -> bool:
        """Arm one idle resume after the current worker actually exits.

        ``QThread`` cannot be replaced while the old worker is still running.
        Used after arrival, mode changes and playback completion. Returning
        ``False`` lets the caller start immediately when no worker remains.
        """

        worker = self.speech_worker
        if worker is None or not worker.isRunning():
            return False
        self._resume_after_stale_worker = True
        return True

    def is_capturing_user_speech(self) -> bool:
        """True only while a SpeechWorker has actually detected the user speaking
        (hardware VAD started), NOT while it idly waits for speech. The P3 arbiter
        treats this -- not the mere presence of a running worker -- as "busy", so a
        proactive reaction can fire during the (common) idle-listen gaps yet never
        cuts off a half-spoken sentence (option A: preempt idle, never active)."""
        worker = self.speech_worker
        return bool(
            self._is_capture_allowed()
            and worker is not None
            and worker.isRunning()
            and worker.is_capturing_user_speech()
        )

    def handle_speech_status(self, message: str, session_id: int) -> None:
        if (
            session_id == self.voice_session_id
            and (self.voice_mode_active or self._response_input_active()
                 or self._finishing_response_session == session_id)
            and self._is_capture_allowed()
        ):
            self.set_dialogue_text(message)

    def handle_recognized(self, text: str, session_id: int) -> None:
        if (
            session_id != self.voice_session_id
            or not (self.voice_mode_active or self._response_input_active()
                    or self._finishing_response_session == session_id)
            or not self._is_capture_allowed()
            or self._wake_reply_session is not None
            or self._response_preparing
            or self.is_conversation_busy()
        ):
            return
        text = (text or "").strip()
        if not text:
            if self.daily_active and self._response_finishing:
                self.end_daily_conversation(stop_reply=False)
                return
            self.schedule_next_recording(600)
            return
        command = text.strip().rstrip('。.!！?？').strip()
        if command in {'关闭麦克风', '完全关闭麦克风'}:
            self.set_manual_muted(True)
            return
        if self.daily_active and command in {'结束对话', '先聊到这里', '先不聊了'}:
            self.end_daily_conversation()
            return
        if self._finishing_response_session == session_id:
            self._finishing_response_session = None
            if not self.voice_mode_active:
                self.set_voice_active(False)
        if self._response_input_active():
            self._response_timer.stop()
            self._response_until = 0.
            self._response_finishing = False
            self._response_pending = True
            self._response_generation += 1
        self.on_recognized_text(text)

    def handle_wake_detected(self, keyword: str, session_id: int) -> None:
        if (
            session_id != self.voice_session_id or not self.wake_enabled
            or self.voice_mode_active or self._wake_reply_session is not None
            or not self._is_capture_allowed() or self.is_conversation_busy()
            or keyword not in self.wake_words
        ):
            return
        if self.on_wake_word is not None:
            logger.info("event=desktop_voice_wake_detected engine=sherpa_onnx")
            self.on_wake_word(keyword)

    def request_manual_wake(self, character_name: str) -> bool:
        """An explicit attention gesture uses the same acknowledgement owner."""
        if (not self.backend_ready() or not self._is_capture_allowed()
                or self.conversation_active or self._response_scope_id
                or self.is_conversation_busy() or self.on_wake_word is None):
            return False
        logger.info('event=desktop_voice_wake_requested engine=manual')
        self.on_wake_word(character_name)
        return True

    def handle_error(self, message: str, session_id: int) -> None:
        if (
            session_id != self.voice_session_id
            or not self.listening_enabled
            or not self._is_capture_allowed()
        ):
            return
        fatal = is_fatal_speech_error(message) or message.startswith("唤醒监听不可用：")
        soft = message in {'没有检测到语音输入。', '没有识别到有效中文。'}
        if self.daily_active and (not soft or self._response_finishing):
            self.end_daily_conversation(stop_reply=False)
            if not soft:
                self.capture_error = message
                self.set_dialogue_text(message)
        if self.voice_mode_active or fatal:
            self.set_dialogue_text(message)
        if fatal:
            self.capture_error = message
            self._clear_response_window()
            self.voice_mode_active = False
            self.wake_enabled = False
            self._wake_reply_session = None
            self.wake_enabled_changed.emit(False)
            self.voice_session_id += 1
            self.set_voice_active(False)
            self.set_busy(False)
        if not self.voice_mode_active and not self._response_input_active():
            self.wake_ready = False
        self.capture_state_changed.emit()

    def handle_finished(self, session_id: int) -> None:
        if self._capture_is_blocked():
            return
        if session_id == self.voice_session_id:
            self.wake_ready = False
            self._set_capture_ready(False)
        if self.speech_worker and not self.speech_worker.isRunning():
            if not getattr(self.speech_worker, 'capture_released', True):
                self.capture_error = '采音资源尚未释放，请检查设备连接，退出并重新启动桌面程序。'
                self.set_dialogue_text(self.capture_error)
                self.capture_state_changed.emit()
                return
            self.speech_worker.deleteLater()
            self.speech_worker = None
        resume_after_stale_worker = self._resume_after_stale_worker
        self._resume_after_stale_worker = False
        if session_id != self.voice_session_id:
            # Ducking or departure alone must never re-arm recording.
            if (
                resume_after_stale_worker
                and self.listening_enabled
                and self._is_capture_allowed()
                and not self.is_conversation_busy()
            ):
                self.set_busy(False)
                self.schedule_next_recording(650)
            return
        if self._response_finishing:
            self._response_finishing = False
            self._response_timer.stop()
            if not self.voice_mode_active:
                self.set_voice_active(False)
        if self._finishing_response_session == session_id:
            self._finishing_response_session = None
            if not self.voice_mode_active:
                self.set_voice_active(False)
        if not self.listening_enabled:
            self.set_busy(self.is_conversation_busy())
            return
        if not self._is_capture_allowed():
            self.set_busy(self.is_conversation_busy())
            return
        if self.is_conversation_busy():
            self.set_busy(True)
            return
        self.set_busy(False)
        self.schedule_next_recording(650)

    def _start_speech_worker(self) -> None:
        if self._capture_is_blocked():
            return
        # Re-check at the final creation seam: capture permission may change after a timer
        # callback entered ``maybe_start_recording`` but before worker creation.
        if not self._is_capture_allowed():
            return
        if self.speech_worker is not None and not self.speech_worker.isRunning():
            self.speech_worker.deleteLater()
            self.speech_worker = None
        if self.voice_mode_active:
            self.set_busy(True)
        session_id = self.voice_session_id
        if self.wake_enabled and not self.voice_mode_active and not self._response_input_active():
            self.speech_worker = WakeWordWorker(
                self, detector=self._wake_detector, words=self.wake_words, mic_backend=self._mic_backend,
                **({"input_device": self._input_device} if self._input_device else {}),
            )
            self.speech_worker.wake_detected.connect(
                lambda word, sid=session_id: self.handle_wake_detected(word, sid),
            )
            self.speech_worker.capture_ready.connect(
                lambda sid=session_id: self._wake_capture_ready(sid))
        else:
            self.speech_worker = SpeechWorker(self, stt_port=self._stt_port, mic_backend=self._mic_backend,
                                             **({"input_device": self._input_device} if self._input_device else {}))
            self.speech_worker.capture_ready.connect(
                lambda at, sid=session_id: self.handle_capture_ready(at, sid))
            self.speech_worker.capture_finished.connect(
                lambda sid=session_id: self._set_capture_ready(False) if sid == self.voice_session_id else None)
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
        self._worker_session_id = session_id
        self.speech_worker.start()

    def handle_capture_ready(self, occurred_at, session_id):
        if session_id != self.voice_session_id or not self._is_capture_allowed():
            return
        if self._response_preparing:
            self._response_preparing = False
            self._response_until = occurred_at + self._response_seconds
            self._response_timer.start(max(1, round((self._response_until - time.monotonic()) * 1000)))
        if not self.voice_mode_active and not self._response_input_active():
            return
        self.capture_error = ''
        self.set_voice_active(True)
        self._set_capture_ready(True)

    def _wake_capture_ready(self, session_id):
        if session_id == self.voice_session_id and self._is_capture_allowed() and self.wake_enabled:
            self.wake_ready = True
            self.capture_error = ''
            self.capture_state_changed.emit()

    def _is_capture_allowed(self) -> bool:
        try:
            if (self.speech_worker is not None and not self.speech_worker.isRunning()
                    and not getattr(self.speech_worker, 'capture_released', True)):
                return False
            return (not self.manual_muted and not self._device_testing and not self._suspended
                    and self.backend_ready() and bool(self._capture_allowed()))
        except Exception as exc:
            # A broken capture gate must fail closed: opening a live microphone
            # when capture is forbidden is worse than waiting for the next
            # explicit permission change.
            logger.warning("event=desktop_mic_gate_failed error=%s", exc)
            return False


class ReactionVoiceDuckGate:
    """Real ``VoiceInputGate`` (the full-duplex seam proactive.py reserves): when
    she starts a SYSTEM turn (galgame reaction / song report) it ducks the idly
    listening mic so her own TTS is not captured as the user speaking.

    Resume is FREE: a system turn's completion always runs ``on_chat_done`` ->
    ``schedule_next_recording`` (``_end_stream_playback`` is reached for a played
    AND a swallowed NO_COMMENT turn; an errored turn resumes via ``on_error``), so
    this gate only needs the duck -- adding a resume here would double-start the
    mic. Also ducks passive wake-word listening; disabled microphones are a no-op.

    Duck-typed to ``spica.core.proactive.VoiceInputGate``. ``before_system_speech``
    runs on the reaction engine's worker thread -- exactly like the existing
    ``start_turn`` call on that path -- and only touches ``QThread.requestInterruption``
    (atomic) plus a monotonic session-id bump, so no widget is touched off-GUI.
    """

    def __init__(self, voice_input_controller: VoiceInputController | None) -> None:
        self._vc = voice_input_controller

    def before_system_speech(self) -> None:
        vc = self._vc
        if vc is not None and vc.listening_enabled:
            vc.interrupt_current_recording()

    def after_system_speech(self) -> None:
        return None  # resume rides the existing on_chat_done path (see class docstring)
