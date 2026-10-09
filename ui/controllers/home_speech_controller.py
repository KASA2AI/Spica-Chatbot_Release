"""Local Home speech delivery through the existing ChatStreamController.

Home owns durable schedules and sensing. This controller only arbitrates one
utterance, checks its first-sound deadline and reports actual presentation/release.
No model, prompt builder or playback engine is created here.
"""
from concurrent.futures import Future
from dataclasses import dataclass, field
import logging
import threading
import time
import uuid

from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot

from spica.core.prepared_speech import PreparedSpeech
from spica.core.proactive import (ProactiveTurnHandle, ProactiveTurnResult,
    PreparedProactiveTurnRequest, ScheduledProactiveTurnRequest)
from spica.runtime.jobs import ThreadJobRunner

logger = logging.getLogger(__name__)


@dataclass
class _Speech:
    request: object
    handle: ProactiveTurnHandle
    expires: float | None
    audio: dict = field(default_factory=dict)
    terminal: str | None = None
    forced_status: str | None = None
    admitted: bool = False
    released: bool = False


class HomeSpeechController(QObject):
    _proposed = Signal(object)
    _cancelled = Signal(str)
    _scope_closed = Signal(str, bool)
    _reply_cancelled = Signal(str)
    _warm_requested = Signal(object, object, object)

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.chat = window.chat_stream_controller
        self.voice = window.voice_input_controller
        self.engine = window.host.chat_engine
        self.jobs = ThreadJobRunner()
        self._lock = threading.RLock()
        self._closed = False
        self._prepared = None
        self._warming = None
        self._pending = {}
        self._submitted = {}
        self._warm_futures = set()
        self._active = None
        self._state = (True, self._busy())
        self._proposed.connect(self._enqueue, Qt.ConnectionType.QueuedConnection)
        self._cancelled.connect(self._cancel, Qt.ConnectionType.QueuedConnection)
        self._scope_closed.connect(self._close_scope, Qt.ConnectionType.QueuedConnection)
        self._reply_cancelled.connect(self._cancel_reply, Qt.ConnectionType.QueuedConnection)
        self._warm_requested.connect(self._warm, Qt.ConnectionType.QueuedConnection)
        self.chat.delivery_event.connect(self._delivery)
        self.chat.turn_started.connect(self._turn_started)
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def local_state(self):
        return self._state  # Immutable GUI-owned publication; backend never reads widgets.

    def _busy(self):
        return self.window._is_proactive_busy()

    def submit(self, request):
        handle = ProactiveTurnHandle(uuid.uuid4().hex)
        now = time.monotonic()
        item = _Speech(request, handle, now + request.ttl_seconds if request.ttl_seconds is not None else None)
        with self._lock:
            if self._closed:
                self._finish(item, 'unavailable')
            else:
                self._submitted[handle.proposal_id] = item
                try:
                    self._proposed.emit(item)
                except RuntimeError:
                    self._submitted.pop(handle.proposal_id, None)
                    self._finish(item, 'unavailable')
        return handle

    def cancel(self, identity):
        if self._closed:
            return False
        self._cancelled.emit(identity)
        return True

    def close_response_scope(self, scope, *, preserve_inflight=False):
        if not self._closed:
            self._scope_closed.emit(scope, preserve_inflight)

    @Slot(str, bool)
    def _close_scope(self, scope, preserve):
        self.voice.end_response_scope(scope, preserve_inflight=preserve)

    def cancel_reply(self, request):
        if not self._closed:
            self._reply_cancelled.emit(request)

    @Slot(str)
    def _cancel_reply(self, request):
        if self.chat._presentation_turn_id == request:
            self.chat.stop_current()

    @Slot(str)
    def _turn_started(self, identity):
        # Any actual intervening conversation invalidates an unpublished draft.
        with self._lock:
            draft = self._prepared
            if draft is not None and draft.playback_request_id != identity:
                draft.cancel()

    def prepare_speech(self, directive, **options):
        with self._lock:
            if self._closed:
                return None
            previous = self._prepared
            if previous is not None:
                if previous.playback_request_id is not None:
                    return None
                previous.cancel()
                if not previous.done.is_set():
                    return None
            draft = self._prepared = PreparedSpeech(self.engine, self.jobs, directive, **options)
            draft.start()
            return draft

    def prepare_audio(self, warmup, *, revision):
        future = Future()
        with self._lock:
            if self._closed:
                future.set_result('desktop_unavailable')
            else:
                self._warm_futures.add(future)
                self._warm_requested.emit(warmup, revision, future)
        return future

    @Slot(object, object, object)
    def _warm(self, warmup, revision, future):
        with self._lock:
            self._warm_futures.discard(future)
        if self._closed:
            if not future.done():
                future.set_result('desktop_unavailable')
            return
        output = self.window.audio_controller.output_status('home')
        if not output.get('available'):
            future.set_result('audio_preparation_failed')
            return
        tts = self.engine.deps.tts
        def key():
            return (self.engine.config.character.model_dump_json(), id(tts),
                    getattr(tts, 'resource_status', {}).get('generation'), revision, repr(output))
        with self._lock:
            previous = self._warming
            if previous is not None and previous[0] == key() and (
                    not previous[1].done() or previous[1].result() == 'ready'):
                previous[1].add_done_callback(lambda prior: future.set_result(prior.result()))
                return
            if previous is not None and not previous[1].done():
                future.set_result('audio_preparation_unavailable')
                return
            self._warming = (key(), future)
        def run():
            try:
                result = 'ready' if warmup() else 'audio_preparation_failed'
            except Exception:
                logger.exception('Home output preparation failed')
                result = 'audio_preparation_failed'
            with self._lock:
                if self._warming is not None and self._warming[1] is future:
                    self._warming = (key(), future)
            future.set_result(result)
        self.jobs.submit(run)

    @Slot(object)
    def _enqueue(self, item):
        with self._lock:
            self._submitted.pop(item.handle.proposal_id, None)
        if self._closed:
            self._finish(item, 'unavailable')
            return
        key = item.request.source
        prior = self._pending.pop(key, None)
        if prior is not None:
            self._finish(prior, 'superseded')
        if len(self._pending) >= 16:
            self._finish(item, 'busy')
            return
        self._pending[key] = item
        self._tick()

    def _current(self, item):
        try:
            return item.request.is_current is None or item.request.is_current()
        except Exception:
            return False

    def _deadline(self, item):
        wants_audio = item.request.want_audio and (self.engine.daily_voice_enabled
            or isinstance(item.request, ScheduledProactiveTurnRequest)) and self.engine.config.tts.enabled
        return item.request.first_sound_deadline if wants_audio else None

    @Slot(str)
    def _cancel(self, identity):
        for key, item in tuple(self._pending.items()):
            if item.handle.proposal_id == identity:
                self._pending.pop(key)
                self._finish(item, 'cancelled')
                return
        if self._active is not None and self._active.handle.proposal_id == identity:
            self._stop_active('cancelled')

    def _stop_active(self, status):
        item = self._active
        if item is None:
            return
        item.forced_status = status
        if self.chat._presentation_turn_id == item.handle.proposal_id:
            self.chat.stop_current()
        self._finish(item, status, released=False)

    def _finish(self, item, status, *, released=True):
        handle = item.handle
        if not handle.admitted.done():
            handle.admitted.set_result(item.admitted)
        if not handle.audio_started.done():
            handle.audio_started.set_result(None)
        if not handle.result.done():
            audio = None if not item.audio else ('completed' if all(v == 'completed' for v in item.audio.values())
                else 'failed' if any(v == 'failed' for v in item.audio.values())
                else 'not_started' if all(v == 'not_started' for v in item.audio.values()) else 'stopped')
            handle.result.set_result(ProactiveTurnResult(status, request_id=handle.proposal_id,
                turn_id=handle.proposal_id, endpoint_id='desktop',
                answer=self.chat._reply_answer if item.admitted else '',
                presentation_outcome=item.terminal, first_audio_started_at=handle.audio_started.result(),
                audio_outcome=audio,
                generation_failed=item.admitted and self.chat._generation_failed))
        if released and not handle.released.done():
            handle.released.set_result(True)

    @Slot(object)
    def _delivery(self, event):
        runtime = self.window.host.home_runtime
        if runtime is not None and runtime.alarms is not None:
            runtime.alarms.observe_reply(event)
        item = self._active
        if item is None or event.request_id != item.handle.proposal_id:
            return
        if event.kind == 'desktop_audio_playback':
            item.audio[event.unit_index] = event.outcome
            if event.outcome == 'started' and not item.handle.audio_started.done():
                deadline = self._deadline(item)
                if deadline is not None and event.occurred_at > deadline:
                    self._stop_active('expired')
                else:
                    item.handle.audio_started.set_result(event.occurred_at)
        elif event.kind == 'desktop_presentation_terminal':
            item.terminal = event.outcome
            self._finish(item, item.forced_status or ('completed' if event.outcome == 'completed' else 'failed'), released=False)
        elif event.kind == 'desktop_turn_lifecycle_released':
            item.released = True
            if not item.handle.released.done():
                item.handle.released.set_result(True)
            self._active = None

    def _tick(self):
        available = not self._closed and not self.voice.suspended and not self.window._restart_requested
        busy = self._busy()
        self._state = (available, busy)
        now = time.monotonic()
        active = self._active
        if active is not None and not active.handle.result.done():
            deadline = self._deadline(active)
            if not available or not self._current(active):
                self._stop_active('cancelled')
            elif deadline is not None and now >= deadline and not active.handle.audio_started.done():
                self._stop_active('expired')
        for key, item in tuple(self._pending.items()):
            if (not available or not self._current(item) or item.expires is not None and now >= item.expires):
                self._pending.pop(key)
                self._finish(item, 'expired' if available else 'unavailable')
                continue
            deadline = self._deadline(item)
            if deadline is not None and now >= deadline:
                self._pending.pop(key)
                self._finish(item, 'expired')
                continue
            scheduled = isinstance(item.request, ScheduledProactiveTurnRequest)
            if scheduled and now < item.request.due_at:
                continue
            if not scheduled and (busy or self._active is not None):
                self._pending.pop(key)
                self._finish(item, 'busy')
                continue
            if scheduled and now >= item.request.yield_at and busy:
                self.chat.stop_current()
                self.voice.interrupt_current_recording()
                busy = self._busy()
            if busy or self._active is not None or any(w.isRunning() for w in self.chat.retired_chat_workers):
                continue
            self._pending.pop(key)
            self._start(item)
            break

    def _start(self, item):
        request = item.request
        replay = None
        if isinstance(request, PreparedProactiveTurnRequest):
            with self._lock:
                draft = self._prepared
                if draft is None or draft.id != request.speech_id:
                    self._finish(item, 'expired')
                    return
                try:
                    draft.bind(item.handle.proposal_id)
                except RuntimeError:
                    self._finish(item, 'expired')
                    return
                replay = draft.replay
        elif getattr(request, 'local_audio_cue', None):
            from spica.runtime.tts_job import local_alarm_events
            replay = lambda identity, cancelled: (event.to_legacy_dict()
                for event in local_alarm_events(request.local_audio_cue, cancelled))
        self._active = item
        try:
            self.voice.interrupt_current_recording()
            self.chat.start_system_turn(request, request_id=item.handle.proposal_id, replay=replay)
            item.admitted = True
            item.handle.admitted.set_result(True)
            if isinstance(request, ScheduledProactiveTurnRequest) and request.response_scope_id:
                self.voice.bind_business_reply(self.chat, seconds=request.response_window_seconds,
                    deadline=request.response_window_deadline, scope_id=request.response_scope_id)
        except Exception:
            logger.exception('Home local speech admission failed')
            self._finish(item, 'failed')
            self._active = None

    def shutdown(self):
        with self._lock:
            self._closed = True
            for item in self._submitted.values():
                self._finish(item, 'cancelled')
            self._submitted.clear()
            for future in self._warm_futures:
                future.set_result('desktop_unavailable')
            self._warm_futures.clear()
        self._state = (False, False)
        for item in self._pending.values():
            self._finish(item, 'cancelled')
        self._pending.clear()
        self._stop_active('cancelled')
        with self._lock:
            if self._prepared is not None:
                self._prepared.cancel()
        self.chat._poll_released()
        clean = self._active is None and not self.jobs.pending
        if clean:
            self._timer.stop()
        return clean
