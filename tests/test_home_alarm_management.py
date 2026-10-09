"""Alarm management through the same Home owner used by voice and desktop IPC."""
from datetime import datetime
from types import SimpleNamespace
import threading
from contextlib import contextmanager

import pytest

from spica.config.home import HomeConfig, HomeWakeConfig
from spica.home.alarm_store import AlarmStore
from spica.home.alarms import HomeAlarms
from test_home_alarms import Clock, Speech, drive, ready_output, room


def stamp(value):
    return datetime.fromisoformat(value + '+08:00').timestamp()


def home(tmp_path):
    clock = Clock()
    clock.at = stamp('2026-09-20T20:00:45')
    speech = Speech(clock)
    config = HomeConfig(wake=HomeWakeConfig(enabled=True, leave_bed_seconds=3, leave_bed_min_frames=6))
    store = AlarmStore(tmp_path / 'alarms.sqlite3')
    kwargs = dict(propose_speech=speech.submit, cancel_speech=speech.cancel,
                  prepare_output=ready_output, active_character=lambda: 'sana',
                  clock=lambda: clock.mono, wall_clock=lambda: clock.at)
    return SimpleNamespace(clock=clock, speech=speech, store=store, config=config,
                           kwargs=kwargs, alarms=HomeAlarms(config, store, **kwargs))


def change(env, request, action, **arguments):
    with env.alarms.conversation(request, 'home-local-control', want_audio=False):
        return env.alarms.manage(action=action, arguments=arguments)


def view(env):
    with env.alarms.conversation('query', 'home-local-control', want_audio=False):
        return env.alarms.plan()


def test_one_fixed_alarm_updates_in_place_and_survives_restart(tmp_path):
    env = home(tmp_path)
    saved = change(env, 'fixed-1', 'save_fixed', local_time='08:25', weekdays=[0, 1, 2, 3, 4], label='上班')
    assert saved['fixed']['next']['due_at'] == stamp('2026-09-21T08:25:00')
    identity = saved['fixed']['id']
    changed = change(env, 'fixed-2', 'save_fixed', local_time='07:30', weekdays=[0, 1, 2, 3, 4], label='上班')
    assert changed['fixed']['id'] == identity
    assert changed['next']['due_at'] == stamp('2026-09-21T07:30:00')
    env.alarms.close()
    env.alarms = HomeAlarms(env.config, env.store, **env.kwargs)
    assert view(env)['next']['due_at'] == stamp('2026-09-21T07:30:00')
    assert len(env.store.schedules()) == 1
    env.alarms.close()


def test_scene_guard_observes_a_complete_alarm_edit_without_waiting_for_its_lock(tmp_path):
    import inspect
    import sys
    from spica.home.runtime import HomeRuntime
    from test_home import Camera, MQTTEvents, profile
    env = home(tmp_path)
    env.clock.at = stamp('2026-09-21T07:30:00')
    change(env, 'initial', 'save_fixed', local_time='08:25', weekdays=[0, 1, 2, 3, 4])
    runtime = HomeRuntime(env.config, MQTTEvents(), Camera(), None, profile(), alarms=env.alarms)
    assert runtime.scene_allowed()
    entered, release = threading.Event(), threading.Event()
    answers, errors = [], []
    method = env.alarms._manage.__func__
    lines, start = inspect.getsourcelines(method)
    final_line = start + next(i for i, line in enumerate(lines) if 'self._reconcile_bedtime(reply_request=' in line)
    def trace(frame, event, arg):
        if (event == 'line' and frame.f_code is method.__code__ and not entered.is_set()
                and (frame.f_locals.get('current') == {} or frame.f_lineno == final_line)):
            entered.set()
            assert release.wait(2)
        return trace
    def edit():
        sys.settrace(trace)
        try:
            change(env, 'edit', 'save_fixed', local_time='07:31', weekdays=[0, 1, 2, 3, 4])
        except BaseException as exc:
            errors.append(exc)
        finally:
            sys.settrace(None)
    def read():
        try:
            with runtime._command_lock:
                answers.append(runtime.scene_allowed())
        except BaseException as exc:
            errors.append(exc)
    writer, reader = threading.Thread(target=edit), threading.Thread(target=read)
    writer.start()
    try:
        assert entered.wait(2)
        reader.start()
        reader.join(.5)
        assert not reader.is_alive(), 'scene guard cannot take the reversed alarm lock'
        assert not errors and answers == [True], 'read the previous complete state until the edit is published'
    finally:
        release.set()
        writer.join(2)
        if reader.ident is not None:
            reader.join(2)
    assert not errors and not writer.is_alive()
    env.clock.advance(60)
    assert not runtime.scene_allowed(), 'the newly due occurrence is published when management completes'
    runtime.close()


def test_moving_fixed_once_preserves_independent_temporary_and_following_weekday(tmp_path):
    env = home(tmp_path)
    saved = change(env, 'fixed', 'save_fixed', local_time='08:25', weekdays=[0, 1, 2, 3, 4])
    instance = saved['next']['id']
    change(env, 'move', 'move', instance_id=instance, due_at=stamp('2026-09-21T09:00:00'))
    result = change(env, 'nap', 'add_temporary', due_at=stamp('2026-09-21T08:30:00'), label='临时')
    assert result['next']['due_at'] == stamp('2026-09-21T08:30:00')
    assert result['fixed']['next']['id'] == instance
    assert result['fixed']['next']['due_at'] == stamp('2026-09-21T09:00:00')
    assert result['fixed']['local_time'] == '08:25'
    env.clock.advance(13 * 3600 + 15)
    assert view(env)['next']['due_at'] == stamp('2026-09-22T08:25:00')
    env.alarms.close()


def test_fixed_switch_and_rule_edits_preserve_date_override_but_not_independent_alarm(tmp_path):
    env = home(tmp_path)
    saved = change(env, 'fixed', 'save_fixed', local_time='08:25', weekdays=[0, 1, 2, 3, 4])
    fixed, instance = saved['fixed']['id'], saved['next']['id']
    change(env, 'move', 'move', instance_id=instance, due_at=stamp('2026-09-21T09:00:00'))
    changed = change(env, 'earlier-rule', 'save_fixed', local_time='07:30', weekdays=[0, 1, 2, 3, 4])
    assert changed['fixed']['next']['due_at'] == stamp('2026-09-21T09:00:00')
    change(env, 'temporary', 'add_temporary', due_at=stamp('2026-09-21T08:30:00'))
    disabled = change(env, 'disable', 'set_enabled', schedule_id=fixed, enabled=False)
    assert disabled['fixed']['next'] is None
    assert disabled['next']['kind'] == 'temporary'
    enabled = change(env, 'enable', 'set_enabled', schedule_id=fixed, enabled=True)
    assert enabled['fixed']['next']['due_at'] == stamp('2026-09-21T09:00:00')
    skipped = change(env, 'skip-monday', 'skip', instance_id=instance)
    assert skipped['fixed']['next']['due_at'] == stamp('2026-09-22T07:30:00')
    restored = change(env, 'restore-monday', 'restore', instance_id=instance)
    assert restored['fixed']['next']['due_at'] == stamp('2026-09-21T07:30:00')
    env.alarms.close()


def test_rearming_temporary_while_old_run_finishes_cannot_disable_tomorrow(tmp_path):
    env = home(tmp_path)
    saved = change(env, 'nap', 'add_temporary', due_at=stamp('2026-09-20T20:01:45'), label='小睡')
    old = saved['next']
    drive(env, 70)
    assert view(env)['active'][0]['id'] == old['id']
    change(env, 'disable', 'set_enabled', schedule_id=old['schedule_id'], enabled=False)
    rearmed = change(env, 'rearm', 'rearm', schedule_id=old['schedule_id'])
    assert rearmed['next']['due_at'] == stamp('2026-09-21T20:01:45')
    assert rearmed['next']['schedule_id'] != old['schedule_id']
    env.clock.advance(600)
    env.alarms.step(room(at=env.clock.at), env.clock.mono, 1)
    result = view(env)
    assert not result['active']
    assert result['next']['due_at'] == stamp('2026-09-21T20:01:45')
    assert result['temporary'][0]['enabled']
    env.alarms.close()


def test_temporary_edit_delete_and_stale_or_replayed_commands(tmp_path):
    env = home(tmp_path)
    saved = change(env, 'temporary', 'add_temporary', due_at=stamp('2026-09-21T08:30:00'), label='临时')
    identity = saved['next']['schedule_id']
    updated = change(env, 'edit', 'edit_temporary', schedule_id=identity, due_at=stamp('2026-09-21T09:30:00'), label='午睡')
    repeated = change(env, 'edit', 'edit_temporary', schedule_id=identity, due_at=stamp('2026-09-21T09:30:00'), label='午睡')
    assert repeated['revision'] == updated['revision']
    assert len(repeated['temporary']) == 1
    assert repeated['next']['due_at'] == stamp('2026-09-21T09:30:00')
    with env.alarms.conversation('stale-delete', 'home-local-control', want_audio=False):
        with pytest.raises(ValueError, match='刷新'):
            env.alarms.manage(action='delete', arguments={'schedule_id': identity}, expected_revision=saved['revision'])
    removed = change(env, 'delete', 'delete', schedule_id=identity)
    assert removed['next'] is None and not removed['temporary']
    with env.alarms.conversation('history', 'home-local-control', want_audio=False):
        assert env.alarms.query()['instances']
    env.alarms.close()


def test_voice_changes_fixed_once_but_relative_request_adds_exact_temporary(tmp_path):
    env = home(tmp_path)
    change(env, 'fixed', 'save_fixed', local_time='08:25', weekdays=[0, 1, 2, 3, 4])
    with env.alarms.conversation('voice-morning', 'desktop', user_text='明早九点叫我'):
        result = env.alarms.set_from_text(local_time='09:00', on_date='2026-09-21')
    assert result['fixed']['next']['due_at'] == stamp('2026-09-21T09:00:00')
    assert not result['temporary']
    with env.alarms.conversation('voice-nap', 'desktop', user_text='半小时后叫醒我'):
        env.clock.advance(19)
        result = env.alarms.set_from_text(after_minutes=30)
        env.clock.advance(5)
        repeated = env.alarms.set_from_text(after_minutes=30)
    assert result['next']['due_at'] == stamp('2026-09-20T20:30:45')
    assert repeated['revision'] == result['revision']
    with env.alarms.conversation('too-late', 'desktop', user_text='后天八点叫我'):
        with pytest.raises(ValueError, match='24'):
            env.alarms.set_from_text(local_time='08:00', on_date='2026-09-22')
    env.alarms.close()


def test_voice_skip_requires_full_date_target_and_restoring_past_time_never_rings(tmp_path):
    env = home(tmp_path)
    saved = change(env, 'fixed', 'save_fixed', local_time='08:25', weekdays=[0, 1, 2, 3, 4])
    identity = saved['next']['id']
    change(env, 'later', 'move', instance_id=identity, due_at=stamp('2026-09-21T09:00:00'))
    change(env, 'temporary', 'add_temporary', due_at=stamp('2026-09-21T08:30:00'))
    with env.alarms.conversation('ambiguous', 'desktop', user_text='明天不用叫我'):
        with pytest.raises(ValueError, match='多个'):
            env.alarms.adjust_from_text(action='skip', on_date='2026-09-21')
    env.clock.advance(stamp('2026-09-21T08:40:00') - env.clock.at)
    with env.alarms.conversation('restore', 'desktop', user_text='恢复今天固定闹钟的原时间'):
        result = env.alarms.adjust_from_text(action='restore', instance_id=identity)
    assert result['next']['due_at'] == stamp('2026-09-22T08:25:00')
    env.alarms.step(room(at=env.clock.at), env.clock.mono, 1)
    assert not env.speech.calls
    env.alarms.close()


def test_registered_voice_tool_cannot_modify_recurrence_and_query_returns_effective_plan(tmp_path):
    from spica.home.tools import register_home_tools
    from spica.plugins.registry import CapabilityRegistry
    env = home(tmp_path)
    registry = CapabilityRegistry()
    register_home_tools(registry, env.alarms)
    names = {value['function']['name']: value['function'] for value in registry.tool_schemas()}
    assert 'set_wake_alarm_enabled' not in names
    assert 'weekdays' not in names['set_wake_alarm']['parameters']['properties']
    with env.alarms.conversation('spoken', 'desktop', user_text='明早九点叫我'):
        created = registry.tool_handler('set_wake_alarm')(local_time='09:00', on_date='2026-09-21')
    with env.alarms.conversation('query', 'desktop'):
        result = registry.tool_handler('list_wake_alarms')()
    assert result['plan']['next']['id'] == created['next']['id']
    env.alarms.close()


def test_bedtime_spoken_time_keeps_earlier_independent_alarm_and_uses_its_wake_time(tmp_path):
    from spica.home.bedtime import HomeBedtime
    from test_home_bedtime import power
    env = home(tmp_path)
    change(env, 'fixed', 'save_fixed', local_time='08:25', weekdays=[0, 1, 2, 3, 4])
    temporary = change(env, 'independent', 'add_temporary', due_at=stamp('2026-09-21T08:30:00'))['next']
    adapter, _, _ = power(tmp_path)
    env.alarms.bedtime = HomeBedtime(env.config, env.store, adapter,
        clock=lambda: env.clock.mono, wall_clock=lambda: env.clock.at)
    with env.alarms.conversation('goodnight', 'desktop', user_text='晚安，明早九点叫我', want_audio=False):
        result = env.alarms.prepare_bedtime_from_text(wake_date='2026-09-21', wake_time='09:00')
    assert result['wake_datetime'].endswith('08:30:00+08:00')
    assert view(env)['fixed']['next']['due_at'] == stamp('2026-09-21T09:00:00')
    assert any(v['kind'] == 'temporary' and v['due_at'] == stamp('2026-09-21T08:30:00') for v in view(env)['upcoming'])
    env.alarms.close()


def test_rearm_uses_selected_date_and_label_and_completed_edit_requires_rearm(tmp_path):
    env = home(tmp_path)
    saved = change(env, 'temporary', 'add_temporary', due_at=env.clock.at+30)
    identity = saved['next']['schedule_id']
    drive(env, 40, location='desk')
    with pytest.raises(ValueError, match='重新'):
        change(env, 'invalid-edit', 'edit_temporary', schedule_id=identity, due_at=env.clock.at+600)
    result = change(env, 'rearm', 'rearm', schedule_id=identity,
                    due_at=stamp('2026-09-22T10:15:00'), label='指定日期')
    assert result['next']['due_at'] == stamp('2026-09-22T10:15:00')
    assert result['temporary'][0]['label'] == '指定日期'
    env.alarms.close()


def test_move_racing_actual_playback_preserves_run_and_rejects_override(tmp_path):
    env = home(tmp_path)
    env.clock.at = stamp('2026-09-21T08:24:55')
    saved = change(env, 'fixed', 'save_fixed', local_time='08:25', weekdays=[0, 1, 2, 3, 4])
    identity = saved['next']['id']
    drive(env, 10, finish=False)
    assert env.speech.calls
    cancel = env.alarms.cancel_speech
    def started_during_cancel(proposal):
        env.speech.calls[-1][1].audio_started.set_result(env.clock.mono)
        cancel(proposal)
    env.alarms.cancel_speech = started_during_cancel
    with pytest.raises(ValueError, match='起播'):
        change(env, 'racing-move', 'move', instance_id=identity, due_at=env.clock.at+1800)
    result = view(env)
    assert result['active'][0]['id'] == identity
    assert not result['adjustments']
    env.alarms.cancel_speech = cancel
    env.alarms.close()


def test_overlapping_alarms_share_light_and_run_without_consuming_future_alarm(tmp_path):
    env = home(tmp_path)
    first = change(env, 'one', 'add_temporary', due_at=env.clock.at+30)['next']
    change(env, 'two', 'add_temporary', due_at=env.clock.at+30)
    later = change(env, 'later', 'add_temporary', due_at=env.clock.at+1000)['upcoming'][-1]
    presses = []
    def press(claim, **kwargs):
        if claim():
            presses.append(env.clock.at)
        return 'requested'
    env.alarms.prepare_light = press
    env.clock.advance(30)
    env.alarms.prepare_environment()
    assert len(presses) == 1
    drive(env, 10)
    assert len(view(env)['active']) == 1
    drive(env, 4, location='desk')
    finished = [v for v in env.alarms.instances.values() if v['state'] == 'completed']
    assert len(finished) == 2
    follower = next(v for v in finished if v.get('covered_by'))
    assert follower['audible_coverage'] is True
    assert follower['first_sound_at'] is None
    assert view(env)['next']['id'] == later['id']
    env.alarms.close()


def test_overlapping_unheard_or_visually_skipped_run_is_not_reported_as_audible(tmp_path):
    env = home(tmp_path)
    change(env, 'one', 'add_temporary', due_at=env.clock.at+30)
    change(env, 'two', 'add_temporary', due_at=env.clock.at+30)
    drive(env, 50, finish=False)
    values = list(env.alarms.instances.values())
    assert all(v['state'] == 'paused' for v in values)
    assert not any(v.get('audible_coverage') for v in values)
    assert len(env.speech.calls) == 1
    env.alarms.close()


def test_edit_after_goodnight_replaces_owned_rtc_without_repeating_lights(tmp_path):
    from spica.home.bedtime import HomeBedtime
    from test_home_bedtime import power, settle
    env = home(tmp_path)
    env.config.wake.power_control_enabled = True
    env.config.wake.suspend_margin_seconds = 10
    adapter, logind, rtc = power(tmp_path)
    adapter.clock = lambda: env.clock.at
    lights = []
    bedtime = env.alarms.bedtime = HomeBedtime(env.config, env.store, adapter,
        clock=lambda: env.clock.mono, wall_clock=lambda: env.clock.at,
        resources_ready=lambda: False, light_off=lambda: lights.append('off'))
    saved = change(env, 'fixed', 'save_fixed', local_time='08:25', weekdays=[0, 1, 2, 3, 4])
    identity = saved['next']['id']
    with env.alarms.conversation('night', 'home-local-control', want_audio=False):
        env.alarms.prepare_bedtime(reply_required=False)
    settle(bedtime, {'waiting_reply'})
    assert int(rtc.read_text()) == stamp('2026-09-21T08:20:00')
    result = change(env, 'move', 'move', instance_id=identity, due_at=stamp('2026-09-21T09:00:00'))
    assert result['bedtime']['rtc_sync']['status'] == 'pending'
    settle(bedtime, {'waiting_reply'})
    assert int(rtc.read_text()) == stamp('2026-09-21T08:55:00')
    change(env, 'earlier-independent', 'add_temporary', due_at=env.clock.at+120)
    settle(bedtime, {'morning_preparation'})
    assert int(rtc.read_text()) == 0  # Too close: remain awake.
    assert lights == ['off'] and 'Suspend' not in logind.calls
    env.alarms.close()


def test_edit_after_suspend_submission_keeps_original_rtc_and_reports_unsynced(tmp_path):
    from spica.home.bedtime import HomeBedtime
    from test_home_bedtime import power, settle
    env = home(tmp_path)
    env.config.wake.power_control_enabled = True
    env.config.wake.suspend_margin_seconds = 10
    adapter, logind, rtc = power(tmp_path)
    adapter.clock = lambda: env.clock.at
    bedtime = env.alarms.bedtime = HomeBedtime(env.config, env.store, adapter,
        clock=lambda: env.clock.mono, wall_clock=lambda: env.clock.at)
    saved = change(env, 'fixed', 'save_fixed', local_time='08:25', weekdays=[0, 1, 2, 3, 4])
    with env.alarms.conversation('night', 'home-local-control', want_audio=False):
        env.alarms.prepare_bedtime(reply_required=False)
    settle(bedtime, {'suspend_requested'})
    old_rtc = rtc.read_text()
    result = change(env, 'move', 'move', instance_id=saved['next']['id'], due_at=stamp('2026-09-21T07:00:00'))
    assert result['bedtime']['rtc_sync']['status'] == 'unconfirmed'
    assert rtc.read_text() == old_rtc
    assert result['next']['due_at'] == stamp('2026-09-21T07:00:00')
    env.alarms.close()


def test_successive_changes_wait_for_old_arm_cleanup_and_use_latest_target(tmp_path):
    from spica.home.bedtime import HomeBedtime
    from test_home_bedtime import power, settle
    env = home(tmp_path)
    env.config.wake.power_control_enabled = True
    env.config.wake.suspend_margin_seconds = 10
    adapter, _, rtc = power(tmp_path)
    adapter.clock = lambda: env.clock.at
    entered, release = threading.Event(), threading.Event()
    prepare, calls = adapter.prepare_suspend, []
    def slow_arm(epoch, cancelled, *, record):
        calls.append(epoch)
        result = prepare(epoch, cancelled, record=record)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
        return result
    adapter.prepare_suspend = slow_arm
    bedtime = env.alarms.bedtime = HomeBedtime(env.config, env.store, adapter,
        clock=lambda: env.clock.mono, wall_clock=lambda: env.clock.at, resources_ready=lambda: False)
    saved = change(env, 'fixed', 'save_fixed', local_time='08:25', weekdays=[0, 1, 2, 3, 4])
    with env.alarms.conversation('night', 'home-local-control', want_audio=False):
        env.alarms.prepare_bedtime(reply_required=False)
    assert entered.wait(1)
    try:
        change(env, 'nine', 'move', instance_id=saved['next']['id'], due_at=stamp('2026-09-21T09:00:00'))
        change(env, 'ten', 'move', instance_id=saved['next']['id'], due_at=stamp('2026-09-21T10:00:00'))
        assert len(calls) == 1
    finally:
        release.set()
    settle(bedtime, {'waiting_reply'})
    assert calls == [stamp('2026-09-21T08:20:00'), stamp('2026-09-21T09:55:00')]
    assert int(rtc.read_text()) == stamp('2026-09-21T09:55:00')
    assert env.store.bedtime()['receipt']['rtc_epoch'] == int(rtc.read_text())
    env.alarms.close()
    assert env.store.bedtime()['rtc_sync']['status'] == 'not_required'


def test_moving_monday_past_tuesday_keeps_tuesdays_fixed_alarm_next(tmp_path):
    env = home(tmp_path)
    saved = change(env, 'fixed', 'save_fixed', local_time='08:25', weekdays=[0, 1, 2, 3, 4])
    result = change(env, 'cross-day', 'move', instance_id=saved['next']['id'], due_at=stamp('2026-09-23T09:00:00'))
    assert result['next']['due_at'] == stamp('2026-09-22T08:25:00')
    env.alarms.close()


def test_legacy_fixed_rules_require_explicit_choice_and_preserve_history(tmp_path):
    env = home(tmp_path)
    for request, clock in [('legacy-one', '08:00'), ('legacy-two', '09:00')]:
        with env.alarms.conversation(request, 'home-local-control'):
            env.alarms.create(local_time=clock, timezone='Asia/Shanghai', weekdays=[0, 1, 2, 3, 4])
    result = view(env)
    assert len(result['fixed_conflicts']) == 2 and result['fixed'] is None
    with pytest.raises(ValueError, match='多个'):
        change(env, 'ambiguous-save', 'save_fixed', local_time='07:00', weekdays=[0, 1, 2, 3, 4])
    selected = result['fixed_conflicts'][1]['id']
    kept = change(env, 'select', 'select_fixed', schedule_id=selected)
    assert kept['fixed']['id'] == selected and not kept['fixed_conflicts']
    assert len(env.store.schedules()) == 2
    assert len({value['schedule_id'] for value in kept['upcoming']}) == 1
    env.alarms.close()


def test_legacy_voice_cancel_cannot_bypass_24_hour_limit(tmp_path):
    from spica.home.tools import register_home_tools
    from spica.plugins.registry import CapabilityRegistry
    env = home(tmp_path)
    future = change(env, 'future', 'add_temporary', due_at=env.clock.at+48*3600)['next']
    registry = CapabilityRegistry()
    register_home_tools(registry, env.alarms)
    with env.alarms.conversation('cancel', 'desktop', user_text='取消后天的闹钟'):
        with pytest.raises(ValueError, match='24'):
            registry.tool_handler('control_wake_alarm')(action='cancel', instance_id=future['id'])
    assert view(env)['next']['id'] == future['id']
    env.alarms.close()


@pytest.mark.parametrize('changed_member', ['leader', 'follower', 'disable-leader'])
def test_editing_unstarted_overlap_preserves_independent_occurrence(tmp_path, changed_member):
    env = home(tmp_path)
    change(env, 'one', 'add_temporary', due_at=env.clock.at+30)
    change(env, 'two', 'add_temporary', due_at=env.clock.at+31)
    env.clock.advance(31)
    env.alarms.prepare_light = lambda claim, **kwargs: 'requested' if claim() else 'unconfirmed'
    env.alarms.prepare_environment()
    leader = next(v for v in env.alarms.instances.values() if not v.get('covered_by'))
    follower = next(v for v in env.alarms.instances.values() if v.get('covered_by'))
    selected = follower if changed_member == 'follower' else leader
    if changed_member == 'disable-leader':
        change(env, 'edit', 'set_enabled', schedule_id=selected['schedule_id'], enabled=False)
    else:
        change(env, 'edit', 'edit_temporary', schedule_id=selected['schedule_id'], due_at=env.clock.at+600)
    remaining = leader if selected is follower else follower
    assert not remaining.get('covered_by')
    drive(env, 10, location='desk')
    assert remaining['state'] == 'completed'
    if changed_member != 'disable-leader':
        assert view(env)['next']['id'] == selected['id']
        assert env.store.get(selected['id'])['state'] == 'scheduled'
    env.alarms.close()


def test_cross_midnight_override_is_visible_and_restore_uses_original_date(tmp_path):
    env = home(tmp_path)
    env.clock.at = stamp('2026-09-21T20:00:00')
    saved = change(env, 'fixed', 'save_fixed', local_time='23:00', weekdays=[0, 1, 2, 3, 4])
    identity = saved['next']['id']
    change(env, 'move', 'move', instance_id=identity, due_at=stamp('2026-09-22T00:30:00'))
    with env.alarms.conversation('restore', 'desktop', user_text='恢复今天固定闹钟的原时间'):
        result = env.alarms.adjust_from_text(action='restore', instance_id=identity, on_date='2026-09-21')
    assert result['next']['due_at'] == stamp('2026-09-21T23:00:00')
    change(env, 'move-again', 'move', instance_id=identity, due_at=stamp('2026-09-22T00:30:00'))
    env.clock.advance(4*3600)
    assert view(env)['adjustments'][0]['id'] == identity
    env.alarms.close()


def test_enabling_after_today_time_does_not_backfill_and_weekly_skips_find_next(tmp_path):
    env = home(tmp_path)
    saved = change(env, 'fixed', 'save_fixed', local_time='08:25', weekdays=[0, 1, 2, 3, 4])
    identity = saved['fixed']['id']
    change(env, 'off', 'set_enabled', schedule_id=identity, enabled=False)
    env.clock.advance(stamp('2026-09-22T08:25:01') - env.clock.at)
    result = change(env, 'on', 'set_enabled', schedule_id=identity, enabled=True)
    assert result['next']['due_at'] == stamp('2026-09-23T08:25:00')
    drive(env, 10)
    assert not env.speech.calls
    result = change(env, 'weekly', 'save_fixed', local_time='08:25', weekdays=[0])
    result = change(env, 'skip-1', 'skip', instance_id=result['next']['id'])
    result = change(env, 'skip-2', 'skip', instance_id=result['next']['id'])
    assert result['next']['due_at'] == stamp('2026-10-12T08:25:00')
    env.alarms.close()


def test_answer_to_bedtime_question_reuses_intent_and_waits_for_new_reply(tmp_path):
    from spica.home.bedtime import HomeBedtime
    from test_home_bedtime import power, settle
    env = home(tmp_path)
    env.config.wake.power_control_enabled = True
    env.config.wake.suspend_margin_seconds = 10
    adapter, logind, _ = power(tmp_path)
    adapter.clock = lambda: env.clock.at
    lights = []
    bedtime = env.alarms.bedtime = HomeBedtime(env.config, env.store, adapter,
        clock=lambda: env.clock.mono, wall_clock=lambda: env.clock.at,
        light_off=lambda: lights.append('off'))
    with env.alarms.conversation('night', 'desktop', user_text='晚安', want_audio=False):
        env.alarms.prepare_bedtime_from_text()
    bedtime.observe_text_delivery('night', True)
    bedtime.step()
    with env.alarms.conversation('answer', 'desktop', user_text='明早九点叫我', want_audio=False):
        result = env.alarms.prepare_bedtime_from_text(wake_date='2026-09-21', wake_time='09:00')
    assert result['wake_datetime'].endswith('09:00:00+08:00')
    settle(bedtime, {'waiting_reply'})
    assert lights == ['off'] and 'Suspend' not in logind.calls
    bedtime.observe_text_delivery('night', True)
    bedtime.step()
    assert 'Suspend' not in logind.calls
    bedtime.observe_text_delivery('answer', True)
    settle(bedtime, {'suspend_requested'})
    assert logind.calls.count('Suspend') == 1
    env.alarms.close()


def test_resumed_bedtime_reply_and_due_label_edit_never_suspend_again(tmp_path):
    from spica.home.bedtime import HomeBedtime
    from test_home_bedtime import power, settle
    env = home(tmp_path)
    env.config.wake.power_control_enabled = True
    env.config.wake.suspend_margin_seconds = 10
    adapter, logind, rtc = power(tmp_path)
    adapter.clock = lambda: env.clock.at
    bedtime = env.alarms.bedtime = HomeBedtime(env.config, env.store, adapter,
        clock=lambda: env.clock.mono, wall_clock=lambda: env.clock.at)
    change(env, 'fixed', 'save_fixed', local_time='20:10', weekdays=list(range(7)))
    with env.alarms.conversation('night', 'home-local-control', want_audio=False):
        env.alarms.prepare_bedtime(reply_required=False)
    settle(bedtime, {'suspend_requested'})
    adapter.suspend_count.write_text('1')
    env.clock.advance(stamp('2026-09-20T20:05:00') - env.clock.at)
    settle(bedtime, {'morning_preparation'})
    with env.alarms.conversation('later', 'desktop', user_text='晚安，今天20:20叫我', want_audio=False):
        env.alarms.prepare_bedtime_from_text(wake_date='2026-09-20', wake_time='20:20')
    settle(bedtime, {'waiting_reply'})
    bedtime.observe_text_delivery('later', True)
    settle(bedtime, {'night'})
    assert int(rtc.read_text()) == 0 and logind.calls.count('Suspend') == 1
    env.clock.advance(stamp('2026-09-20T20:20:01') - env.clock.at)
    bedtime.step()
    change(env, 'label', 'save_fixed', local_time='20:10', weekdays=list(range(7)), label='起床')
    assert bedtime.intent['state'] == 'morning_preparation'
    drive(env, 10)
    assert env.speech.calls and logind.calls.count('Suspend') == 1
    env.alarms.close()


def test_change_after_final_validation_but_before_suspend_submission_can_rearm(tmp_path):
    from spica.home.bedtime import HomeBedtime
    from test_home_bedtime import power, settle
    env = home(tmp_path)
    env.config.wake.power_control_enabled = True
    env.config.wake.suspend_margin_seconds = 10
    adapter, logind, rtc = power(tmp_path)
    adapter.clock = lambda: env.clock.at
    saved = change(env, 'fixed', 'save_fixed', local_time='08:25', weekdays=[0, 1, 2, 3, 4])
    suspend = adapter.suspend
    def change_before_submit(receipt, cancelled, *, validate, **kwargs):
        def race():
            validate()
            change(env, 'racing-time', 'move', instance_id=saved['next']['id'], due_at=stamp('2026-09-21T09:00:00'))
            bedtime.resources_ready = lambda: False
        return suspend(receipt, cancelled, validate=race, **kwargs)
    adapter.suspend = change_before_submit
    bedtime = env.alarms.bedtime = HomeBedtime(env.config, env.store, adapter,
        clock=lambda: env.clock.mono, wall_clock=lambda: env.clock.at)
    with env.alarms.conversation('night', 'home-local-control', want_audio=False):
        env.alarms.prepare_bedtime(reply_required=False)
    settle(bedtime, {'waiting_reply', 'suspend_unknown'})
    assert bedtime.intent['state'] == 'waiting_reply'
    assert int(rtc.read_text()) == stamp('2026-09-21T08:55:00')
    assert 'Suspend' not in logind.calls
    env.alarms.close()


def test_normal_alarm_tool_answer_waits_for_its_own_bedtime_reply(tmp_path):
    from spica.home.bedtime import HomeBedtime
    from test_home_bedtime import power, settle
    env = home(tmp_path)
    env.config.wake.power_control_enabled = True
    env.config.wake.suspend_margin_seconds = 10
    adapter, logind, _ = power(tmp_path)
    adapter.clock = lambda: env.clock.at
    bedtime = env.alarms.bedtime = HomeBedtime(env.config, env.store, adapter,
        clock=lambda: env.clock.mono, wall_clock=lambda: env.clock.at)
    with env.alarms.conversation('night', 'desktop', user_text='晚安', want_audio=False):
        env.alarms.prepare_bedtime_from_text()
    bedtime.observe_text_delivery('night', True)
    bedtime.step()
    try:
        with env.alarms.conversation('answer', 'desktop', user_text='明早九点叫我', want_audio=False):
            env.alarms.set_from_text(local_time='09:00', on_date='2026-09-21')
            settle(bedtime, {'waiting_reply', 'suspend_requested'})
            assert bedtime.intent['request_id'] == 'answer'
            assert 'Suspend' not in logind.calls
            bedtime.observe_text_delivery('night', True)
            bedtime.step()
            assert 'Suspend' not in logind.calls
        bedtime.observe_text_delivery('answer', True)
        settle(bedtime, {'suspend_requested'})
        assert logind.calls.count('Suspend') == 1
    finally:
        env.alarms.close()


@pytest.mark.parametrize('consume_failure_before_due', [True, False])
def test_failed_previous_run_does_not_absorb_a_later_independent_alarm(tmp_path, consume_failure_before_due):
    env = home(tmp_path)
    first = change(env, 'one', 'add_temporary', due_at=env.clock.at+30)['next']
    second = change(env, 'two', 'add_temporary', due_at=env.clock.at+50)['upcoming'][-1]
    drive(env, 36, finish=False)
    env.speech.calls[-1][1].audio_started.set_result(env.clock.mono)
    env.speech.finish(status='failed', audio='failed')
    if not consume_failure_before_due:
        env.clock.advance(14.5)  # Failure arrived before the new due, but no Home tick consumed it.
    drive(env, 30 if consume_failure_before_due else 10)
    assert env.store.get(first['id'])['state'] == 'paused'
    assert env.store.get(second['id'])['first_sound_at'] is not None
    assert not env.store.get(second['id']).get('covered_by')
    env.alarms.close()


def test_delayed_terminal_receipt_does_not_claim_audio_after_it_finished(tmp_path):
    env = home(tmp_path)
    change(env, 'one', 'add_temporary', due_at=env.clock.at+30)
    follower = change(env, 'two', 'add_temporary', due_at=env.clock.at+40)['upcoming'][-1]
    drive(env, 36, finish=False)
    env.speech.finish()  # All audio ended four seconds before the second alarm.
    env.clock.advance(4.5)
    env.alarms.step(room(at=env.clock.at), env.clock.mono, 1)
    assert not env.store.get(follower['id'])['audible_coverage']
    drive(env, 10)
    assert env.store.get(follower['id'])['audible_coverage']
    env.alarms.close()


def test_enable_commit_and_past_occurrence_suppression_survive_same_crash(tmp_path, monkeypatch):
    env = home(tmp_path)
    fixed = change(env, 'fixed', 'save_fixed', local_time='08:25', weekdays=list(range(7)))['fixed']['id']
    change(env, 'off', 'set_enabled', schedule_id=fixed, enabled=False)
    env.clock.advance(stamp('2026-09-22T08:25:01')-env.clock.at)
    original_db = env.store.db
    @contextmanager
    def stop_after_commit():
        with original_db() as db:
            yield db
            wrote = db.total_changes > 0
        if wrote:
            raise RuntimeError('simulated process stop after commit')
    with monkeypatch.context() as patch:
        patch.setattr(env.store, 'db', stop_after_commit)
        with pytest.raises(RuntimeError, match='simulated process stop'):
            change(env, 'on', 'set_enabled', schedule_id=fixed, enabled=True)
    env.alarms.close()
    env.clock.advance(1)
    env.store = AlarmStore(tmp_path/'alarms.sqlite3')
    env.alarms = HomeAlarms(env.config, env.store, **env.kwargs)
    drive(env, 10)
    assert not env.speech.calls
    assert env.store.get(fixed+':2026-09-22')['reason'] == 'plan_time_passed'
    assert view(env)['next']['due_at'] == stamp('2026-09-23T08:25:00')
    env.alarms.close()


def test_voice_resolves_full_target_date_before_24_hour_limit(tmp_path):
    env = home(tmp_path)
    env.clock.at = stamp('2026-09-21T20:00:00')
    change(env, 'fixed', 'save_fixed', local_time='23:00', weekdays=list(range(7)))
    with env.alarms.conversation('move-tomorrow', 'desktop', user_text='明早八点叫我'):
        with pytest.raises(ValueError, match='24'):
            env.alarms.set_from_text(local_time='08:00', on_date='2026-09-22')
    assert not view(env)['temporary']
    change(env, 'temp', 'add_temporary', due_at=stamp('2026-09-22T08:00:00'))
    with env.alarms.conversation('skip-tomorrow', 'desktop', user_text='明天不用叫我'):
        with pytest.raises(ValueError, match='多个'):
            env.alarms.adjust_from_text(action='skip', on_date='2026-09-22')
    assert view(env)['temporary'][0]['enabled']
    env.alarms.close()


def test_distinct_alarm_tools_in_one_turn_are_not_deduplicated_together(tmp_path):
    import json
    from spica.home.tools import register_home_tools
    from spica.plugins.registry import CapabilityRegistry
    from spica.runtime.context import TurnContext, TurnRequest
    from spica.runtime.observer import NoopTurnObserver
    from spica.runtime.tools import RegistryToolSet
    from spica.runtime.tool_round import _run_tool_calls
    env = home(tmp_path)
    change(env, 'fixed', 'save_fixed', local_time='08:25', weekdays=[0, 1, 2, 3, 4])
    registry = CapabilityRegistry()
    register_home_tools(registry, env.alarms)
    text = '把明早固定改九点，并设八点半的临时叫醒'
    fixed = dict(local_time='09:00', on_date='2026-09-21')
    temporary = dict(local_time='08:30', on_date='2026-09-21', additional=True)
    calls = [dict(name='set_wake_alarm', arguments=json.dumps(args)) for args in (fixed, temporary, temporary)]
    ctx = TurnContext(TurnRequest(user_input=text))
    with env.alarms.conversation('same-turn', 'desktop', user_text=text):
        history = _run_tool_calls(ctx, NoopTurnObserver(), RegistryToolSet(registry), lambda *args: None, calls)
    results = [json.loads(item['output']) for item in history]
    assert all(item['ok'] for item in results)
    first, second, repeated = [item['data'] for item in results]
    assert second['revision'] == first['revision']+1 == repeated['revision']
    assert second['fixed']['next']['due_at'] == stamp('2026-09-21T09:00:00')
    assert len(second['temporary']) == 1
    assert second['next']['due_at'] == stamp('2026-09-21T08:30:00')
    env.alarms.close()
