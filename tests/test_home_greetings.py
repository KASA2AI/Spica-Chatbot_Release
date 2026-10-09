"""Farewell windows, shared quotas and receipt ordering, without real output."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from spica.core.proactive import ProactiveTurnHandle, ProactiveTurnResult
from spica.home.alarm_store import AlarmStore
from spica.home.greetings import HomeGreetings


class Draft:
    def __init__(self, clock, text, kwargs):
        self.id = str(id(self))
        self.answer, self.kwargs, self.clock = text, kwargs, clock
        self.expiry = clock[1]+kwargs['ttl_seconds']
        self.started = self.closed = False
        self.error = None
        self.available = True
        self.settlements = []
    @property
    def ready(self):
        return self.available and self.current()
    def current(self, **kwargs):
        return not self.closed and (self.started or self.clock[1] < self.expiry) and self.kwargs['is_current']()
    def cancel(self):
        self.closed = True
    def settle(self, **kwargs):
        self.settlements.append(kwargs)
        self.cancel()


def setup(tmp_path, at='08:35:00'):
    clock = [datetime.fromisoformat('2026-09-18T'+at+'+08:00').timestamp(), 1000.]
    state = SimpleNamespace(allowed=True, desktop=True, busy=False, role='spica')
    drafts, proposed, cancelled = [], [], []
    store = AlarmStore(tmp_path/'home.sqlite3')
    def prepare(text, **kwargs):
        result = Draft(clock, '路上小心，等你回来。', kwargs)
        drafts.append(result)
        return result
    def propose(request):
        handle = ProactiveTurnHandle(str(len(proposed)))
        handle.admitted.set_result(True)
        proposed.append((request,handle))
        return handle
    args = dict(prepare=prepare, propose=propose, cancel=cancelled.append,
        local_state=lambda:(state.desktop,state.busy), allowed=lambda:state.allowed,
        character=lambda:state.role, clock=lambda:clock[1], wall_clock=lambda:clock[0])
    home = HomeGreetings(store, **args)
    def advance(seconds):
        clock[0] += seconds
        clock[1] += seconds
    return SimpleNamespace(home=home, store=store, args=args, clock=clock, state=state,
        drafts=drafts, proposed=proposed, cancelled=cancelled, advance=advance)


def finish(env, *, start=True, release=True):
    request, handle = env.proposed[-1]
    if not handle.audio_started.done():
        handle.audio_started.set_result(env.clock[1] if start else None)
    handle.result.set_result(ProactiveTurnResult('completed' if start else 'cancelled',
        request_id='actual', presentation_outcome='completed' if start else 'stopped'))
    if release:
        handle.released.set_result(True)
    return handle


def test_welcome_status_uses_playback_receipts_and_keeps_a_later_skip(tmp_path):
    env = setup(tmp_path)
    try:
        env.home.welcome()
        handle = env.proposed[-1][1]
        assert env.home.snapshot()['welcome']['state'] == 'pending'
        handle.audio_started.set_result(env.clock[1])
        assert env.home.snapshot()['welcome']['state'] == 'started'
        handle.result.set_result(ProactiveTurnResult('completed', audio_outcome='completed'))
        env.home.step()
        assert env.home.snapshot()['welcome']['state'] == 'delivered'
        env.home.welcome()
        old = env.proposed[-1][1]
        env.state.busy = True
        env.home.welcome()
        old.audio_started.set_result(env.clock[1])
        old.result.set_result(ProactiveTurnResult('completed', audio_outcome='completed'))
        env.home.step()
        assert env.home.snapshot()['welcome']['state'] == 'skipped'
        assert env.home.snapshot()['welcome']['reason'] == 'busy'
    finally:
        env.home.close()


def test_welcome_status_failure_cannot_interrupt_room_processing(tmp_path):
    env = setup(tmp_path)
    try:
        env.home.welcome()
        handle = env.proposed[-1][1]
        handle.audio_started.set_exception(RuntimeError('receipt unavailable'))
        handle.result.set_exception(RuntimeError('receipt unavailable'))
        env.home.step()
        assert env.home.snapshot()['welcome']['state'] == 'unknown'
        assert env.home.snapshot()['welcome']['reason'] == 'receipt_unavailable'
    finally:
        env.home.close()


def test_preparation_and_unready_opening_do_not_consume_daily_quota(tmp_path):
    env = setup(tmp_path)
    env.home.departing()
    assert not env.proposed and env.store.farewell_for_day('2026-09-18') is None
    env.home.step()
    assert env.drafts and not env.proposed
    env.home.departing()
    assert len(env.proposed) == 1
    draft = env.drafts[-1]
    handle = finish(env, release=False)
    env.home.step()
    assert not draft.closed and env.store.farewell_for_day('2026-09-18')['state'] == 'started'
    handle.released.set_result(True)
    env.home.step()
    assert draft.closed and draft.settlements[0]['delivered']
    env.advance(60)
    env.home.departing()
    assert len(env.proposed) == 1
    restarted = HomeGreetings(AlarmStore(env.store.path), **env.args)
    restarted.departing()
    assert len(env.proposed) == 1
    restarted.close()
    env.home.close()


@pytest.mark.parametrize('started', [False, True])
def test_nine_oclock_withdraws_only_local_speech_that_has_not_started(tmp_path, started):
    env = setup(tmp_path, '08:59:55')
    env.home.step()
    env.home.departing()
    handle = env.proposed[-1][1]
    if started:
        handle.audio_started.set_result(env.clock[1])
    env.advance(6)
    env.home.step()
    assert bool(env.cancelled) is not started
    finish(env, start=started)
    env.home.step()
    env.home.close()








@pytest.mark.parametrize('audio_outcome,proposals', [('not_started', 2), (None, 1)])
def test_definitely_unplayed_farewell_releases_quota_but_unknown_does_not(tmp_path, audio_outcome, proposals):
    env = setup(tmp_path)
    env.home.step()
    env.home.departing()
    handle = env.proposed[-1][1]
    handle.audio_started.set_result(None)
    handle.result.set_result(ProactiveTurnResult('completed', request_id='unplayed',
        presentation_outcome='completed', audio_outcome=audio_outcome))
    handle.released.set_result(True)
    env.home.step()
    env.advance(5)
    env.home.step()
    env.home.departing()
    assert len(env.proposed) == proposals
    env.home.close()
