"""RTC/suspend and legacy shutdown-cleanup contracts use temporary files and a controlled logind adapter."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from spica.adapters.home_power import HomePower
from spica.config.home import HomeConfig, HomeWakeConfig
from spica.home.alarm_store import AlarmStore
from spica.home.bedtime import HomeBedtime
from spica.core.events import DesktopAudioPlaybackEvent, DesktopPresentationTerminalEvent, DesktopTurnLifecycleReleasedEvent


class Logind:
    def __init__(self):
        self.scheduled = ['', 0]
        self.preparing = False
        self.preparing_sleep = False
        self.calls = []
        self.failure = None
    def __call__(self, args, **kwargs):
        assert kwargs['timeout'] == 2 and '--allow-interactive-authorization=no' in args
        member = args[7]
        self.calls.append(member)
        if self.failure == member:
            raise RuntimeError('synthetic logind failure')
        if member in {'CanPowerOff', 'CanSuspend'}:
            data = ['yes']
        elif member == 'Suspend':
            data = None
        elif member == 'ScheduledShutdown':
            data = self.scheduled
        elif member == 'PreparingForShutdown':
            data = self.preparing
        elif member == 'PreparingForSleep':
            data = self.preparing_sleep
        elif member == 'ScheduleShutdown':
            self.scheduled = [args[9], int(args[10])]
            data = None
        elif member == 'CancelScheduledShutdown':
            self.scheduled = ['', 0]
            data = [True]
        else:
            raise AssertionError(member)
        return SimpleNamespace(returncode=0, stdout=json.dumps(dict(data=data)))


def power(tmp_path):
    rtc = tmp_path/'wakealarm'
    rtc.write_text('0')
    logind = Logind()
    mem_sleep, suspend_count = tmp_path/'mem_sleep', tmp_path/'suspend_count'
    mem_sleep.write_text('s2idle [deep]')
    suspend_count.write_text('0')
    result = HomePower(rtc=rtc, run=logind, wall_clock=lambda: 1000, mem_sleep=mem_sleep, suspend_count=suspend_count)
    result.boot_id = lambda: 'boot-one'
    return result, logind, rtc


def settle(bedtime, states):
    deadline = time.monotonic()+2
    while bedtime.intent['state'] not in states:
        bedtime.step()
        assert time.monotonic() < deadline, bedtime.intent
        time.sleep(.005)
    return bedtime.intent


def test_home_close_retries_power_cleanup_after_a_temporary_database_lock(tmp_path):
    from test_home_alarm_management import home
    from test_home import Camera, MQTTEvents, profile
    from spica.home.runtime import HomeRuntime
    env = home(tmp_path)
    env.config.wake.power_control_enabled = True
    adapter, logind, rtc = power(tmp_path)
    bedtime = env.alarms.bedtime = HomeBedtime(env.config, env.store, adapter, wall_clock=lambda: 1000)
    runtime = HomeRuntime(env.config, MQTTEvents(), Camera(), None, profile(), alarms=env.alarms)
    entered, release = threading.Event(), threading.Event()
    original = env.store.save_power_receipt
    def record(identity, receipt):
        if not entered.is_set():
            entered.set()
            assert release.wait(2)
        return original(identity, receipt)
    env.store.save_power_receipt = record
    bedtime.begin(wake_at=2000, instance_id=None, request_id='sleep', require_audio=False)
    assert entered.wait(2)
    errors = []
    def close():
        try:
            runtime.close()
        except RuntimeError as exc:
            errors.append(exc)
    with env.store.db() as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        closing = threading.Thread(target=close)
        closing.start()
        try:
            assert bedtime._cancelled.wait(2)
            release.set()
            closing.join(7)
            assert not closing.is_alive() and errors
        finally:
            release.set()
            blocker.rollback()
            closing.join(2)
    bedtime.jobs.drain(2)
    assert bedtime.jobs.is_idle
    runtime.close()
    runtime.close()
    assert runtime._resources_done.is_set() and bedtime._future is None
    assert env.store.bedtime()['receipt'] == bedtime.intent['receipt']
    assert rtc.read_text() == '0' and 'Suspend' not in logind.calls


def test_power_never_overwrites_other_rtc_or_shutdown_reservations(tmp_path):
    adapter, logind, rtc = power(tmp_path)
    rtc.write_text('2000')
    assert adapter.arm_wake(3000, threading.Event())['status'] == 'failed'
    assert rtc.read_text() == '2000'
    rtc.write_text('0')
    receipt = adapter.arm_wake(3000, threading.Event())
    assert receipt['status'] == 'verified' and receipt['rtc_owned']
    receipt = dict(receipt, shutdown_owned=True, shutdown_attempted=True, shutdown_usec=1060_000000)
    logind.scheduled = ['poweroff', 1060_000000]  # Historical S5 receipt, never a new request.
    logind.scheduled = ['reboot', 1090_000000]
    rtc.write_text('4000')
    assert adapter.cancel(receipt)['status'] == 'cancelled'
    assert 'CancelScheduledShutdown' not in logind.calls
    assert logind.scheduled == ['reboot', 1090_000000]
    assert rtc.read_text() == '4000'


def test_shutdown_uncertainty_preserves_wake_and_actual_shutdown_close_keeps_it(tmp_path):
    adapter, logind, rtc = power(tmp_path)
    receipt = adapter.arm_wake(3000, threading.Event())
    uncertain = dict(receipt, shutdown_attempted=True, shutdown_owned=False, shutdown_usec=1060_000000)
    assert adapter.cancel(uncertain)['status'] == 'unknown'
    assert rtc.read_text() == '3000'
    scheduled = dict(receipt, shutdown_owned=True, shutdown_attempted=True, shutdown_usec=1060_000000)
    logind.scheduled = ['poweroff', 1060_000000]
    logind.preparing = True
    assert adapter.cancel(scheduled, closing=True)['status'] == 'shutting_down'
    assert rtc.read_text() == '3000'


@pytest.mark.parametrize('daily', [True, False])
def test_rtc_failure_withdraws_bedtime_and_restores_previous_daily_policy(tmp_path, daily):
    adapter, logind, rtc = power(tmp_path)
    rtc.write_text('2000')  # Another caller owns this alarm.
    store = AlarmStore(tmp_path/'home.sqlite3')
    config = HomeConfig(daily_detection_enabled=daily, wake=HomeWakeConfig(power_control_enabled=True, suspend_margin_seconds=10))
    bedtime = HomeBedtime(config, store, adapter, wall_clock=lambda: 1000)
    bedtime.begin(wake_at=3000, instance_id=None, request_id='sleep', require_audio=True)
    assert bedtime.suppress_daily
    result = settle(bedtime, {'cancelled', 'failed'})
    assert result['previous_daily_enabled'] == daily
    assert 'RTC' in result['reason']
    assert not bedtime.suppress_daily
    assert 'ScheduleShutdown' not in logind.calls
    assert rtc.read_text() == '2000'
    bedtime.close()


def test_explicit_awake_night_waits_for_reply_and_survives_restart_without_rtc(tmp_path):
    adapter, logind, rtc = power(tmp_path)
    rtc.write_text('9000')  # Another application's reservation must stay untouched.
    store = AlarmStore(tmp_path/'home.sqlite3')
    now, lights, displays = [1000.], [], []
    config = HomeConfig(wake=HomeWakeConfig(power_control_enabled=False))
    bedtime = HomeBedtime(config, store, adapter, wall_clock=lambda: now[0],
        light_off=lambda: lights.append('off') or {'press_status': 'requested'},
        indicators_off=lambda cancelled: {'case': {'status': 'unconfirmed'}},
        display_off=lambda: displays.append('off') or {'status': 'blank_requested'})
    bedtime.begin(wake_at=3000, instance_id=None, request_id='awake-night',
                  require_audio=False, stay_awake=True)
    bedtime.step()
    assert bedtime.intent['state'] == 'waiting_reply' and bedtime.suppress_daily
    assert displays == []
    bedtime.observe_text_delivery('awake-night', True)
    settle(bedtime, {'night'})
    assert bedtime.intent['state'] == 'night' and bedtime.intent['power_mode'] == 'awake'
    assert not bedtime.suppress_alarm and lights == ['off']
    assert bedtime.intent['indicator_lights']['case']['status'] == 'unconfirmed'
    assert bedtime.intent['display_off'] == {'status': 'blank_requested'}
    assert displays == ['off']
    bedtime.step()
    assert displays == ['off']
    bedtime.close()
    resumed = HomeBedtime(config, store, adapter, wall_clock=lambda: now[0])
    assert resumed.suppress_daily and resumed.intent['state'] == 'night'
    now[0] = 3000
    resumed.step()
    assert resumed.intent['state'] == 'morning_preparation'
    resumed.close()
    assert rtc.read_text() == '9000' and logind.calls == []


@pytest.mark.parametrize('end_state', ['resumed', 'cancelled', 'failed'])
def test_daytime_restores_only_bedtime_leds_once_and_early_preparation_stays_dark(tmp_path, end_state):
    adapter, _, _ = power(tmp_path)
    store = AlarmStore(tmp_path/'home.sqlite3')
    actions = []
    bedtime = HomeBedtime(HomeConfig(), store, adapter, wall_clock=lambda: 1000,
        indicators_off=lambda cancelled: actions.append('off') or {'case': {'status': 'requested'}},
        indicators_on=lambda cancelled: actions.append('on') or {'case': {'status': 'requested'}})
    bedtime.begin(wake_at=3000, instance_id=None, request_id='night', require_audio=False,
                  reply_required=False, stay_awake=True)
    settle(bedtime, {'night'})
    assert bedtime.intent['state'] == 'night' and actions == ['off']
    bedtime.intent['state'] = 'morning_preparation'
    bedtime.step()
    assert actions == ['off']  # Pre-alarm model warmup does not light up the room via RGB.
    bedtime.intent['state'] = end_state
    bedtime.step()
    bedtime.jobs.drain(1)
    bedtime.step()
    bedtime.step()
    assert actions == ['off', 'on']
    assert store.bedtime()['indicator_restore'] == {
        'status': 'finished', 'devices': {'case': {'status': 'requested'}}}
    bedtime.close()


def test_closing_cancels_and_drains_an_inflight_led_restore(tmp_path):
    adapter, _, _ = power(tmp_path)
    entered = threading.Event()
    def restore(cancelled):
        entered.set()
        assert cancelled.wait(2)
        return {'case': {'status': 'cancelled'}}
    bedtime = HomeBedtime(HomeConfig(), AlarmStore(tmp_path/'home.sqlite3'), adapter,
        wall_clock=lambda: 1000, indicators_off=lambda cancelled: {'case': {'status': 'requested'}},
        indicators_on=restore)
    bedtime.begin(wake_at=3000, instance_id=None, request_id='failed', require_audio=False)
    assert bedtime.intent['state'] == 'failed'  # Power control disabled: restore the daytime state.
    bedtime.jobs.drain(1)
    bedtime.step()
    assert entered.wait(1)
    bedtime.close()
    assert bedtime.jobs.is_idle
    assert bedtime.intent['indicator_restore']['devices']['case']['status'] == 'cancelled'


@pytest.mark.parametrize('release_first', [False, True])
def test_bedtime_waits_for_real_audio_and_turn_release_and_cancel_is_durable(tmp_path, release_first):
    adapter, logind, rtc = power(tmp_path)
    store = AlarmStore(tmp_path/'home.sqlite3')
    config = HomeConfig(wake=HomeWakeConfig(power_control_enabled=True, suspend_margin_seconds=10))
    bedtime = HomeBedtime(config, store, adapter, wall_clock=lambda: 1000)
    bedtime.begin(wake_at=3000, instance_id=None, request_id='sleep', require_audio=True)
    settle(bedtime, {'waiting_reply'})
    bedtime.observe_reply(DesktopAudioPlaybackEvent('sleep', 'turn', 'desktop', 0, 'started', 5))
    bedtime.observe_reply(DesktopAudioPlaybackEvent('sleep', 'turn', 'desktop', 0, 'completed', 6))
    terminal = DesktopPresentationTerminalEvent('sleep', 'turn', 'desktop', 'completed')
    released = DesktopTurnLifecycleReleasedEvent('sleep', 'turn', 'desktop')
    bedtime.observe_reply(released if release_first else terminal)
    bedtime.step()
    assert 'ScheduleShutdown' not in logind.calls
    bedtime.observe_reply(terminal if release_first else released)
    settle(bedtime, {'suspend_requested'})
    assert logind.calls.count('Suspend') == 1 and not bedtime.intent.get('resume_observed')
    adapter.suspend_count.write_text('1')
    bedtime.cancel()
    settle(bedtime, {'cancelled'})
    assert logind.scheduled == ['', 0] and rtc.read_text() == '0'
    assert store.bedtime()['state'] == 'cancelled'
    assert not bedtime.suppress_daily
    bedtime.close()


def test_cancel_during_rtc_call_prevents_any_late_shutdown(tmp_path):
    adapter, logind, rtc = power(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = adapter.arm_wake
    def arm(epoch, cancelled, **kwargs):
        result = original(epoch, cancelled, **kwargs)
        entered.set()
        assert release.wait(2)
        return result
    adapter.arm_wake = arm
    bedtime = HomeBedtime(HomeConfig(wake=HomeWakeConfig(power_control_enabled=True, suspend_margin_seconds=10)),
        AlarmStore(tmp_path/'home.sqlite3'), adapter, wall_clock=lambda: 1000)
    bedtime.begin(wake_at=3000, instance_id=None, request_id='sleep', require_audio=False, reply_required=False)
    assert entered.wait(1)
    bedtime.cancel()
    release.set()
    settle(bedtime, {'cancelled'})
    assert 'ScheduleShutdown' not in logind.calls
    assert rtc.read_text() == '0'
    bedtime.close()


@pytest.mark.parametrize('close_first', [False, True])
def test_suspend_receipt_survives_before_next_tick_and_close_preserves_pending_wake(tmp_path, close_first):
    adapter, logind, rtc = power(tmp_path)
    store = AlarmStore(tmp_path/'home.sqlite3')
    config = HomeConfig(wake=HomeWakeConfig(power_control_enabled=True, suspend_margin_seconds=10))
    bedtime = HomeBedtime(config, store, adapter, wall_clock=lambda: 1000)
    bedtime.begin(wake_at=3000, instance_id=None, request_id='sleep', require_audio=False, reply_required=False)
    bedtime._future.result(timeout=2)
    bedtime.step()
    bedtime._future.result(timeout=2)
    assert bedtime.intent['state'] == 'suspending'
    assert logind.calls.count('Suspend') == 1
    if close_first:
        bedtime.close()
        assert rtc.read_text() == '3000'  # Accepted suspend is not cancellable.
    adapter.suspend_count.write_text('1')
    recovered = HomeBedtime(config, store, adapter, wall_clock=lambda: 1000)
    settle(recovered, {'night'})
    assert recovered.intent['resume_observed']
    assert logind.scheduled == ['', 0] and rtc.read_text() == '0'
    recovered.close()
    bedtime.close()


def test_h2_storage_failure_releases_daily_suppression_and_cleans_power(tmp_path, monkeypatch):
    from test_home_alarms import setup
    from test_home import Camera, MQTTEvents, profile
    from spica.home.runtime import HomeRuntime
    from spica.home.models import SensorObservation
    env = setup(tmp_path)
    adapter, logind, rtc = power(tmp_path)
    config = env.config.model_copy(update={'wake': env.config.wake.model_copy(update={'power_control_enabled': True})})
    bedtime = env.alarms.bedtime = HomeBedtime(config, env.store, adapter, wall_clock=lambda: 1000)
    bedtime.begin(wake_at=3000, instance_id=None, request_id='sleep', require_audio=True)
    settle(bedtime, {'waiting_reply'})
    camera, mqtt = Camera(), MQTTEvents()
    runtime = HomeRuntime(config, mqtt, camera, None, profile(), alarms=env.alarms, clock=lambda: env.clock.mono)
    mqtt.events = [('presence', SensorObservation('room', True, env.clock.at, env.clock.mono, env.clock.mono+300))]
    def fail_refresh(now):
        raise RuntimeError('synthetic store failure')
    monkeypatch.setattr(env.store, 'materialize', fail_refresh)
    env.clock.advance(30)
    runtime.step()
    runtime.step()
    assert env.alarms.error
    assert not bedtime.suppress_daily
    assert camera.reasons == {'occupancy'}
    settle(bedtime, {'cancelled'})
    assert rtc.read_text() == '0' and 'ScheduleShutdown' not in logind.calls
    runtime.close()


def test_system_boot_prepares_camera_at_alarm_time_without_daily_wake(tmp_path):
    from test_home_alarms import setup, current
    from test_home import Camera, MQTTEvents, profile
    from spica.home.runtime import HomeRuntime
    from spica.home.models import SensorObservation
    env = setup(tmp_path)
    value = current(env)
    env.clock.advance(value['wake_at']-env.clock.at)
    adapter, logind, rtc = power(tmp_path)
    receipt = adapter.arm_wake(value['wake_at'], threading.Event())
    env.store.save_bedtime(dict(id='intent', request_id='sleep', boot_id='previous-boot',
        state='shutdown_scheduled', instance_id=value['id'], receipt=receipt,
        require_audio=True, deadline=env.clock.at-100, shutdown_at=env.clock.at-200,
        previous_daily_enabled=True))
    bedtime = env.alarms.bedtime = HomeBedtime(env.config, env.store, adapter, wall_clock=lambda: env.clock.at)
    assert bedtime.suppress_daily
    settle(bedtime, {'morning_preparation'})
    camera, mqtt = Camera(), MQTTEvents()
    runtime = HomeRuntime(env.config, mqtt, camera, None, profile(), alarms=env.alarms, clock=lambda: env.clock.mono)
    mqtt.events = [('presence', SensorObservation('room', True, env.clock.at, env.clock.mono, env.clock.mono+700))]
    runtime.step()
    assert camera.reasons == set()
    env.clock.advance(240)
    runtime.step()
    assert camera.reasons == set()
    env.clock.advance(60)
    runtime.step()
    assert camera.reasons == {'wake_alarm'}
    assert runtime.snapshot()['wake_attempts'] == 0
    with env.alarms.conversation('cancel-morning', 'owner'):
        env.alarms.control(action='cancel', instance_id=value['id'])
    runtime.step()
    assert camera.reasons == {'occupancy'}
    runtime.close()


def test_power_write_intents_are_durable_before_system_acceptance(tmp_path):
    adapter, logind, rtc = power(tmp_path)
    store = AlarmStore(tmp_path/'home.sqlite3')
    store.save_bedtime(dict(id='intent', state='arming', receipt=None))
    def record(receipt):
        store.save_power_receipt('intent', receipt)
    receipt = adapter.prepare_suspend(3000, threading.Event(), record=record)
    assert store.bedtime()['receipt']['rtc_attempted']
    assert not store.bedtime()['receipt']['rtc_owned']
    def crash_after_acceptance(args, **kwargs):
        result = logind(args, **kwargs)
        if args[7] == 'Suspend':
            saved = store.bedtime()['receipt']
            assert saved['suspend_attempted'] and saved['resume_stamp']['suspend_count'] == 0
            raise SystemExit('synthetic process death before the receipt returns')
        return result
    adapter.run = crash_after_acceptance
    with pytest.raises(SystemExit):
        adapter.suspend(receipt, threading.Event(), validate=lambda:None, margin_seconds=10, record=record)
    adapter.run = logind
    assert adapter.cancel(store.bedtime()['receipt'])['status'] == 'unknown'
    assert rtc.read_text() == '3000'
    adapter.suspend_count.write_text('1')
    assert adapter.cancel(store.bedtime()['receipt'])['status'] == 'cancelled'
    assert logind.scheduled == ['', 0] and rtc.read_text() == '0'


@pytest.mark.parametrize('boot_id,state', [('boot-one', 'cancelled'), ('boot-two', 'resumed')])
def test_recovery_distinguishes_core_restart_from_system_boot(tmp_path, boot_id, state):
    adapter, logind, rtc = power(tmp_path)
    receipt = adapter.arm_wake(3000, threading.Event())
    receipt = dict(receipt, shutdown_owned=True, shutdown_attempted=True, shutdown_usec=1060_000000)
    logind.scheduled = ['poweroff', 1060_000000]
    store = AlarmStore(tmp_path/'home.sqlite3')
    store.save_bedtime(dict(id='intent', request_id='sleep', boot_id='boot-one', state='shutdown_scheduled',
        receipt=receipt, require_audio=True, deadline=1100, shutdown_at=1060, previous_daily_enabled=False))
    adapter.boot_id = lambda: boot_id
    bedtime = HomeBedtime(HomeConfig(), store, adapter, wall_clock=lambda: 1000)
    result = settle(bedtime, {state})
    assert result['system_boot_observed'] == (boot_id == 'boot-two')
    assert not bedtime.suppress_daily
    assert rtc.read_text() == '0'
    assert logind.scheduled == ['', 0]
    bedtime.close()


def test_final_suspend_recheck_stays_awake_if_reply_used_up_the_margin(tmp_path):
    adapter, logind, rtc = power(tmp_path)
    now = [1000.]
    adapter.clock = lambda: now[0]
    config = HomeConfig(wake=HomeWakeConfig(power_control_enabled=True, suspend_margin_seconds=10))
    bedtime = HomeBedtime(config, AlarmStore(tmp_path/'home.sqlite3'), adapter, wall_clock=lambda: now[0])
    bedtime.begin(wake_at=1020, instance_id=None, request_id='sleep', require_audio=False)
    settle(bedtime, {'waiting_reply'})
    now[0] = 1015
    bedtime.observe_text_delivery('sleep', True)
    result = settle(bedtime, {'night'})
    assert result['reason'] == 'wake_time_too_close' and 'Suspend' not in logind.calls
    assert rtc.read_text() == '0'
    bedtime.close()


def test_actual_same_process_resume_preserves_night_and_does_not_repeat_suspend(tmp_path):
    adapter, logind, rtc = power(tmp_path)
    store = AlarmStore(tmp_path/'home.sqlite3')
    config = HomeConfig(wake=HomeWakeConfig(power_control_enabled=True, suspend_margin_seconds=10))
    bedtime = HomeBedtime(config, store, adapter, wall_clock=lambda: 1000)
    bedtime.begin(wake_at=3000, instance_id=None, request_id='sleep', require_audio=False, reply_required=False)
    settle(bedtime, {'suspend_requested'})
    for _ in range(4):
        bedtime.step()
    assert bedtime.resume_generation == 0 and bedtime.suppress_daily
    adapter.suspend_count.write_text('1')
    settle(bedtime, {'night'})
    assert bedtime.resume_generation == 1 and rtc.read_text() == '0'
    bedtime.close()
    recovered = HomeBedtime(config, store, adapter, wall_clock=lambda: 1000)
    assert recovered.intent['state'] == 'night' and recovered.suppress_daily
    assert not recovered.suppress_alarm and logind.calls.count('Suspend') == 1
    recovered.cancel(reason='owner_restored_daily')
    settle(recovered, {'cancelled'})
    recovered.close()


def test_explicit_goodnight_after_resume_arms_again_and_waits_for_its_reply(tmp_path):
    from test_home_alarms import setup
    env = setup(tmp_path)
    env.clock.at -= 3600
    adapter, logind, rtc = power(tmp_path)
    adapter.clock = lambda: env.clock.at
    config = env.config.model_copy(update={'wake': env.config.wake.model_copy(update={
        'power_control_enabled': True, 'suspend_margin_seconds': 10})})
    env.alarms.config = config
    lights = []
    bedtime = env.alarms.bedtime = HomeBedtime(config, env.store, adapter,
        wall_clock=lambda: env.clock.at, light_off=lambda: lights.append('off'))
    try:
        with env.alarms.conversation('first-night', 'owner', want_audio=False):
            env.alarms.prepare_bedtime(reply_required=False)
        settle(bedtime, {'suspend_requested'})
        original_id = bedtime.intent['id']
        adapter.suspend_count.write_text('1')
        settle(bedtime, {'night'})
        for _ in range(3): bedtime.step()
        assert logind.calls.count('Suspend') == 1  # No automatic re-suspend loop.
        with env.alarms.conversation('first-night', 'owner', want_audio=False):
            env.alarms.prepare_bedtime(reply_required=False)
        assert bedtime.intent['id'] == original_id and len(lights) == 1
        with env.alarms.conversation('back-to-sleep', 'owner', want_audio=False):
            env.alarms.prepare_bedtime()
        assert bedtime.intent['id'] != original_id
        settle(bedtime, {'waiting_reply'})
        assert int(rtc.read_text()) == bedtime.intent['wake_at']
        assert logind.calls.count('Suspend') == 1 and len(lights) == 2
        bedtime.observe_text_delivery('back-to-sleep', True)
        settle(bedtime, {'suspend_requested'})
        assert logind.calls.count('Suspend') == 2
        adapter.suspend_count.write_text('2')
        settle(bedtime, {'night'})
    finally:
        env.alarms.close()


@pytest.mark.parametrize('text,accepted', [
    ('关灯,我准备睡觉了。', True),
    ('Speaker 关灯了,我要睡觉了,晚安。', True),
    ('Spica，晚安，关灯，我准备睡觉了', True),
    ('只关灯，不要进入晚安', False),
    ('如果我说晚安，会发生什么', False),
    ('我准备睡觉了吗？', False),
    ('你刚才说，晚安', False),
])
def test_bedtime_admission_accepts_owner_sleep_clause_after_light_request(text, accepted):
    from spica.home.alarms import HomeAlarms
    calls = []
    owner = SimpleNamespace(_require_origin=lambda: ('request', 'owner', text, False),
        bedtime=None, wake_active=lambda: False,
        prepare_bedtime=lambda **kw: calls.append(kw) or {'state': 'arming'})
    if accepted:
        assert HomeAlarms.prepare_bedtime_from_text(owner)['state'] == 'arming'
        assert len(calls) == 1
    else:
        with pytest.raises(PermissionError):
            HomeAlarms.prepare_bedtime_from_text(owner)
        assert not calls


def test_bedtime_no_near_alarm_asks_once_then_explicit_no_wake_can_suspend(tmp_path):
    from test_home_alarms import setup
    env = setup(tmp_path)
    env.clock.advance(3600)  # Friday's alarm passed; Monday must not be selected.
    adapter, logind, rtc = power(tmp_path)
    lights = []
    config = env.config.model_copy(update={'wake': env.config.wake.model_copy(update={'power_control_enabled': True})})
    bedtime = env.alarms.bedtime = HomeBedtime(config, env.store, adapter,
        wall_clock=lambda: env.clock.at, light_off=lambda: lights.append('off') or {'press_status': 'requested'})
    with env.alarms.conversation('sleep', 'owner'):
        result = env.alarms.prepare_bedtime()
    assert result['ask_wake_time'] and bedtime.intent['state'] == 'awaiting_alarm'
    assert env.alarms.uses_home_audio('sleep')
    assert lights == ['off'] and not logind.calls and rtc.read_text() == '0'
    with env.alarms.conversation('again', 'owner'):
        assert not env.alarms.prepare_bedtime()['ask_wake_time']
        assert env.alarms.uses_home_audio('again')
    assert not env.alarms.uses_home_audio('sleep')
    assert bedtime.intent['request_id'] == 'again'
    assert bedtime.intent['state'] == 'awaiting_alarm'
    assert lights == ['off'] and not logind.calls and rtc.read_text() == '0'
    with env.alarms.conversation('no-alarm', 'owner'):
        env.alarms.prepare_bedtime(no_wake=True, reply_required=False)
    settle(bedtime, {'suspend_requested'})
    assert rtc.read_text() == '0' and logind.calls.count('Suspend') == 1
    adapter.suspend_count.write_text('1')
    settle(bedtime, {'night'})
    env.alarms.close()


def test_bedtime_under_five_minutes_keeps_original_alarm_and_stays_up(tmp_path):
    from test_home_alarms import setup, current
    env = setup(tmp_path)
    adapter, logind, rtc = power(tmp_path)
    env.alarms.bedtime = HomeBedtime(env.config, env.store, adapter, wall_clock=lambda: env.clock.at)
    with env.alarms.conversation('sleep', 'owner'):
        result = env.alarms.prepare_bedtime()
    assert result['state'] == 'morning_preparation'
    assert result['instance_id'] == current(env)['id']
    assert result['wake_datetime'] == '2026-09-18T08:30:00+08:00'
    assert not logind.calls and rtc.read_text() == '0'
    env.alarms.close()


def test_final_alarm_change_prevents_suspend_and_keeps_schedule(tmp_path):
    from test_home_alarms import setup, current
    env = setup(tmp_path)
    env.clock.advance(-600)
    adapter, logind, rtc = power(tmp_path)
    adapter.clock = lambda: env.clock.at
    cfg = env.config.model_copy(update={'wake': env.config.wake.model_copy(update={
        'power_control_enabled': True, 'suspend_margin_seconds': 10})})
    bedtime = env.alarms.bedtime = HomeBedtime(cfg, env.store, adapter, wall_clock=lambda: env.clock.at)
    with env.alarms.conversation('sleep', 'owner', want_audio=False):
        env.alarms.prepare_bedtime()
    settle(bedtime, {'waiting_reply'})
    value = current(env)
    value['due_at'] += 600
    env.store.save(value)
    bedtime.observe_text_delivery('sleep', True)
    settle(bedtime, {'cancelled', 'failed'})
    assert 'Suspend' not in logind.calls and rtc.read_text() == '0'
    assert env.store.get(value['id'])['state'] == 'scheduled'
    env.alarms.close()
