"""Blocking device work must remain revocable without holding the Home owner."""
from concurrent.futures import Future
import threading

import pytest

from spica.config.home import HomeConfig, HomeWakeConfig
from spica.home.alarm_store import AlarmStore
from spica.home.bedtime import HomeBedtime
from test_home_bedtime import power, settle


def run_thread(operation):
    result = Future()
    def run():
        try:
            result.set_result(operation())
        except BaseException as exc:
            result.set_exception(exc)
    worker = threading.Thread(target=run)
    worker.start()
    return worker, result


def test_alarm_begin_and_cancel_release_owner_lock_while_led_off_is_blocked(tmp_path):
    from test_home_alarm_management import home
    env = home(tmp_path)
    env.config.wake.power_control_enabled = True
    adapter, logind, _ = power(tmp_path)
    adapter.clock = lambda: env.clock.at
    entered, release, saw_cancel, restored = (threading.Event() for _ in range(4))
    actions = []
    def off(cancelled):
        actions.append('off_enter')
        entered.set()
        assert cancelled.wait(3)
        saw_cancel.set()
        assert release.wait(3)
        actions.append('off_exit')
        return {'ram': {'status': 'cancelled'}}
    def on(cancelled):
        assert release.is_set()
        actions.append('on')
        restored.set()
        return {'ram': {'status': 'requested'}}
    bedtime = env.alarms.bedtime = HomeBedtime(env.config, env.store, adapter,
        wall_clock=lambda: env.clock.at, indicators_off=off, indicators_on=on)
    def begin():
        with env.alarms.conversation('night', 'home-local-control', want_audio=False):
            return env.alarms.prepare_bedtime(no_wake=True, reply_required=False)
    def cancel():
        with env.alarms.conversation('stop', 'home-local-control', want_audio=False):
            return env.alarms.cancel_bedtime()
    beginning, result = run_thread(begin)
    cancelling = None
    try:
        assert entered.wait(1)
        assert result.result(timeout=1)['indicator_lights'] == {'status': 'pending'}
        assert not release.is_set(), 'begin must return before the hardware operation completes'
        bedtime._future.result(timeout=1)
        bedtime.step()
        assert bedtime.intent['state'] == 'waiting_reply' and 'Suspend' not in logind.calls
        cancelling, result = run_thread(cancel)
        assert result.result(timeout=1)['state'] == 'cancelling'
        assert saw_cancel.wait(1), 'cancel must acquire the alarm lock and reach the running device job'
        settle(bedtime, {'cancelled'})
        with pytest.raises(RuntimeError, match='正在处理'):
            bedtime.begin(wake_at=None, instance_id=None, request_id='new', require_audio=False)
        assert not restored.is_set() and 'Suspend' not in logind.calls
        assert bedtime.intent['indicator_lights'] == {'status': 'pending'}
        release.set()
        bedtime.jobs.drain(1)
        assert bedtime.jobs.is_idle
        # Even a completed worker must leave business state to the owner tick.
        assert env.store.bedtime()['indicator_lights'] == {'status': 'pending'}
        bedtime.step()
        assert restored.wait(1)
        bedtime.jobs.drain(1)
        bedtime.step()
        assert actions == ['off_enter', 'off_exit', 'on']
        assert bedtime.intent['indicator_lights'] == {'ram': {'status': 'cancelled'}}
        assert bedtime.intent['indicator_restore']['status'] == 'finished'
        assert 'Suspend' not in logind.calls
    finally:
        bedtime._cancelled.set()
        release.set()
        beginning.join(2)
        if cancelling is not None:
            cancelling.join(2)
        env.alarms.close()


def test_ready_reply_and_rtc_cannot_submit_suspend_before_led_off_finishes(tmp_path):
    adapter, logind, _ = power(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def off(cancelled):
        entered.set()
        assert release.wait(3)
        assert not cancelled.is_set()
        return {'ram': {'status': 'requested'}}
    bedtime = HomeBedtime(HomeConfig(wake=HomeWakeConfig(power_control_enabled=True, suspend_margin_seconds=10)),
        AlarmStore(tmp_path/'home.sqlite3'), adapter, wall_clock=lambda: 1000, indicators_off=off)
    try:
        bedtime.begin(wake_at=3000, instance_id=None, request_id='night', require_audio=False)
        assert entered.wait(1)
        bedtime._future.result(timeout=1)
        bedtime.observe_text_delivery('night', True)
        bedtime.step()
        assert bedtime.intent['state'] == 'waiting_reply' and 'Suspend' not in logind.calls
        assert bedtime._future is None and not bedtime.jobs.is_idle
        release.set()
        settle(bedtime, {'suspend_requested'})
        assert logind.calls.count('Suspend') == 1
        assert bedtime.intent['indicator_lights'] == {'ram': {'status': 'requested'}}
        adapter.suspend_count.write_text('1')
    finally:
        release.set()
        bedtime.close()


def test_retime_keeps_old_identity_until_cancelled_led_job_is_drained(tmp_path):
    adapter, _, rtc = power(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def off(cancelled):
        entered.set()
        assert release.wait(3)
        return {'ram': {'status': 'cancelled' if cancelled.is_set() else 'requested'}}
    bedtime = HomeBedtime(HomeConfig(wake=HomeWakeConfig(power_control_enabled=True, suspend_margin_seconds=10)),
        AlarmStore(tmp_path/'home.sqlite3'), adapter, wall_clock=lambda: 1000,
        indicators_off=off, resources_ready=lambda: False)
    try:
        bedtime.begin(wake_at=3000, instance_id=None, request_id='night', require_audio=False, reply_required=False)
        assert entered.wait(1)
        bedtime._future.result(timeout=1)
        bedtime.step()
        old_id, old_token = bedtime.intent['id'], bedtime._cancelled
        bedtime.reconcile_alarm({'id': 'replacement', 'wake_at': 4000}, 2)
        bedtime._future.result(timeout=1)
        bedtime.step()
        assert old_token.is_set()
        assert bedtime.intent['id'] == old_id and bedtime.intent['state'] == 'retiming'
        assert bedtime._future is None and rtc.read_text() == '0'
        bedtime.step()
        assert bedtime.intent['id'] == old_id and not release.is_set()
        release.set()
        settle(bedtime, {'waiting_reply'})
        assert bedtime.intent['id'] != old_id and not bedtime._cancelled.is_set()
        assert rtc.read_text() == '4000'
        assert bedtime.intent['indicator_lights'] == {'ram': {'status': 'cancelled'}}
    finally:
        release.set()
        bedtime.close()


@pytest.mark.parametrize('power_enabled', [False, True])
def test_close_cancels_running_off_without_starting_a_late_restore(tmp_path, power_enabled):
    adapter, logind, _ = power(tmp_path)
    entered, exited = threading.Event(), threading.Event()
    restored = []
    def off(cancelled):
        entered.set()
        assert cancelled.wait(3)
        exited.set()
        return {'ram': {'status': 'cancelled'}}
    bedtime = HomeBedtime(HomeConfig(wake=HomeWakeConfig(power_control_enabled=power_enabled)),
        AlarmStore(tmp_path/'home.sqlite3'), adapter, wall_clock=lambda: 1000,
        indicators_off=off, indicators_on=lambda token: restored.append('on'))
    bedtime.begin(wake_at=3000, instance_id=None, request_id='night', require_audio=False, reply_required=False)
    assert entered.wait(1)
    bedtime.close()
    bedtime.step()
    bedtime.close()
    assert exited.is_set() and bedtime.jobs.is_idle and not restored
    assert bedtime.intent['indicator_lights'] == {'ram': {'status': 'cancelled'}}
    assert 'Suspend' not in logind.calls


def test_os_cleanup_pending_cleans_rtc_and_blocks_effects_until_background_probe_confirms_idle(tmp_path):
    adapter, logind, rtc = power(tmp_path)
    pending = {'ram': dict(status='cleanup_pending', reason='synthetic OS task still active',
        owner=dict(installation_identity='bound-installation', instances={'off': ['bound-instance']}))}
    now, answer, probes, restores = [0.], [False], [], []
    entered, release = threading.Event(), threading.Event()
    def probe(value):
        probes.append(value)
        entered.set()
        assert release.wait(3)
        return answer[0]
    bedtime = HomeBedtime(HomeConfig(wake=HomeWakeConfig(power_control_enabled=True, suspend_margin_seconds=10)),
        AlarmStore(tmp_path/'home.sqlite3'), adapter, wall_clock=lambda: 1000, clock=lambda: now[0],
        indicators_off=lambda token: pending,
        indicators_on=lambda token: restores.append('on') or {'ram': {'status': 'requested'}},
        indicators_settled=probe)
    try:
        bedtime.begin(wake_at=3000, instance_id=None, request_id='night', require_audio=False, reply_required=False)
        bedtime.jobs.drain(1)
        settle(bedtime, {'cancelled'})
        bedtime.jobs.drain(1)
        assert rtc.read_text() == '0' and 'Suspend' not in logind.calls
        assert bedtime.store.bedtime()['indicator_cleanup_pending'] == pending
        assert bedtime._indicator_job is None and not restores
        with pytest.raises(RuntimeError, match='正在处理'):
            bedtime.begin(wake_at=None, instance_id=None, request_id='new', require_audio=False)
        for _ in range(3):
            bedtime.step()
        assert not probes
        now[0] = 5
        bedtime.step()
        assert entered.wait(1)
        bedtime.step()
        assert len(probes) == 1 and not release.is_set() and not restores
        release.set()
        bedtime.jobs.drain(1)
        bedtime.step()
        assert bedtime.intent['indicator_cleanup_pending'] == pending
        now[0] = 9.9
        bedtime.step()
        assert len(probes) == 1
        answer[0], now[0] = True, 10
        bedtime.step()
        bedtime.jobs.drain(1)
        bedtime.step()
        bedtime.jobs.drain(1)
        bedtime.step()
        assert probes == [pending, pending] and restores == ['on']
        assert 'indicator_cleanup_pending' not in bedtime.intent
        assert bedtime.intent['indicator_lights'] == pending, 'cleanup must not invent an old command success'
        assert bedtime.intent['indicator_restore']['status'] == 'finished'
        assert 'Suspend' not in logind.calls
    finally:
        answer[0] = True
        release.set()
        bedtime.close()


def test_unknown_restore_does_not_retry_new_hardware_jobs_and_survives_restart(tmp_path):
    adapter, _, _ = power(tmp_path)
    pending = {'ram': dict(status='cleanup_pending', owner={'installation_identity': 'other-installation',
        'instances': {'on': ['old-instance']}})}
    restores = []
    store = AlarmStore(tmp_path/'home.sqlite3')
    bedtime = HomeBedtime(HomeConfig(), store, adapter,
        wall_clock=lambda: 1000, indicators_off=lambda token: {'ram': {'status': 'requested'}},
        indicators_on=lambda token: restores.append('on') or pending)
    bedtime.begin(wake_at=3000, instance_id=None, request_id='night', require_audio=False)
    bedtime.jobs.drain(1)
    bedtime.step()
    bedtime.jobs.drain(1)
    bedtime.step()
    assert bedtime.intent['indicator_restore']['status'] == 'pending'
    for _ in range(3):
        bedtime.step()
    assert restores == ['on'] and store.bedtime()['indicator_cleanup_pending'] == pending
    with pytest.raises(RuntimeError, match='cleanup remains pending'):
        bedtime.close()
    # Another platform/install cannot certify the original task's lifetime.
    from spica.adapters.home_night_lights import HomeNightLights
    foreign = HomeNightLights(HomeConfig(), effective_platform='linux')
    recovered = HomeBedtime(HomeConfig(), store, adapter, wall_clock=lambda: 1000,
        indicators_settled=foreign.cleanup_settled, indicators_on=lambda token: restores.append('unexpected'))
    recovered.step()
    recovered.jobs.drain(1)
    recovered.step()
    assert recovered.intent['indicator_cleanup_pending'] == pending and restores == ['on']
    with pytest.raises(RuntimeError, match='cleanup remains pending'):
        recovered.close()
    assert recovered.jobs.is_idle
