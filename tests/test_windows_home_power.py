"""Portable state-machine tests; physical S3 is an explicit hardware check."""
from types import SimpleNamespace
import threading
from unittest.mock import Mock

import pytest

from spica.adapters.windows_home_power import WindowsHomePower
from spica.adapters.windows_home_display import WindowsHomeDisplay, WindowsMonitor
from spica.config.home import HomeConfig, HomeLightsConfig


@pytest.fixture
def power():
    stamp = dict(boot_id='windows:boot', suspend_count=12)
    state = SimpleNamespace(boot_id='windows:boot', resume_stamp=lambda: dict(stamp),
        resume_ready=lambda: True, can_suspend=lambda: True, wake_enabled=lambda: True,
        shutting_down=lambda: False, suspend=Mock(), close=Mock())
    current = [0]
    task = SimpleNamespace(name='owned-task', read=lambda: current[0])
    task.create = Mock(side_effect=lambda epoch: current.__setitem__(0, epoch))
    def cancel(epoch):
        if current[0] not in (0, epoch):
            raise RuntimeError('changed task')
        current[0] = 0
    task.cancel = Mock(side_effect=cancel)
    return SimpleNamespace(adapter=WindowsHomePower(state=state, task=task, wall_clock=lambda: 1000),
                           state=state, task=task, stamp=stamp, current=current, stop=threading.Event())


def test_verified_task_is_retained_until_real_resume(power):
    receipt = power.adapter.prepare_suspend(2000, power.stop)
    assert receipt['status'] == 'verified' and receipt['rtc_owned']
    result = power.adapter.suspend(receipt, power.stop, validate=lambda: None, margin_seconds=15)
    assert result['status'] == 'suspend_requested'
    power.state.suspend.assert_called_once()
    assert not power.adapter.resumed_since(result)
    assert power.adapter.cancel(result)['status'] == 'unknown'
    assert power.current == [2000]
    power.stamp['suspend_count'] += 1
    assert power.adapter.cancel(result)['status'] == 'cancelled'
    assert power.current == [0]


@pytest.mark.parametrize('change', ['task', 'policy', 'alarm', 'cancelled', 's3'])
def test_changed_preconditions_never_submit_suspend(power, change):
    receipt = power.adapter.prepare_suspend(2000, power.stop)
    validate = Mock()
    if change == 'task': power.current[0] = 2500
    if change == 'policy': power.state.wake_enabled = lambda: False
    if change == 's3': power.state.can_suspend = lambda: False
    if change == 'cancelled': power.stop.set()
    if change == 'alarm': validate.side_effect = RuntimeError('alarm changed')
    result = power.adapter.suspend(receipt, power.stop, validate=validate, margin_seconds=15)
    assert result['status'] == 'failed' and not result['suspend_attempted']
    power.state.suspend.assert_not_called()


def test_registration_lost_reply_retains_receipt_for_exact_cleanup(power):
    def lost_reply(epoch):
        power.current[0] = epoch
        raise OSError('lost registration reply')
    power.task.create.side_effect = lost_reply
    recorded = []
    receipt = power.adapter.prepare_suspend(2000, power.stop, record=lambda value: recorded.append(dict(value)))
    assert receipt['status'] == 'unknown' and recorded[0]['rtc_attempted']
    assert power.adapter.cancel(receipt)['status'] == 'cancelled'
    assert power.current == [0]


def test_foreign_and_changed_reservations_cannot_be_cancelled(power):
    foreign = dict(rtc_owned=True, rtc_epoch=2000)
    assert power.adapter.cancel(foreign, after_boot=True)['status'] == 'unknown'
    power.task.cancel.assert_not_called()
    receipt = power.adapter.prepare_suspend(2000, power.stop)
    power.current[0] = 2500
    assert power.adapter.cancel(receipt)['status'] == 'unknown'
    assert power.current == [2500]


def test_existing_task_is_never_adopted_even_at_same_time(power):
    power.current[0] = 2000
    receipt = power.adapter.arm_wake(2000, power.stop)
    assert receipt['status'] == 'failed' and not receipt['rtc_attempted']
    assert power.adapter.cancel(receipt)['status'] == 'cancelled'
    power.task.create.assert_not_called()
    power.task.cancel.assert_not_called()


def test_insufficient_margin_stays_awake_and_cleans_own_task(power):
    receipt = power.adapter.prepare_suspend(1010, power.stop)
    result = power.adapter.suspend(receipt, power.stop, validate=lambda: None, margin_seconds=15)
    assert result['status'] == 'stay_awake'
    power.state.suspend.assert_not_called()
    assert power.adapter.cancel(result)['status'] == 'cancelled'


def test_no_alarm_and_close_do_not_delete_tasks(power):
    receipt = power.adapter.prepare_suspend(None, power.stop)
    assert receipt['status'] == 'verified' and not receipt['rtc_attempted']
    power.adapter.close()
    power.task.cancel.assert_not_called()
    power.state.close.assert_called_once()


@pytest.mark.parametrize('monitors', [[], [WindowsMonitor('tv', 'FFA SMART TV')],
    [WindowsMonitor('a', '27M1 Max'), WindowsMonitor('b', '27M1 Max')]])
def test_other_displays_are_never_powered(monitors):
    request = Mock()
    display = WindowsHomeDisplay(HomeConfig(), enumerate_monitors=lambda: monitors, request=request)
    assert display.wake().status == display.blank().status == 'unavailable'
    request.assert_not_called()


def test_display_rechecks_identity_for_each_operation():
    monitors = [WindowsMonitor('bound', '27M1 Max')]
    request = Mock()
    display = WindowsHomeDisplay(HomeConfig(windows_monitor_id='bound', monitor_name='27M1 Max'),
        enumerate_monitors=lambda: monitors, request=request)
    assert display.wake().status == 'wake_requested'
    monitors[0] = WindowsMonitor('replaced', '27M1 Max')
    assert display.blank().status == 'unavailable'
    request.assert_called_once_with(True)


def test_windows_never_uses_linux_led_transports(monkeypatch):
    from spica.adapters.home_night_lights import HomeNightLights
    from spica.adapters.windows_home_lights import WindowsHomeLights
    native = Mock(spec=WindowsHomeLights)
    for method in ('respeaker_set', 'gpu_set', 'validate_profile'):
        getattr(native, method).side_effect = OSError('native RGB unavailable')
    monkeypatch.setattr('spica.adapters.windows_home_lights.WindowsHomeLights', lambda *_: native)
    run, usb, gpu = Mock(), Mock(), Mock()
    lights = HomeNightLights(HomeConfig(lights=HomeLightsConfig(bedtime_respeaker_leds_off=True,
        bedtime_case_leds_off=True, bedtime_gpu_leds_off=True)), effective_platform='windows',
        run=run, case_sync_off=usb, gpu_off=gpu)
    assert all(result['status'] == 'unconfirmed' for result in lights.off().values())
    run.assert_not_called()
    usb.assert_not_called()
    gpu.assert_not_called()
    native.respeaker_set.assert_called_once_with(False, None)
    native.gpu_set.assert_called_once_with(False, None)


@pytest.mark.parametrize('event_first', [False, True])
def test_resume_notification_and_event_log_can_arrive_in_either_order(event_first):
    from spica.adapters.windows_power_state import WindowsPowerState
    state = WindowsPowerState.__new__(WindowsPowerState)
    state.boot_id = 'windows:boot'
    state._lock = threading.Lock()
    state._record = state._resume_baseline = 100
    state._checked = 0.
    state._sleeping = state._pending = state._resume_notified = False
    record = [100]
    state._read_resume_record = lambda: record[0]
    for event_id in (110, 120):
        state._on_power_notification(4)
        with pytest.raises(RuntimeError, match='pending'):
            state.resume_stamp()
        if event_first:
            record[0] = event_id
            state._checked = 0.
            with pytest.raises(RuntimeError, match='pending'):
                state.resume_stamp()  # still sleeping until native callback
            state._on_power_notification(18)
        else:
            state._on_power_notification(18)
            with pytest.raises(RuntimeError, match='pending'):
                state.resume_stamp()  # old event cannot establish recovery
            record[0] = event_id
            state._checked = 0.
        assert state.resume_stamp()['suspend_count'] == event_id
        state._on_power_notification(7)  # duplicate user-resume notification
        assert state.resume_stamp()['suspend_count'] == event_id


def test_uncached_previous_resume_cannot_prove_recovery_from_the_next_sleep():
    from spica.adapters.windows_power_state import WindowsPowerState
    state = WindowsPowerState.__new__(WindowsPowerState)
    state.boot_id = 'windows:boot'
    state._lock = threading.Lock()
    state._record = state._resume_baseline = 100
    state._checked = 0.
    state._sleeping = state._pending = False
    state._resume_notified = True
    record = [110]  # Previous resume is already in the log, but not the cache.
    state._read_resume_record = lambda: record[0]

    state._on_power_notification(4)
    state._on_power_notification(18)
    with pytest.raises(RuntimeError, match='pending'):
        state.resume_stamp()
    state._on_power_notification(7)
    with pytest.raises(RuntimeError, match='pending'):
        state.resume_stamp()

    record[0] = 120
    state._checked = 0.
    assert state.resume_stamp()['suspend_count'] == 120


@pytest.mark.parametrize('failure', ['read_error', 'history_reset'])
def test_failed_suspend_boundary_sample_keeps_recovery_unknown(failure):
    from spica.adapters.windows_power_state import WindowsPowerState
    state = WindowsPowerState.__new__(WindowsPowerState)
    state.boot_id = 'windows:boot'
    state._lock = threading.Lock()
    state._record = state._resume_baseline = 100
    state._checked = 0.
    state._sleeping = state._pending = state._resume_notified = False
    state._read_resume_record = (Mock(side_effect=OSError('event log unavailable'))
                                 if failure == 'read_error' else lambda: 90)

    state._on_power_notification(4)  # Native callbacks must not raise.
    state._read_resume_record = lambda: 120
    state._on_power_notification(18)
    with pytest.raises(RuntimeError, match='pending'):
        state.resume_stamp()
    state._on_power_notification(7)
    with pytest.raises(RuntimeError, match='pending'):
        state.resume_stamp()
