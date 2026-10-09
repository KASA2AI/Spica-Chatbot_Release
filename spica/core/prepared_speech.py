"""Unpublished local speech through ChatEngine/run_turn; resources follow delivery."""
from dataclasses import replace
import logging
from pathlib import Path
import tempfile
import threading
import time
import uuid

from spica.core.events import DoneEvent, ErrorEvent, UnitReadyEvent
from spica.core.proactive import compose_system_directive_message, is_no_comment_answer
from spica.runtime.context import TurnRequest
from spica.runtime.scoped_audio import ScopedAudio

logger = logging.getLogger(__name__)


class PreparedSpeech:
    def __init__(self, engine, jobs, directive, *, ttl_seconds, want_audio, source, is_current,
                 material_hint=None, event_binding=None):
        self.id = uuid.uuid4().hex
        self.engine, self.jobs, self.source = engine, jobs, source
        self.cancellation = threading.Event()
        self.request = TurnRequest(user_input=compose_system_directive_message(directive),
            conversation_id='default', interaction_mode='system', evidence_turn_id=self.id,
            cancelled=self.cancellation, want_audio=want_audio and engine.daily_voice_enabled,
            input_source=source, include_user_time_context=False,
            material_hint=material_hint, event_binding=event_binding)
        self.deadline = time.monotonic() + ttl_seconds
        self.role = engine.config.character.model_dump()
        self.is_current = is_current
        self._directory = tempfile.TemporaryDirectory(prefix='spica-prepared-speech-')
        self._lock = threading.RLock()
        self.events, self.answer, self.error = [], '', None
        self.done = threading.Event()
        self.commit = None
        self.context_current = lambda: True
        self.playback_request_id = None
        self.started = self.settled = self._settling = self.closed = False
        deps = replace(engine.deps, config=engine.config.model_copy(deep=True))
        self.deps = replace(deps, defer_prepared_turn=self._freeze,
            tts=ScopedAudio(deps.tts, self._directory.name) if deps.tts is not None else None)

    def _freeze(self, commit, current):
        self.commit, self.context_current = commit, current

    def current(self, *, check_memory=False):
        try:
            return (not self.closed and not self.cancellation.is_set()
                and self.role == self.engine.config.character.model_dump()
                and (self.started or time.monotonic() < self.deadline)
                and (self.is_current is None or self.is_current())
                and (not check_memory or self.context_current()))
        except Exception:
            return False

    @property
    def ready(self):
        if (not self.done.is_set() or self.error or not self.answer.strip()
                or is_no_comment_answer(self.answer) or self.commit is None or not self.current()):
            return False
        units = [event for event in self.events if isinstance(event, UnitReadyEvent)]
        return (not self.request.want_audio or bool(units)
            and all(event.audio_path and Path(event.audio_path).is_file() and not event.audio_error for event in units))

    def start(self):
        def collect():
            try:
                for event in self.engine.stream_request(self.request, deps_snapshot=self.deps):
                    if not self.current():
                        self.cancellation.set()
                    if isinstance(event, ErrorEvent):
                        self.error = event.message
                    if isinstance(event, DoneEvent):
                        self.answer = event.answer
                    self.events.append(event)
            except Exception as exc:
                self.error = str(exc)
            finally:
                self.done.set()
                if not self.current():
                    self.cancel()
        self.jobs.submit(collect)

    def bind(self, request_id):
        with self._lock:
            if self.playback_request_id is not None or not self.ready or not self.current(check_memory=True):
                raise RuntimeError('prepared speech is no longer available')
            self.playback_request_id = request_id

    def replay(self, request_id, cancelled):
        if request_id != self.playback_request_id:
            raise PermissionError('unregistered prepared presentation')
        for event in tuple(self.events):
            if cancelled.is_set():
                return
            if not self.current(check_memory=True):
                raise RuntimeError('prepared speech context changed before delivery')
            yield event.to_legacy_dict()

    def settle(self, *, delivered, request_id=None, outcome='completed', audio_played=None):
        """The playback owner calls this only after releasing the audio files."""
        with self._lock:
            if self.settled:
                return
            self.settled = self._settling = True
            turn_id = request_id or self.playback_request_id or self.id
        def write():
            try:
                if delivered and self.commit is not None:
                    self.commit(turn_id)
                    self.engine.record_presentation(turn_id, outcome="cancelled" if outcome == "stopped" else outcome)
            except Exception:
                logger.exception('prepared speech presentation evidence could not be saved')
            finally:
                with self._lock:
                    self.playback_request_id = None
                    self._settling = False
                self.cancel()
        self.jobs.submit(write)

    def cancel(self):
        with self._lock:
            self.cancellation.set()
            if self.done.is_set() and self.playback_request_id is None and not self._settling and not self.closed:
                self._directory.cleanup()
                self.events.clear()
                self.closed = True
