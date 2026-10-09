"""Live wake playback and its optional closing reaction, owned by HomeAlarms."""
from dataclasses import dataclass
import threading

from spica.core.proactive import ProactiveTurnRequest
from spica.ports.conversation import MaterialHint


@dataclass
class _SpeechAttempt:
    handle: object
    valid: threading.Event
    character: str
    finished_mono: float | None = None
    fallback: bool = False


class WakeRun:
    def __init__(self, clock, wall_clock):
        self.clock, self.wall_clock = clock, wall_clock
        self.attempt = None
        self._last_audio_attempt = None
        self.last_release = None
        self.started_mono = self._next_speech_mono = None
        self.due_mono = None
        self.generation_failures = 0

    def observe_due(self, due_at):
        now = self.wall_clock()
        if self.due_mono is None and now >= due_at:
            self.due_mono = self.clock() - (now - due_at)

    def attach(self, handle, valid, character, *, fallback=False):
        self.attempt = attempt = _SpeechAttempt(handle, valid, character, fallback=fallback)
        self.last_release = handle.released
        # Capture the terminal receipt when it arrives, not at the next Home tick.
        # An already-settled handle has no known end time; do not invent one.
        if not handle.result.done():
            clock = self.clock
            handle.result.add_done_callback(lambda _: setattr(attempt, 'finished_mono', clock()))

    def detach(self):
        attempt, self.attempt = self.attempt, None
        if attempt is not None:
            attempt.valid.clear()  # Revoke before transport cancellation or I/O.
        return attempt

    def capture_first_sound(self, record, attempt=None):
        attempt = self.attempt if attempt is None else attempt
        if attempt is None or not attempt.handle.audio_started.done():
            return False
        started = attempt.handle.audio_started.result()
        if started is None:
            return False
        self._last_audio_attempt = attempt
        if record['first_sound_at'] is not None:
            return False
        self.started_mono = started
        record['first_sound_at'] = self.wall_clock()-(self.clock()-started)
        return True

    def audible_since(self, record, due_at):
        if record['first_sound_at'] is not None and record['first_sound_at'] >= due_at:
            return True
        attempt = self._last_audio_attempt
        if attempt is None:
            return False
        due_mono = self.clock()+due_at-self.wall_clock()
        if attempt.handle.audio_started.result() >= due_mono:
            return True
        if attempt.handle.result.done():
            return attempt.finished_mono is not None and attempt.finished_mono >= due_mono
        return self.clock() >= due_mono

    def elapsed(self, record):
        first = record['first_sound_at']
        if first is None:
            return 0.
        if self.started_mono is None:
            self.started_mono = self.clock()-max(0, self.wall_clock()-first)
        return max(0., self.clock()-self.started_mono)

    def resume(self):
        self._next_speech_mono = self.clock()
        self.due_mono = self.clock()

    def response_gap(self, record, seconds, *, finished_wall=None, finished_mono=None):
        record['next_speech_at'] = (self.wall_clock() if finished_wall is None else finished_wall)+seconds
        self._next_speech_mono = (self.clock() if finished_mono is None else finished_mono)+seconds

    def retry_generation(self, record):
        self.generation_failures = min(4, self.generation_failures+1)
        self.response_gap(record, min(60, 10*2**(self.generation_failures-1)))

    def speech_due(self, record, wall_now):
        if record['first_sound_at'] is None:
            return self.clock()+record['next_speech_at']-wall_now
        if self._next_speech_mono is None:
            self._next_speech_mono = self.clock()
        return self._next_speech_mono

    def response_deadline(self, maximum_seconds):
        return (self.clock() if self.started_mono is None else self.started_mono)+maximum_seconds

    @property
    def has_audio_start(self):
        # Persisted first_sound_at alone must never replay a closing reaction.
        return self._last_audio_attempt is not None


class WakeReaction:
    """One ephemeral, idle-only reply; never restores or changes an occurrence.

    Home ticks wait for actual release. Shared speech owns generation, playback,
    admission and the first-sound deadline. No additional worker or audio owner.
    """
    def __init__(self, directive, previous_release, guard, clock, *, completed_at, event_binding=None):
        self.directive, self.previous_release = directive, previous_release
        self.guard, self.clock = guard, clock
        self.created_at = completed_at
        self.event_binding = event_binding
        self.deadline = self.created_at + 10
        self.handle = None
        self.end_reason = ''
        self.valid = threading.Event()
        self.valid.set()

    def current(self):
        # Called from shared playback too; never acquire the Home lock here.
        return self.valid.is_set() and self.guard()

    @property
    def started(self):
        return (self.handle is not None and self.handle.audio_started.done()
                and self.handle.audio_started.result() is not None)

    def step(self, propose):
        if not self.current():
            self.end_reason = 'context_changed'
            return False
        if self.handle is not None and self.handle.result.done():
            result = self.handle.result.result()
            self.end_reason = f'speech:{result.status}:{result.audio_outcome}'
            return False
        if not self.started and self.clock() >= self.deadline:
            self.end_reason = 'first_sound_deadline'
            return False
        if self.handle is None:
            if not self.previous_release.done():
                return True
            if not self.previous_release.result():
                self.end_reason = 'previous_audio_release_failed'
                return False
            self.handle = propose(ProactiveTurnRequest(
                self.directive, source='home.wake_finished', policy='drop_if_busy',
                material_hint=MaterialHint('home.wake_finished', ''), event_binding=self.event_binding,
                first_sound_deadline=self.deadline, is_current=self.current))
            if self.handle is None:
                self.end_reason = 'speech_unavailable'
                return False
            return True
        return True

    def cancel(self, cancel_speech):
        self.valid.clear()
        if self.handle is not None and not self.handle.result.done():
            cancel_speech(self.handle.proposal_id)
