"""H2 business transitions with explicit synthetic time, vision and audio receipts."""
from concurrent.futures import Future
from datetime import datetime
from types import SimpleNamespace

import pytest

from spica.config.home import HomeConfig, HomeWakeConfig
from spica.core.proactive import ProactiveTurnHandle, ProactiveTurnResult
from spica.home.alarm_store import AlarmSchedule, AlarmStore
from spica.home.alarms import HomeAlarms, WakeObservation
from spica.home.models import RoomObservation, TrackedPerson


class Clock:
    def __init__(self):
        self.at = datetime.fromisoformat('2026-09-18T08:28:50+08:00').timestamp()
        self.mono = 1000.
    def advance(self, seconds):
        self.at += seconds
        self.mono += seconds


class Speech:
    def __init__(self, clock):
        self.clock, self.calls, self.cancelled = clock, [], []
    def submit(self, request):
        handle = ProactiveTurnHandle(str(len(self.calls)))
        handle.admitted.set_result(True)
        self.calls.append((request, handle))
        return handle
    def cancel(self, identity):
        self.cancelled.append(identity)
        handle = self.calls[int(identity)][1]
        if not handle.audio_started.done():
            handle.audio_started.set_result(None)
        if not handle.result.done():
            handle.result.set_result(ProactiveTurnResult('cancelled', audio_outcome='stopped'))
    def finish(self, status='completed', audio='completed'):
        handle = self.calls[-1][1]
        if not handle.audio_started.done():
            handle.audio_started.set_result(self.clock.mono if audio == 'completed' else None)
        if not handle.result.done():
            handle.result.set_result(ProactiveTurnResult(status, audio_outcome=audio))


def ready_output():
    future = Future()
    future.set_result('ready')
    return future


def setup(tmp_path, *, daily=True, at=None):
    clock = Clock()
    if at is not None:
        clock.at = datetime.fromisoformat(at).timestamp()
    speech = Speech(clock)
    role = ['spica']
    config = HomeConfig(daily_detection_enabled=daily,
        wake=HomeWakeConfig(enabled=True, initial_volume=.65, maximum_volume=.95,
                            leave_bed_seconds=5, leave_bed_min_frames=10, response_window_min_seconds=8))
    store = AlarmStore(tmp_path/'alarms.sqlite3')
    kwargs = dict(propose_speech=speech.submit, cancel_speech=speech.cancel,
                  prepare_output=ready_output, active_character=lambda: role[0],
                  clock=lambda: clock.mono, wall_clock=lambda: clock.at)
    alarms = HomeAlarms(config, store, **kwargs)
    with alarms.conversation('create', 'owner'):
        schedule = alarms.create(local_time='08:30', timezone='Asia/Shanghai', weekdays=[0,1,2,3,4])
    return SimpleNamespace(clock=clock, speech=speech, role=role, config=config, store=store,
                           alarms=alarms, kwargs=kwargs, schedule=schedule)


def room(location='bed', *, at=1000., track_id=1, identity='owner'):
    people = () if location in {'unknown', 'empty'} else (TrackedPerson(track_id, (.1,.2,.2,.6), location, identity),)
    return RoomObservation(people, location == 'desk', at, 'camera',
        'no_fresh_frame' if location == 'unknown' else 'observed' if people else 'no_person',
        None if location == 'unknown' else location == 'bed',
        None if location == 'unknown' else location in {'desk', 'outside'})


def drive(env, seconds, *, location='bed', finish=True, track_id=1, identity='owner'):
    for _ in range(round(seconds*2)):
        env.clock.advance(.5)
        owner = room(location, at=env.clock.at, track_id=track_id, identity=identity)
        env.alarms.step(owner, env.clock.mono, 1)
        if finish and env.speech.calls and not env.speech.calls[-1][1].result.done():
            env.speech.finish()


def current(env):
    return min(env.alarms.instances.values(), key=lambda value: value['due_at'])


def test_unready_role_uses_same_alarm_ringtone_and_actual_first_sound(tmp_path):
    env = setup(tmp_path)
    env.alarms.fallback_enabled = True
    def failed_output():
        future = Future()
        future.set_result('audio_preparation_failed')
        return future
    env.alarms.prepare_output = failed_output
    drive(env, 75, finish=False)
    request, handle = env.speech.calls[-1]
    assert request.local_audio_cue == 'home_wake'
    assert current(env)['first_sound_at'] is None
    started = env.clock.at
    env.speech.finish()
    handle.released.set_result(True)
    drive(env, .5, finish=False)
    assert current(env)['first_sound_at'] == started
    env.alarms.prepare_output = ready_output
    drive(env, 9, finish=False)
    assert env.speech.calls[-1][0].local_audio_cue is None
    assert current(env)['first_sound_at'] == started
    env.alarms.close()


def test_failed_role_attempt_reserves_time_and_releases_before_fallback(tmp_path):
    env = setup(tmp_path)
    env.alarms.fallback_enabled = True
    drive(env, 76, finish=False)
    request, handle = env.speech.calls[-1]
    assert request.local_audio_cue is None and request.first_sound_deadline == request.due_at + 13
    env.speech.finish(status='failed', audio='not_started')
    drive(env, .5, finish=False)
    assert len(env.speech.calls) == 1, 'Native/presentation release must precede fallback.'
    handle.released.set_result(True)
    drive(env, .5, finish=False)
    fallback = env.speech.calls[-1][0]
    assert fallback.local_audio_cue == 'home_wake'
    assert fallback.first_sound_deadline == request.due_at + 15
    assert fallback.volume == pytest.approx(request.volume * .22)
    assert current(env)['first_sound_at'] is None
    env.alarms.close()


def test_stop_button_ends_whole_wake_group_and_survives_reload(tmp_path):
    env = setup(tmp_path)
    with env.alarms.conversation('overlap', 'home-local-control'):
        env.alarms.manage(action='add_temporary', arguments={'due_at': current(env)['due_at']})
    drive(env, 77)
    cancelled, closed = [], []
    env.alarms.cancel_reply = cancelled.append
    env.alarms.close_response_scope = lambda scope, **kw: closed.append((scope, kw))
    with env.alarms.conversation('reply', 'desktop', embodied=True):
        pass
    scopes = {env.alarms._response_scope(v) for v in env.alarms.instances.values()
              if v['due_at'] <= env.clock.at}
    with env.alarms.conversation('button', 'home-local-control', want_audio=False):
        result = env.alarms.manage(action='stop_current', arguments={'requested_at': env.clock.at})
    assert len(result['stopped']) == 2
    assert cancelled == ['reply'] and closed
    assert {scope for scope, _ in closed} == scopes
    assert not env.alarms.wake_active()
    count = len(env.speech.calls)
    drive(env, 20)
    assert len(env.speech.calls) == count
    assert all(v['state'] == 'cancelled' and v['reason'] == 'owner_stopped'
               for v in env.alarms.instances.values() if v['due_at'] <= env.clock.at)
    assert any(v['state'] == 'scheduled' for v in env.alarms.instances.values())
    env.alarms.close()
    env.alarms = HomeAlarms(env.config, env.store, **env.kwargs)
    drive(env, 20)
    assert len(env.speech.calls) == count
    env.alarms.close()


def test_delayed_stop_button_does_not_cancel_a_later_alarm(tmp_path):
    env = setup(tmp_path)
    requested_at = env.clock.at
    drive(env, 77)
    with env.alarms.conversation('old-button', 'home-local-control', want_audio=False):
        result = env.alarms.manage(action='stop_current', arguments={'requested_at': requested_at})
    assert not result['stopped'] and current(env)['state'] == 'calling'
    env.alarms.close()


def test_stop_button_revokes_wake_even_when_transport_cancellation_fails(tmp_path):
    env = setup(tmp_path)
    drive(env, 76, finish=False)
    request, _ = env.speech.calls[-1]
    def broken_cancel(_):
        raise OSError('desktop disconnected')
    env.alarms.cancel_speech = broken_cancel
    with env.alarms.conversation('button', 'home-local-control', want_audio=False):
        env.alarms.manage(action='stop_current', arguments={'requested_at': env.clock.at})
    assert not request.is_current()
    assert env.store.get(current(env)['id'])['state'] == 'cancelled'
    count = len(env.speech.calls)
    drive(env, 20)
    assert len(env.speech.calls) == count
    env.alarms.close()


def test_stop_after_clock_rollback_still_cancels_current_group_not_future(tmp_path):
    env = setup(tmp_path)
    with env.alarms.conversation('overlap', 'home-local-control'):
        env.alarms.manage(action='add_temporary', arguments={'due_at': current(env)['due_at']})
    drive(env, 77)
    due_ids = {v['id'] for v in env.alarms.instances.values() if v['due_at'] <= env.clock.at}
    env.clock.at -= 60
    with env.alarms.conversation('stop-after-correction', 'home-local-control', want_audio=False):
        result = env.alarms.manage(action='stop_current', arguments={
            'requested_at': env.clock.at, 'requested_mono': env.clock.mono})
    assert set(result['stopped']) == due_ids
    assert not env.alarms.wake_active()
    assert all(v['state'] == 'scheduled' for v in env.alarms.instances.values() if v['id'] not in due_ids)
    count = len(env.speech.calls)
    drive(env, 10)
    assert len(env.speech.calls) == count
    env.alarms.close()


def test_delayed_monotonic_stop_cannot_cancel_new_wake_after_clock_change(tmp_path):
    env = setup(tmp_path)
    requested_at, requested_mono = env.clock.at, env.clock.mono
    drive(env, 77)
    env.clock.at -= 120
    with env.alarms.conversation('old-button', 'home-local-control', want_audio=False):
        result = env.alarms.manage(action='stop_current', arguments={
            'requested_at': requested_at, 'requested_mono': requested_mono})
    assert not result['stopped'] and current(env)['state'] == 'calling'
    env.alarms.close()


def test_stop_does_not_cancel_unstarted_alarm_moved_into_future(tmp_path):
    env = setup(tmp_path)
    drive(env, 72, finish=False)
    value = current(env)
    assert value['state'] == 'preparing' and value['first_sound_at'] is None
    identity, due_at = value['id'], env.clock.at+1800
    with env.alarms.conversation('reschedule', 'home-local-control'):
        env.alarms.manage(action='move', arguments={'instance_id': identity, 'due_at': due_at})
    with env.alarms.conversation('button', 'home-local-control', want_audio=False):
        result = env.alarms.manage(action='stop_current', arguments={
            'requested_at': env.clock.at, 'requested_mono': env.clock.mono})
    assert not result['stopped']
    assert env.alarms.instances[identity]['state'] == 'scheduled'
    assert env.alarms.instances[identity]['due_at'] == due_at
    env.alarms.close()


@pytest.mark.parametrize('follower_first', [True, False])
def test_failed_stop_save_does_not_leave_other_group_members_authorized(tmp_path, follower_first):
    env = setup(tmp_path)
    with env.alarms.conversation('overlap', 'home-local-control'):
        env.alarms.manage(action='add_temporary', arguments={'due_at': current(env)['due_at']})
    drive(env, 76, finish=False)
    due = [v for v in env.alarms.instances.values() if v['due_at'] <= env.clock.at]
    first = next(v for v in due if bool(v.get('covered_by')) == follower_first)
    env.alarms.instances = {first['id']: first, **env.alarms.instances}
    save, attempts = env.store.save, []
    def fail_first(value):
        if value.get('reason') == 'owner_stopped':
            attempts.append(value['id'])
            # All playback permissions must already be revoked before the first I/O.
            assert all(not request.is_current() for request, _ in env.speech.calls)
            if len(attempts) == 1:
                raise OSError('temporary storage outage')
        return save(value)
    env.store.save = fail_first
    with env.alarms.conversation('button', 'home-local-control', want_audio=False):
        result = env.alarms.manage(action='stop_current', arguments={'requested_at': env.clock.at})
    assert set(result['stopped']) == {v['id'] for v in due}
    assert result['persistence_failed'] == [first['id']]
    assert len(attempts) == 2 and all(v['state'] == 'cancelled' for v in due)
    env.store.save = save
    count = len(env.speech.calls)
    drive(env, 20)
    assert len(env.speech.calls) == count
    assert not env.alarms._pending_stop_saves
    assert all(env.store.get(v['id'])['state'] == 'cancelled' for v in due)
    env.alarms.close()


def test_terminal_scope_exists_without_speech_and_new_reply_role_survives_reload(tmp_path):
    env = setup(tmp_path/'silent')
    outcomes = []
    env.alarms.record_outcome = lambda value, at: outcomes.append(value)
    drive(env, 78, location='outside')
    assert not env.speech.calls and current(env)['state'] == 'completed'
    assert [value['character_id'] for value in outcomes] == ['spica']
    env = setup(tmp_path/'reply')
    drive(env, 78)
    env.role[0] = 'sana'
    with env.alarms.conversation('reply-as-sana', 'default', user_text='再让我抱一会。', character_id='sana', embodied=True) as binding:
        assert binding.event_id == current(env)['id']
    saved = env.store.get(current(env)['id'])
    assert saved['event_characters'] == ['spica', 'sana']
    env.alarms.close()
    env.alarms = HomeAlarms(env.config, env.store, **env.kwargs, record_outcome=lambda value, at: outcomes.append(value))
    drive(env, 8, location='outside')
    assert [value['character_id'] for value in outcomes[-2:]] == ['spica', 'sana']




def test_outcome_acknowledgement_defers_on_database_lock_without_delaying_close(tmp_path):
    import threading
    entered, stopped = threading.Event(), threading.Event()
    class Receipt(Future):
        def result(self, timeout=None):
            entered.set()
            return super().result(timeout)
    env, receipt = setup(tmp_path), Receipt()
    env.alarms.record_outcome = lambda value, at: receipt
    drive(env, 78, location='outside')
    assert current(env)['state'] == 'completed'
    receipt.set_result(1)
    env.clock.advance(1)
    with env.store.db() as locked:
        locked.execute('BEGIN IMMEDIATE')
        tick = threading.Thread(target=lambda: env.alarms.step(room('outside', at=env.clock.at), env.clock.mono, 1))
        tick.start()
        assert entered.wait(1)
        def close():
            env.alarms.close()
            stopped.set()
        closing = threading.Thread(target=close)
        closing.start()
        try:
            assert stopped.wait(.5), 'STOP must not wait for a memory acknowledgement write lock'
        finally:
            locked.rollback()
            tick.join(2)
            closing.join(2)
    assert env.store.pending_outcomes(), 'the deferred acknowledgement remains recoverable'


def test_due_light_is_claimed_once_and_first_speech_waits_for_a_post_press_frame(tmp_path):
    env = setup(tmp_path)
    value, presses = current(env), []
    def press(claim, *, deadline=None):
        claim()
        assert env.store.get(value['id'])['light_requested_at'] == env.clock.at
        presses.append(env.clock.at)
        return 'requested'
    env.alarms.prepare_light = env.kwargs['prepare_light'] = press
    env.clock.advance(69)
    env.alarms.prepare_environment()
    assert not presses and not env.alarms.camera_required()
    env.clock.advance(1)
    env.alarms.prepare_environment()
    assert presses == [value['due_at']] and env.alarms.camera_required()
    env.alarms.step(room('empty', at=env.clock.at-1), env.clock.mono, 1, environment_ready=False)
    env.alarms.close()
    env.alarms = HomeAlarms(env.config, env.store, **env.kwargs)
    env.alarms.prepare_environment()
    env.alarms.step(room('empty', at=env.clock.at-1), env.clock.mono, 1)
    env.clock.advance(.5)
    env.alarms.step(room('empty', at=env.clock.at-1), env.clock.mono, 1)
    assert not env.speech.calls and len(presses) == 1
    env.clock.advance(.5)
    env.alarms.step(room('empty', at=env.clock.at), env.clock.mono, 1)
    env.clock.advance(.5)
    env.alarms.step(room('empty', at=env.clock.at), env.clock.mono, 1)
    assert not env.speech.calls
    drive(env, 4, location='empty')
    assert not env.speech.calls
    drive(env, 1, location='empty')
    assert len(env.speech.calls) == 1  # Covered/undetected people never need target binding.
    env.alarms.close()


@pytest.mark.parametrize('reset_seconds', [2, 16])
def test_light_reset_keeps_one_durable_claim_and_rechecks_deadline_before_press(tmp_path, reset_seconds):
    env = setup(tmp_path)
    value, presses = current(env), []
    def reset_then_press(claim, *, deadline):
        assert claim() is not False
        reserved = env.store.get(value['id'])['light_requested_at']
        env.clock.advance(reset_seconds)
        allowed = claim()
        assert env.store.get(value['id'])['light_requested_at'] == reserved
        if allowed is False:
            return 'reset_unconfirmed'
        presses.append(env.clock.at)
        return 'requested'
    env.alarms.prepare_light = reset_then_press
    env.clock.advance(70)
    env.alarms.prepare_environment()
    env.alarms.prepare_environment()
    if reset_seconds == 16:
        assert not presses and value['light_status'] == 'reset_unconfirmed'
        env.alarms.step(room('empty', at=env.clock.at), env.clock.mono, 1)
        assert value['state'] == 'paused' and not env.speech.calls
    else:
        assert presses == [value['due_at']+2] and value['light_ready_at'] == presses[0]
        env.alarms.step(room('empty', at=value['due_at']+1), env.clock.mono-1, 1)
        assert env.alarms._observations[value['id']].outside_frames == 0
        drive(env, 6, location='empty')
        assert len(env.speech.calls) == 1
    env.alarms.close()


@pytest.mark.parametrize('light_result', ['not_ready', 'reset_unconfirmed', 'requested'])
def test_failed_environment_still_starts_voice_and_recovered_vision_can_end_it(tmp_path, light_result):
    env = setup(tmp_path)
    attempts = []
    def press(claim, *, deadline=None):
        if light_result != 'not_ready':
            assert claim() is not False
        attempts.append(True)
        return light_result
    env.alarms.prepare_light = press
    env.clock.advance(70)
    for _ in range(20):
        env.alarms.prepare_environment()
        drive(env, .5, location='unknown')
    value = current(env)
    assert value['first_sound_at'] is not None
    assert value['first_sound_at'] < value['due_at']+15
    assert value['state'] == 'calling' and value['startup_mode'] == 'voice_only'
    assert len(attempts) == 1
    assert '无法确认' in env.speech.calls[0][0].directive
    for _ in range(12):
        env.clock.advance(.5)
        env.alarms.step(room('outside', at=env.clock.at), env.clock.mono, 1, environment_ready=False)
    assert value['state'] == 'calling'  # Invalid capture cannot supply exit evidence.
    drive(env, 6, location='outside')
    assert value['state'] == 'completed' and value['reason'] == 'bed_empty_with_person_outside'
    env.alarms.close()


def test_preparation_reports_changed_device_faults_without_pressing_early(tmp_path):
    env = setup(tmp_path)
    snapshot = dict(light=dict(status='needs_reset', settings={'battery': 1}),
                    camera=dict(device_present=True, calibrated=True, missing_models=[]),
                    audio_endpoint='online')
    env.alarms.check_readiness = lambda: snapshot
    env.alarms.prepare_light = lambda claim, **kwargs: (_ for _ in ()).throw(AssertionError('early press'))
    env.alarms.prepare_environment()
    drive(env, 1)
    value = current(env)
    original = value['preflight']
    assert original['phase'] == 'preparation'
    assert any('复位' in item for item in original['warnings'])
    assert any('1%' in item for item in original['warnings'])
    assert value['output_status'] == 'ready' and not env.speech.calls
    snapshot['light'] = dict(status='ready', settings={'battery': 90})
    drive(env, 31)
    assert not value['preflight']['warnings']
    assert original['checked_at'] < value['preflight']['checked_at']
    assert env.store.get(value['id'])['preflight'] == value['preflight']
    assert not env.alarms.camera_required() and not env.speech.calls
    env.alarms.close()


def test_audio_fault_still_pauses_even_when_environment_falls_back(tmp_path):
    env = setup(tmp_path)
    def unavailable():
        raise RuntimeError('audio offline')
    env.alarms.prepare_output = unavailable
    drive(env, 86, location='unknown')
    value = current(env)
    assert value['state'] == 'paused' and value['first_sound_at'] is None
    assert value['output_status'] == 'audio_preparation_exception:RuntimeError'
    assert not env.speech.calls
    env.alarms.close()


def test_unstarted_audio_failure_survives_late_outside_evidence_and_restart(tmp_path):
    env = setup(tmp_path)
    def unavailable():
        future = Future()
        future.set_result('desktop_unavailable')
        return future
    env.alarms.prepare_output = env.kwargs['prepare_output'] = unavailable
    env.clock.advance(86)
    env.alarms.step(room('unknown', at=env.clock.at), env.clock.mono, 1)
    failed = dict(current(env))
    assert failed['state'] == 'paused'
    assert failed['reason'] == 'output_unconfirmed:first_sound_deadline'
    env.alarms.close()
    env.clock.advance(20*60)
    env.alarms = HomeAlarms(env.config, env.store, **env.kwargs)
    try:
        drive(env, 6, location='outside')
        value = env.store.get(failed['id'])
        assert value['state'] == 'paused' and value['reason'] == failed['reason']
        assert value['revision'] == failed['revision']
        assert value['first_sound_at'] is None and not env.speech.calls
        # An explicit recovery still gives this occurrence a fresh start.
        env.alarms.prepare_output = ready_output
        with env.alarms.conversation('resume-failed-alarm', 'owner'):
            env.alarms.control(action='resume', instance_id=failed['id'])
        drive(env, 6, location='bed')
        assert current(env)['state'] == 'calling' and len(env.speech.calls) == 1
    finally:
        env.alarms.close()


def test_bedtime_returns_and_preserves_specific_wake_readiness_warnings(tmp_path):
    from spica.home.bedtime import HomeBedtime
    env = setup(tmp_path)
    env.alarms.check_readiness = lambda: dict(
        light=dict(status='needs_reset', settings={'battery': 1}),
        camera=dict(device_present=False, calibrated=True), audio_endpoint='desktop_unavailable')
    adapter = SimpleNamespace(boot_id=lambda: 'boot', resume_stamp=lambda: 0)
    env.alarms.bedtime = HomeBedtime(env.config, env.store, adapter,
        wall_clock=lambda: env.clock.at, clock=lambda: env.clock.mono)
    with env.alarms.conversation('bedtime', 'owner'):
        result = env.alarms.prepare_bedtime(stay_awake=True)
    warnings = result['wake_readiness']['warnings']
    assert any('1%' in item for item in warnings)
    assert any('相机设备未找到' in item for item in warnings)
    assert any('音频端当前离线' in item for item in warnings)
    assert env.store.bedtime()['wake_readiness'] == result['wake_readiness']
    assert not env.speech.calls  # Checks never play a test sound.
    env.alarms.close()


@pytest.mark.parametrize('late_start', [False, True])
def test_failed_light_readiness_cannot_press_late_after_first_sound_timeout(tmp_path, late_start):
    env = setup(tmp_path)
    def unexpected(_):
        raise AssertionError('The finger must not execute the expired action')
    env.alarms.prepare_light = unexpected if late_start else lambda claim, **kwargs: 'not_ready'
    env.clock.advance(90 if late_start else 70)
    for _ in range(32):
        env.alarms.prepare_environment()
        env.alarms.step(room('unknown', at=env.clock.at), env.clock.mono, 1)
        env.clock.advance(.5)
    assert current(env)['state'] == 'paused'
    assert len(env.speech.calls) == (0 if late_start else 1)
    assert all(not request.is_current() for request, _ in env.speech.calls)
    env.alarms.prepare_light = unexpected
    env.alarms.prepare_environment()
    env.alarms.close()


def test_unstarted_pause_releases_camera_across_dates_until_explicit_resume(tmp_path):
    env = setup(tmp_path, daily=False)
    env.alarms.prepare_light = env.kwargs['prepare_light'] = lambda claim, **kwargs: 'not_ready'
    env.alarms.prepare_output = lambda: Future()  # Audio, rather than vision, blocks this instance.
    env.clock.advance(70)
    env.alarms.prepare_environment()
    drive(env, 16, location='unknown')
    value = current(env)
    assert value['state'] == 'paused' and value['first_sound_at'] is None
    assert not env.alarms.camera_required()
    next_due = min(v['due_at'] for v in env.alarms.instances.values() if v['due_at'] > env.clock.at)
    env.alarms.close()
    env.clock.advance(next_due-300-env.clock.at)
    env.alarms = HomeAlarms(env.config, env.store, **env.kwargs)
    assert env.store.get(value['id'])['state'] == 'paused'
    assert not env.alarms.camera_required()  # The old pause must not open next morning's camera early.
    with env.alarms.conversation('resume-camera', 'owner'):
        env.alarms.control(action='resume', instance_id=value['id'])
    assert env.alarms.camera_required()
    env.alarms.close()


def test_cancelled_first_utterance_cannot_extend_the_original_first_sound_deadline(tmp_path):
    env = setup(tmp_path)
    drive(env, 76, finish=False)
    env.clock.advance(9)
    env.speech.finish('cancelled', 'not_started')
    env.alarms.step(room('unknown', at=env.clock.at), env.clock.mono, 1)
    assert current(env)['next_speech_at'] == current(env)['due_at']
    env.clock.advance(9)
    env.alarms.step(room('unknown', at=env.clock.at), env.clock.mono, 1)
    assert current(env)['state'] == 'paused' and len(env.speech.calls) == 1
    env.alarms.close()


def test_terminal_before_reply_scope_does_not_leave_an_interaction_pending(tmp_path):
    env = setup(tmp_path)
    drive(env, 77)
    count = len(env.speech.calls)
    env.alarms.observe_reply(SimpleNamespace(kind='desktop_presentation_terminal', request_id='stopped-reply'))
    with env.alarms.conversation('stopped-reply', 'owner', embodied=True):
        drive(env, 20)
    assert 'stopped-reply' not in env.alarms._replies and len(env.speech.calls) > count
    env.alarms.close()


def test_reply_terminal_callback_does_not_wait_for_the_home_business_lock(tmp_path):
    import threading
    env = setup(tmp_path)
    delivered = threading.Event()
    def notify():
        env.alarms.observe_reply(SimpleNamespace(kind='desktop_presentation_terminal', request_id='reply'))
        delivered.set()
    thread = threading.Thread(target=notify)
    try:
        with env.alarms._lock:
            thread.start()
            assert delivered.wait(1), 'Driver terminal callback must not block Home cancellation'
    finally:
        thread.join(1)
        env.alarms.close()
    assert not thread.is_alive()


def test_weekday_instances_timezone_and_completed_date_are_durable(tmp_path):
    env = setup(tmp_path)
    friday = current(env)
    assert datetime.fromtimestamp(friday['wake_at']).timestamp() == friday['due_at']-300
    assert friday['camera_at'] == friday['due_at']
    drive(env, 76, location='desk')
    assert friday['state'] == 'completed'
    assert not env.speech.calls  # Already occupied outside: skip, without claiming the owner woke.
    env.alarms.close()
    restarted = HomeAlarms(env.config, env.store, **env.kwargs)
    assert all(value['id'] != friday['id'] for value in restarted.instances.values())
    next_instance = min(restarted.instances.values(), key=lambda value: value['due_at'])
    assert next_instance['day'] == '2026-09-21'
    assert env.store.get(friday['id'])['reason'] == 'outside_occupied_at_start'
    restarted.close()


def test_volume_ramps_for_three_minutes_even_when_vision_is_unknown(tmp_path):
    env = setup(tmp_path)
    drive(env, 76, location='empty')
    drive(env, 190, location='unknown')
    volumes = [request.volume for request, _ in env.speech.calls]
    assert volumes[0] == .65 and volumes[-1] == pytest.approx(.95)
    assert volumes == sorted(volumes) and max(volumes) <= .95
    first = current(env)['first_sound_at']
    for request, _ in env.speech.calls:
        seconds = request.due_at-(1000+first-datetime.fromisoformat('2026-09-18T08:28:50+08:00').timestamp())
        if 0 <= seconds < 180:
            assert request.volume < .95
        assert request.response_window_seconds == 8
    assert current(env)['state'] == 'calling'
    env.alarms.close()


@pytest.mark.parametrize('status,audio', [('completed', 'not_started'), ('silent', 'not_started'), ('failed', 'failed')])
def test_subtitles_or_failed_audio_pause_without_escalation(tmp_path, status, audio):
    env = setup(tmp_path)
    drive(env, 76, finish=False)
    env.speech.finish(status, audio)
    drive(env, 1, finish=False)
    value = current(env)
    assert value['state'] == 'paused'
    assert value['reason'].startswith('output_unconfirmed')
    assert value['level'] == 0
    drive(env, 65)
    assert len(env.speech.calls) == 1
    env.alarms.close()


@pytest.mark.parametrize('generation_failed', [True, False])
def test_later_generation_failure_retries_but_output_failure_stays_paused(tmp_path, generation_failed):
    env = setup(tmp_path)
    try:
        drive(env, 76, finish=False)
        env.speech.finish()
        drive(env, 9, finish=False)
        assert len(env.speech.calls) == 2
        handle = env.speech.calls[-1][1]
        handle.audio_started.set_result(None)
        handle.result.set_result(SimpleNamespace(status='failed', audio_outcome='not_started',
                                                  generation_failed=generation_failed))
        drive(env, 1, finish=False)
        value = current(env)
        if generation_failed:
            assert value['state'] == 'calling'
            assert value['reason'] == 'generation_failed_retry'
            drive(env, 8, finish=False)
            assert len(env.speech.calls) == 2
            drive(env, 2, finish=False)
            assert len(env.speech.calls) == 3
            env.speech.finish()
            drive(env, 600, finish=False)
            assert value['state'] == 'completed' and value['reason'] == 'duration_limit'
        else:
            assert value['state'] == 'paused'
            reason = value['reason']
            drive(env, 600, finish=False)
            assert value['state'] == 'paused' and value['reason'] == reason
            assert len(env.speech.calls) == 2
    finally:
        env.alarms.close()


def test_ten_minute_limit_stops_inflight_audio_without_claiming_wake_success(tmp_path):
    env = setup(tmp_path)
    drive(env, 76, location='empty', finish=False)
    request, handle = env.speech.calls[-1]
    handle.audio_started.set_result(env.clock.mono)
    drive(env, 599.5, location='unknown', finish=False)
    assert current(env)['state'] == 'calling' and request.is_current()
    drive(env, .5, location='unknown', finish=False)
    value = current(env)
    assert value['state'] == 'completed' and value['reason'] == 'duration_limit'
    assert not request.is_current() and env.speech.cancelled == ['0']
    assert value['ended_at']-value['first_sound_at'] == 600
    assert 'evidence_frames' not in value
    drive(env, 20, location='unknown')
    assert len(env.speech.calls) == 1
    env.alarms.close()


def test_snooze_and_running_cancel_are_rejected_and_restart_preserves_the_time_limit(tmp_path):
    env = setup(tmp_path)
    drive(env, 80)
    value = current(env)
    first = value['first_sound_at']
    with env.alarms.conversation('snooze', 'owner'):
        with pytest.raises(ValueError, match='不支持延后'):
            env.alarms.control(action='snooze')
        with pytest.raises(ValueError, match='十分钟'):
            env.alarms.control(action='cancel')
        env.alarms.set_enabled(schedule_id=env.schedule['schedule']['id'], enabled=False)
    assert value['state'] == 'calling' and value['first_sound_at'] == first
    env.alarms.close()
    env.alarms = HomeAlarms(env.config, env.store, **env.kwargs)
    assert current(env)['state'] == 'paused'
    with env.alarms.conversation('resume', 'owner'):
        result = env.alarms.control(action='resume')
    assert result['first_sound_at'] == first
    drive(env, 597)
    assert current(env)['state'] == 'completed' and current(env)['reason'] == 'duration_limit'
    assert not env.store.schedules()[0]['enabled']
    env.alarms.close()


@pytest.mark.parametrize('withdrawal', ['close', 'fail', 'resume'])
def test_received_first_sound_survives_withdrawal_before_next_tick(tmp_path, withdrawal):
    env = setup(tmp_path)
    drive(env, 76, location='empty', finish=False)
    value = current(env)
    first = env.clock.at
    env.speech.calls[-1][1].audio_started.set_result(env.clock.mono)
    # No Home tick consumes the received playback fact before this withdrawal.
    if withdrawal == 'fail':
        env.alarms.fail(RuntimeError('output disconnected'))
    elif withdrawal == 'resume':
        with env.alarms.conversation('retry-output', 'owner'):
            env.alarms.control(action='resume', instance_id=value['id'])
    env.alarms.close()
    env.clock.advance(601)
    env.alarms = HomeAlarms(env.config, env.store, **env.kwargs)
    with env.alarms.conversation('resume-after-reload', 'owner'):
        env.alarms.control(action='resume', instance_id=value['id'])
    drive(env, 6.5, location='empty', finish=False)
    restored = env.store.get(value['id'])
    assert restored['state'] == 'completed' and restored['reason'] == 'duration_limit'
    assert restored['first_sound_at'] == first
    assert len(env.speech.calls) == 1
    assert not env.alarms.camera_required()
    env.alarms.close()


def test_first_sound_save_failure_does_not_prevent_cancellation_or_bedtime_cleanup(tmp_path, monkeypatch, caplog):
    env = setup(tmp_path)
    drive(env, 76, finish=False)
    request, handle = env.speech.calls[-1]
    handle.audio_started.set_result(env.clock.mono)
    events = []
    def unavailable_store(value):
        assert not request.is_current() and env.speech.cancelled == ['0']
        events.append('save_failed')
        raise OSError('storage unavailable')
    monkeypatch.setattr(env.store, 'save', unavailable_store)
    env.alarms.bedtime = SimpleNamespace(intent=None, close=lambda: events.append('bedtime_closed'))
    env.alarms.close()
    assert events == ['save_failed', 'bedtime_closed']
    assert 'storage unavailable' in caplog.text


def test_role_change_invalidates_unpresented_speech_and_preserves_current_role(tmp_path):
    env = setup(tmp_path)
    drive(env, 76, finish=False)
    old = env.speech.calls[0][0]
    env.role[0] = 'sana'
    assert not old.is_current()
    drive(env, 2, finish=False)
    assert env.speech.cancelled == ['0']
    assert len(env.speech.calls) == 2
    assert env.speech.calls[-1][0].is_current()
    env.alarms.close()


def test_replayed_stale_and_missing_frames_cannot_complete_getting_up(tmp_path):
    env = setup(tmp_path)
    observation = WakeObservation(env.config)
    owner = room('outside')
    for offset in range(20):
        observation.observe(owner, 1, 1, 1+offset*.1)
    assert not observation.confirmed
    for index in range(11):
        observation.observe(owner, 10+index*.5, 1, 10+index*.5)
    assert observation.confirmed
    observation.observe(owner, None, 1, 18)
    assert not observation.confirmed
    env.alarms.close()


def test_alarm_camera_demand_is_independent_and_reuses_daily_camera(tmp_path):
    from test_home import Camera, MQTTEvents, profile
    from spica.home.runtime import HomeRuntime
    from spica.home.models import SensorObservation
    env = setup(tmp_path)
    camera, mqtt = Camera(), MQTTEvents()
    runtime = HomeRuntime(env.config, mqtt, camera, None, profile(),
                          clock=lambda: env.clock.mono, alarms=env.alarms)
    env.clock.advance(70)
    runtime.step()
    assert camera.reasons == {'wake_alarm'}
    mqtt.events = [('presence', SensorObservation('room', True, env.clock.at, env.clock.mono, env.clock.mono+30))]
    runtime.step()
    assert camera.reasons == {'wake_alarm', 'occupancy'}
    with env.alarms.conversation('cancel', 'owner'):
        env.alarms.control(action='cancel', instance_id=current(env)['id'])
    runtime.step()
    assert camera.reasons == {'occupancy'}
    runtime.close()
    assert camera.closed


def test_schedule_enable_after_restart_restores_only_disabled_future_instances(tmp_path):
    env = setup(tmp_path)
    identity = env.schedule['schedule']['id']
    with env.alarms.conversation('disable', 'owner'):
        env.alarms.set_enabled(schedule_id=identity, enabled=False)
    env.alarms.close()
    env.alarms = HomeAlarms(env.config, env.store, **env.kwargs)
    with env.alarms.conversation('enable', 'owner'):
        env.alarms.set_enabled(schedule_id=identity, enabled=True)
    assert current(env)['state'] == 'scheduled'
    with env.alarms.conversation('cancel-today', 'owner'):
        env.alarms.control(action='cancel', instance_id=current(env)['id'])
    with env.alarms.conversation('enable-again', 'owner'):
        env.alarms.set_enabled(schedule_id=identity, enabled=True)
    assert current(env)['state'] == 'cancelled'
    env.alarms.close()


@pytest.mark.parametrize('text', ['醒了', '再睡一会', '再让我抱一会', '今天心情不好', '停止'])
def test_natural_interaction_keeps_the_deadline_and_waits_for_the_actual_reply(tmp_path, text):
    env = setup(tmp_path)
    drive(env, 77)
    count, first = len(env.speech.calls), current(env)['first_sound_at']
    with env.alarms.conversation('reply', 'owner', user_text=text, embodied=True):
        assert env.alarms.uses_home_audio('reply')
        assert not env.alarms.uses_home_audio('unrelated-request')
        result = env.alarms.control_text(text)
        assert result['kind'] == 'interaction' and .65 <= result['result']['volume'] <= .95
        drive(env, 20)
    drive(env, 5)  # Generation has ended but the user's reply has not finished playing.
    assert len(env.speech.calls) == count
    env.alarms.observe_reply(SimpleNamespace(kind='desktop_presentation_terminal', request_id='reply'))
    drive(env, 7)
    assert not env.alarms.uses_home_audio('reply')
    assert len(env.speech.calls) == count
    drive(env, 2)
    assert len(env.speech.calls) == count+1
    assert current(env)['state'] == 'calling' and current(env)['first_sound_at'] == first
    env.alarms.close()


def test_each_random_response_gap_is_shared_by_speech_and_reply_scheduling(tmp_path, monkeypatch):
    env = setup(tmp_path)
    env.config.wake.response_window_min_seconds = 3
    samples = iter([3., 8., 5.])
    def sample(low, high):
        assert (low, high) == (3, 8)
        return next(samples)
    monkeypatch.setattr('spica.home.alarms.random.uniform', sample)
    try:
        drive(env, 77)
        assert env.speech.calls[0][0].response_window_seconds == 3
        with env.alarms.conversation('reply', 'owner', embodied=True):
            result = env.alarms.control_text('再让我抱一会')
            assert result['result']['response_window_seconds'] == 8
            drive(env, 2)
        finished = env.clock.at
        env.alarms.observe_reply(SimpleNamespace(kind='desktop_presentation_terminal', request_id='reply'))
        drive(env, 7.5)
        assert len(env.speech.calls) == 1
        assert current(env)['next_speech_at'] == finished+8
        drive(env, 1)
        assert len(env.speech.calls) == 2
        assert env.speech.calls[-1][0].response_window_seconds == 5
    finally:
        env.alarms.close()


def test_old_paused_date_does_not_hide_the_current_interaction(tmp_path):
    env = setup(tmp_path)
    drive(env, 76, finish=False)
    env.speech.finish('failed', 'not_started')
    drive(env, 1)
    friday = current(env)
    assert friday['state'] == 'paused'
    env.clock.advance(3*86400-77)
    drive(env, 77)
    monday = next(value for value in env.alarms.instances.values() if value['day'] == '2026-09-21')
    assert monday['state'] == 'calling'
    with env.alarms.conversation('stop-current', 'owner'):
        result = env.alarms.control_text('停止')
    assert result['kind'] == 'interaction'
    assert monday['state'] == 'calling' and friday['state'] == 'paused'
    env.alarms.close()


def test_acknowledgement_does_not_resume_a_settled_audio_failure(tmp_path):
    env = setup(tmp_path)
    drive(env, 76, finish=False)
    env.clock.advance(15)
    env.speech.finish('audio_timeout', 'not_started')
    with env.alarms.conversation('awake-too-late', 'owner'):
        env.alarms.control_text('醒了')
    drive(env, 62)
    assert current(env)['state'] == 'paused'
    assert len(env.speech.calls) == 1
    env.alarms.close()


def test_home_tools_need_personal_scope_and_explicit_power_intent(tmp_path):
    from spica.home.tools import register_home_tools, explicit_shutdown_request
    from spica.plugins.registry import CapabilityRegistry
    env = setup(tmp_path)
    registry = CapabilityRegistry()
    register_home_tools(registry, env.alarms)
    with pytest.raises(PermissionError):
        registry.tool_handler('list_wake_alarms')()
    with env.alarms.conversation('user-query', 'owner', user_text='不要关机'):
        assert registry.tool_handler('list_wake_alarms')()['schedules']
        with pytest.raises(PermissionError):
            registry.tool_handler('prepare_home_bedtime')()
    assert explicit_shutdown_request('晚安，我要睡觉了，关闭电脑')
    assert explicit_shutdown_request('请帮我关机吧')
    for text in ('不要关机', '如果我说关闭电脑', '解释一下“关闭电脑”', '什么时候会关机'):
        assert not explicit_shutdown_request(text)
    env.alarms.close()




@pytest.mark.parametrize('location', ['empty', 'bed'])
def test_no_outside_detection_starts_without_identity_or_track_binding(tmp_path, location):
    env = setup(tmp_path)
    drive(env, 74.5, location=location, track_id=None, identity='unknown')
    assert not env.speech.calls
    drive(env, 1, location=location, track_id=None, identity='unknown')
    assert len(env.speech.calls) == 1
    assert current(env)['state'] == 'calling'
    assert not {'target_bound', 'target_owner_confirmed'}.intersection(current(env))
    drive(env, 20, location='empty')
    drive(env, 20, location='unknown')
    assert current(env)['state'] == 'calling' and len(env.speech.calls) > 1
    env.alarms.close()


@pytest.mark.parametrize('change', ['outside', 'unknown', 'camera_restart'])
def test_first_speech_loses_admission_before_audio_but_keeps_original_deadline(tmp_path, change):
    env = setup(tmp_path)
    # Production always assembles bedtime; its resume counter must never
    # replace the separate camera generation used for frame continuity.
    env.alarms.bedtime = SimpleNamespace(intent=None, resume_generation=0, step=lambda: None,
        suppress_alarm=False, close=lambda: None)
    drive(env, 76, location='empty', finish=False)
    request = env.speech.calls[-1][0]
    assert request.is_current()
    env.clock.advance(.5)
    generation = 2 if change == 'camera_restart' else 1
    observation = room('empty' if change == 'camera_restart' else change, at=env.clock.at)
    env.alarms.step(observation, env.clock.mono, generation)
    assert not request.is_current() and env.speech.cancelled == ['0']
    assert current(env)['first_sound_at'] is None
    if change == 'camera_restart':
        for _ in range(12):
            env.clock.advance(.5)
            env.alarms.step(room('empty', at=env.clock.at), env.clock.mono, generation)
        assert len(env.speech.calls) == 2
        assert env.speech.calls[-1][0].first_sound_deadline == request.first_sound_deadline
    for _ in range(20):
        env.clock.advance(.5)
        env.alarms.step(room('unknown', at=env.clock.at), env.clock.mono, generation)
    assert current(env)['state'] == 'paused'
    assert current(env)['reason'] == 'output_unconfirmed:first_sound_deadline'
    assert len(env.speech.calls) == 2  # Unknown vision now admits a bounded voice-only attempt.
    assert env.speech.calls[-1][0].first_sound_deadline == request.first_sound_deadline
    env.alarms.close()


def test_somebody_outside_before_start_skips_even_with_people_in_bed(tmp_path):
    env = setup(tmp_path)
    env.clock.advance(69.5)
    for _ in range(11):
        env.clock.advance(.5)
        observation = RoomObservation(captured_at=env.clock.at, reason='observed',
                                      bed_occupied=True, outside_bed_occupied=True)
        env.alarms.step(observation, env.clock.mono, 1)
    assert current(env)['state'] == 'completed'
    assert current(env)['reason'] == 'outside_occupied_at_start'
    assert not env.speech.calls
    drive(env, 30, location='empty')
    assert not env.speech.calls  # Do not start late when the friend leaves.
    env.alarms.close()


def test_ending_requires_both_bed_undetected_and_somebody_outside_not_the_same_track(tmp_path):
    env = setup(tmp_path)
    drive(env, 76, track_id=None, identity='unknown')
    value = current(env)
    for _ in range(14):
        env.clock.advance(.5)
        env.alarms.step(RoomObservation(captured_at=env.clock.at, reason='observed',
            bed_occupied=True, outside_bed_occupied=True), env.clock.mono, 1)
    assert value['state'] == 'calling'  # A bed detection vetoes the outside person's presence.
    drive(env, 6, location='empty')
    assert value['state'] == 'calling'  # A covered sleeper/empty room is not an exit.
    # Neither track changes nor a friend's face invalidates the regional rule.
    for index in range(10):
        drive(env, .5, location='outside', track_id=index, identity='other')
    assert value['state'] == 'calling'
    drive(env, .5, location='desk', track_id=None, identity='unknown')
    assert value['state'] == 'completed' and value['reason'] == 'bed_empty_with_person_outside'
    assert value['evidence_seconds'] == 5 and value['evidence_frames'] == 11
    env.alarms.close()


def test_unknown_boundary_camera_restart_and_errors_reset_region_confirmation():
    observation = WakeObservation(HomeConfig())
    outside = room('outside', track_id=None, identity='unknown')
    at = 0.
    for bad_frame, generation in [(RoomObservation(reason='camera_failed'), 1),
                                  (RoomObservation(reason='observed'), 1),
                                  (outside, 2)]:
        for _ in range(6):
            at += .5
            observation.observe(outside, at, observation.generation or 1, at)
        assert not observation.confirmed
        at += .5
        observation.observe(bad_frame, at, generation, at)
        assert not observation.confirmed
    # After a camera restart only the new epoch's real observations count.
    for _ in range(6):
        at += .5
        observation.observe(outside, at, 2, at)
    assert observation.confirmed
    observation.observe(outside, at+1, 2, at)  # A future frame is not evidence.
    assert not observation.confirmed


def test_default_exit_requires_three_seconds_but_start_still_requires_five():
    observation = WakeObservation(HomeConfig())
    outside = room('outside')
    for index in range(7):
        at = index * .5
        observation.observe(outside, at, 1, at)

    assert observation.confirmed
    assert not observation.outside_confirmed(True)
    for index in range(7, 11):
        at = index * .5
        observation.observe(outside, at, 1, at)
    assert observation.outside_confirmed(True)
    observation.reset_evidence()
    for index in range(7):
        at = 10 + index * .5
        observation.observe(room('empty'), at, 1, at)
    assert not observation.outside_confirmed(False)
    for index in range(7, 11):
        at = 10 + index * .5
        observation.observe(room('empty'), at, 1, at)
    assert observation.outside_confirmed(False)


def test_each_alarm_collects_its_own_region_evidence_after_its_due_time(tmp_path):
    env = setup(tmp_path)
    first = current(env)
    with env.alarms.conversation('second-alarm', 'owner'):
        env.alarms.create(local_time='08:31', timezone='Asia/Shanghai', weekdays=[0,1,2,3,4])
    second = min((v for v in env.alarms.instances.values() if v['due_at'] > first['due_at']), key=lambda v:v['due_at'])
    drive(env, 76, location='desk')
    assert first['state'] == 'completed'
    assert second['id'] not in env.alarms.snapshot()['regions']
    env.clock.advance(second['due_at']-env.clock.at-.5)
    drive(env, 5, location='empty')
    assert not env.speech.calls
    drive(env, 1, location='empty')
    assert second['state'] == 'calling' and len(env.speech.calls) == 1
    env.alarms.close()


def test_resume_and_core_restart_discard_old_binding_rules_but_need_new_region_evidence(tmp_path):
    env = setup(tmp_path)
    drive(env, 76, identity='unknown')
    value = current(env)
    # An instance saved by the previous implementation must not require the
    # previous track/face, nor carry a partial exit streak across a restart.
    value.update(target_bound=True, target_owner_confirmed=False)
    env.store.save(value)
    drive(env, 4, location='outside')
    env.alarms.close()
    env.alarms = HomeAlarms(env.config, env.store, **env.kwargs)
    assert current(env)['state'] == 'paused'
    with env.alarms.conversation('resume', 'owner'):
        env.alarms.control(action='resume')
    drive(env, 5, location='outside', track_id=9, identity='other')
    assert current(env)['state'] != 'completed'
    drive(env, .5, location='outside', track_id=None, identity='unknown')
    assert current(env)['reason'] == 'bed_empty_with_person_outside'
    assert not {'track_id', 'appearance'}.intersection(env.store.get(value['id']))
    env.alarms.close()


def test_explicit_bedtime_time_skips_earlier_occurrence_but_preserves_periodic_schedule(tmp_path):
    from test_home_bedtime import power
    from spica.home.bedtime import HomeBedtime
    env = setup(tmp_path)
    earlier = current(env)
    adapter, _, _ = power(tmp_path)
    env.alarms.bedtime = HomeBedtime(env.config, env.store, adapter,
        clock=lambda:env.clock.mono, wall_clock=lambda:env.clock.at)
    with env.alarms.conversation('explicit-0900', 'owner', want_audio=False):
        created = env.alarms.create(local_time='09:00', timezone='Asia/Shanghai', on_date='2026-09-18')
        chosen = next(value for value in env.alarms.instances.values() if value['schedule_id'] == created['schedule']['id'])
        result = env.alarms.prepare_bedtime(instance_id=chosen['id'], reply_required=False)
    assert result['wake_datetime'].endswith('09:00:00+08:00')
    assert earlier['state'] == 'cancelled' and chosen['state'] == 'scheduled'
    drive(env, 100, location='bed')
    assert not env.speech.calls
    periodic = [value for value in env.store.query()['schedules'] if value['id'] == earlier['schedule_id']]
    assert periodic[0]['enabled']
    assert any(value['schedule_id'] == earlier['schedule_id'] and value['state'] == 'scheduled'
               and value['due_at'] > chosen['due_at'] for value in env.alarms.instances.values())
    env.alarms.close()


def test_wake_failure_releases_daily_scene_suppression_and_keeps_first_sound(tmp_path):
    env = setup(tmp_path)
    drive(env, 77)
    value = current(env)
    assert value['first_sound_at'] is not None and env.alarms.wake_active()
    first = value['first_sound_at']
    env.alarms.fail(RuntimeError('controlled output failure'))
    assert value['state'] == 'paused' and value['first_sound_at'] == first
    assert not env.alarms.wake_active()
    env.alarms.close()


@pytest.mark.parametrize('elapsed', [70, 76])
def test_explicit_skip_before_first_sound_works_without_a_bedtime_intent(tmp_path, elapsed):
    from spica.plugins.registry import CapabilityRegistry
    from spica.home.tools import register_home_tools
    env = setup(tmp_path)
    with env.alarms.conversation('later-alarm', 'owner'):
        env.alarms.create(local_time='09:00', timezone='Asia/Shanghai', on_date='2026-09-18')
    drive(env, elapsed, finish=False)
    proposals = len(env.speech.calls)
    registry = CapabilityRegistry()
    register_home_tools(registry, env.alarms)
    with env.alarms.conversation('awake', 'owner', user_text='我已经起床了，本次不用再叫'):
        result = registry.tool_handler('restore_home_daily')()
        assert registry.tool_handler('restore_home_daily')() == result
    assert result['reason'] == 'current_alarm_skipped' and current(env)['state'] == 'cancelled'
    drive(env, 10)
    assert len(env.speech.calls) == proposals and env.store.query()['schedules'][0]['enabled']
    assert env.speech.cancelled == (['0'] if proposals else [])
    assert any(value['state'] == 'scheduled' and value['due_at'] > current(env)['due_at']
               for value in env.alarms.instances.values())
    env.alarms.close()


@pytest.mark.parametrize('completed', [False, True])
def test_restore_daily_never_skips_a_future_sleep_alarm(tmp_path, completed):
    # A weekday followed by another weekday makes the next occurrence <24h away.
    env = setup(tmp_path, at='2026-09-21T08:28:50+08:00')
    if completed:
        drive(env, 78)
        drive(env, 6, location='outside')
        assert any(value['state'] == 'completed' and value['reason'] == 'bed_empty_with_person_outside'
                   for value in env.alarms.instances.values())
    with env.alarms.conversation('before', 'owner'):
        next_alarm = env.alarms.plan()['next']
    with env.alarms.conversation('awake', 'owner', user_text='我起床了'):
        result = env.alarms.control_text('我起床了')
        after = env.alarms.plan()['next']
    assert after['id'] == next_alarm['id']
    assert env.store.get(next_alarm['id'])['state'] == 'scheduled'
    assert result['result']['reason'] == 'already_daily'
    env.alarms.close()


@pytest.mark.parametrize('text', ['不要恢复日常', '如果我说我起床了会怎样', '我起床了，但不要恢复日常'])
def test_restore_daily_tool_requires_affirmative_request(tmp_path, text):
    from spica.plugins.registry import CapabilityRegistry
    from spica.home.tools import register_home_tools
    env = setup(tmp_path)
    drive(env, 70, finish=False)
    registry = CapabilityRegistry()
    register_home_tools(registry, env.alarms)
    with env.alarms.conversation('not-authorized', 'owner', user_text=text):
        with pytest.raises(PermissionError):
            registry.tool_handler('restore_home_daily')()
    assert current(env)['state'] != 'cancelled'
    env.alarms.close()


def test_early_restore_only_skips_the_live_bedtime_binding(tmp_path):
    from spica.home.bedtime import HomeBedtime
    from test_home_bedtime import power, settle
    env = setup(tmp_path, at='2026-09-21T05:00:00+08:00')
    adapter, _, _ = power(tmp_path)
    env.alarms.bedtime = HomeBedtime(env.config, env.store, adapter,
        clock=lambda: env.clock.mono, wall_clock=lambda: env.clock.at)
    with env.alarms.conversation('sleep', 'owner'):
        intent = env.alarms.prepare_bedtime(reply_required=False, stay_awake=True)
    target = env.store.get(intent['instance_id'])
    assert target['state'] == 'scheduled'
    with env.alarms.conversation('awake', 'owner', user_text='Spica，我已经起床了，请恢复日常吧。'):
        env.alarms.restore_daily_from_text()
    settle(env.alarms.bedtime, {'cancelled'})
    assert env.store.get(target['id'])['state'] == 'cancelled'
    with env.alarms.conversation('repeat-awake', 'owner', user_text='我起床了'):
        before = env.alarms.plan()['next']
        env.alarms.control_text('我起床了')
        assert env.alarms.plan()['next']['id'] == before['id']
    env.alarms.close()


@pytest.mark.parametrize('previous_seconds', [0, 3, 8])
def test_alarm_light_total_budget_includes_command_lock_wait(tmp_path, previous_seconds):
    import threading
    from spica.home.runtime import HomeRuntime
    env = setup(tmp_path)
    env.clock.advance(70)
    value = current(env)
    runtime = object.__new__(HomeRuntime)
    runtime._command_lock, runtime._stop = threading.RLock(), threading.Event()
    runtime._resources_closed, runtime.scenes = False, None
    runtime.clock = lambda: env.clock.mono
    entered, presses = threading.Event(), []
    def press(claim, *, deadline=None):
        for _ in range(7):
            if claim() is False:
                return 'press_aborted'
            env.clock.advance(1)
        if claim() is False:
            return 'press_aborted'
        presses.append(env.clock.at)
        return 'requested'
    def prepare(claim, **kwargs):
        entered.set()
        return runtime.prepare_alarm_light(claim, **kwargs)
    runtime.mqtt = SimpleNamespace(press_light=press)
    env.alarms.prepare_light = prepare
    try:
        with runtime._command_lock:
            worker = threading.Thread(target=env.alarms.prepare_environment, daemon=True)
            worker.start()
            assert entered.wait(1)
            env.clock.advance(previous_seconds)
        worker.join(1)
        assert not worker.is_alive()
        assert env.clock.at-value['due_at'] <= 8
        assert len(presses) == (1 if previous_seconds == 0 else 0)
        env.clock.advance(max(0, value['due_at']+8-env.clock.at))
        for _ in range(2):  # Consume the asynchronously prepared output on the next tick.
            env.alarms.step(room('unknown', at=env.clock.at), env.clock.mono, 1, environment_ready=False)
        assert value['state'] == 'calling' and len(env.speech.calls) == 1
    finally:
        env.alarms.close()


@pytest.mark.parametrize('wall_jump', [60, -3600])
def test_started_wake_keeps_cadence_camera_and_exit_evidence_across_clock_step(tmp_path, wall_jump):
    env = setup(tmp_path)
    try:
        drive(env, 80)
        assert current(env)['first_sound_at'] is not None
        previous = len(env.speech.calls)
        env.clock.at += wall_jump
        drive(env, 9)
        assert current(env)['state'] == 'calling'
        assert len(env.speech.calls) > previous
        assert env.alarms.camera_required()
        drive(env, 6, location='outside')
        assert current(env)['reason'] == 'bed_empty_with_person_outside'
    finally:
        env.alarms.close()


@pytest.mark.parametrize('wall_jump', [60, -3600])
def test_reply_completion_queued_before_clock_step_preserves_response_window(tmp_path, wall_jump):
    env = setup(tmp_path)
    try:
        drive(env, 80)
        with env.alarms.conversation('clock-step-reply', 'owner', embodied=True):
            drive(env, 20)
        count = len(env.speech.calls)
        env.alarms.observe_reply(SimpleNamespace(kind='desktop_presentation_terminal', request_id='clock-step-reply'))
        env.clock.at += wall_jump
        drive(env, 7.5)
        assert current(env)['state'] == 'calling' and len(env.speech.calls) == count
        drive(env, 1)
        assert len(env.speech.calls) == count+1
    finally:
        env.alarms.close()


def test_role_change_during_later_playback_keeps_alarm_and_schedules_new_response_gap(tmp_path):
    env = setup(tmp_path)
    try:
        drive(env, 80)
        first_sound = current(env)['first_sound_at']
        count = len(env.speech.calls)
        for _ in range(20):
            drive(env, .5, finish=False)
            if len(env.speech.calls) > count:
                break
        assert len(env.speech.calls) == count+1
        request, handle = env.speech.calls[-1]
        handle.audio_started.set_result(env.clock.mono)
        drive(env, 16, finish=False)
        env.role[0] = 'sana'
        drive(env, 1, finish=False)
        assert current(env)['state'] == 'calling' and not request.is_current()
        assert current(env)['first_sound_at'] == first_sound
        assert len(env.speech.calls) == count+1
        drive(env, 8, finish=False)
        assert len(env.speech.calls) == count+2 and env.speech.calls[-1][0].is_current()
    finally:
        env.alarms.close()
