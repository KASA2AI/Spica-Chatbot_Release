"""Desktop alarm input and display use the same Home plan as voice."""
import os
import time
import threading
from types import SimpleNamespace

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')

from PySide6.QtCore import QDate, QTime
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from test_home_alarm_management import home, view


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


def until(qapp, predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(.005)
    assert predicate()


def surface(env):
    def read():
        return view(env)
    def write(action, arguments, *, expected_revision=None, request_id=None):
        with env.alarms.conversation(request_id, 'home-local-control', want_audio=False):
            return env.alarms.manage(action=action, arguments=arguments, expected_revision=expected_revision)
    return SimpleNamespace(home_alarm_plan=read, home_alarm_manage=write)


@pytest.mark.parametrize('save_failed', [False, True])
def test_stop_wake_is_async_and_does_not_wait_for_panel_poll(qapp, save_failed):
    from ui.controllers.alarm_controller import AlarmController
    polling, release, stopped = threading.Event(), threading.Event(), threading.Event()
    calls, feedback = [], []
    def read():
        polling.set()
        release.wait(2)
        raise RuntimeError('poll closed')
    def write(action, arguments, **kwargs):
        calls.append((action, arguments, kwargs))
        stopped.set()
        release.wait(2)
        return {'stopped': ['today'], 'persistence_failed': ['today'] if save_failed else []}
    window = QWidget()
    controller = AlarmController(window, surface_provider=lambda: SimpleNamespace(
        home_alarm_plan=read, home_alarm_manage=write))
    controller.control_feedback.connect(feedback.append)
    try:
        controller.show()
        assert polling.wait(1)
        controller.stop_wake()
        assert stopped.wait(1), 'STOP was queued behind a panel poll'
        assert not release.is_set() and not feedback
        controller.stop_wake()
        assert len(calls) == 1
        assert calls[0][0] == 'stop_current'
        assert calls[0][1]['requested_at'] <= time.time()
        assert calls[0][1]['requested_mono'] <= time.monotonic()
        release.set()
        until(qapp, lambda: bool(feedback))
        assert '本轮叫醒已停止' in feedback[0]
        assert ('保存失败' in feedback[0]) == save_failed
    finally:
        release.set()
        until(qapp, lambda: controller._worker is None and controller._stop_worker is None)
        controller.shutdown()
        window.close()


def test_desktop_edits_and_voice_refresh_share_actual_next_alarm(qapp, tmp_path):
    from ui.controllers.alarm_controller import AlarmController
    env = home(tmp_path)
    window = QWidget()
    host = surface(env)
    controller = AlarmController(window, surface_provider=lambda: host)
    controller.show()
    until(qapp, lambda: controller.panel.fixed_edit_button.isEnabled())
    controller.panel.fixed_edit_button.click()
    editor = controller.editor
    editor.time_edit.setTime(QTime(8, 25))
    editor.save_button.click()
    until(qapp, lambda: controller.editor is None)
    assert '08:25' in controller.panel.next_time.text()
    with env.alarms.conversation('voice', 'desktop', user_text='明早九点叫我'):
        env.alarms.set_from_text(local_time='09:00', on_date='2026-09-21')
    controller.refresh()
    until(qapp, lambda: '09:00' in controller.panel.next_time.text())
    controller.edit_fixed()
    assert controller.editor.time_edit.time() == QTime(8, 25)
    controller.editor.label_edit.setText('工作日')
    controller.editor.save_button.click()
    until(qapp, lambda: controller.editor is None)
    assert view(env)['fixed']['local_time'] == '08:25'
    controller.panel.add_button.click()
    editor = controller.editor
    editor.date_edit.setDate(QDate(2026, 9, 21))
    editor.time_edit.setTime(QTime(8, 30))
    editor.save_button.click()
    until(qapp, lambda: controller.editor is None)
    assert '08:30' in controller.panel.next_time.text()
    assert '09:00' in controller.panel.fixed_detail.text()
    controller.shutdown()
    window.close()
    assert view(env)['next']['kind'] == 'temporary'
    env.alarms.close()


def test_save_waits_for_poll_and_stale_editor_keeps_its_draft(qapp, tmp_path):
    from ui.controllers.alarm_controller import AlarmController
    from test_home_alarm_management import change
    env = home(tmp_path)
    change(env, 'initial', 'save_fixed', local_time='08:25', weekdays=[0, 1, 2, 3, 4])
    window, host = QWidget(), surface(env)
    controller = AlarmController(window, surface_provider=lambda: host)
    controller.show()
    until(qapp, lambda: controller._snapshot is not None)
    controller.edit_fixed()
    editor = controller.editor
    editor.time_edit.setTime(QTime(9, 0))
    read, release = host.home_alarm_plan, threading.Event()
    host.home_alarm_plan = lambda: (release.wait(2), read())[1]
    controller.refresh()
    assert controller._worker is not None
    editor.save_button.click()
    release.set()
    until(qapp, lambda: controller.editor is None)
    assert view(env)['fixed']['local_time'] == '09:00'
    host.home_alarm_plan = read
    controller.edit_fixed()
    editor = controller.editor
    editor.time_edit.setTime(QTime(10, 0))
    change(env, 'another-client', 'save_fixed', local_time='07:00', weekdays=[0, 1, 2, 3, 4])
    controller.refresh()
    until(qapp, lambda: controller._snapshot['fixed']['local_time'] == '07:00')
    editor.save_button.click()
    until(qapp, lambda: '刷新' in editor.status.text())
    assert controller.editor is editor and editor.time_edit.time() == QTime(10, 0)
    assert view(env)['fixed']['local_time'] == '07:00'
    controller.shutdown()
    window.close()
    env.alarms.close()


def test_temporary_switch_preserves_future_date_and_running_card_is_truthful(qapp, tmp_path):
    from ui.controllers.alarm_controller import AlarmController
    from ui.widgets.alarm_panel import AlarmSwitch
    from test_home_alarm_management import change, stamp
    from test_home_alarms import drive
    env = home(tmp_path)
    due = stamp('2026-09-22T09:00:00')
    saved = change(env, 'temporary', 'add_temporary', due_at=due)
    identity = saved['next']['id']
    window, host = QWidget(), surface(env)
    controller = AlarmController(window, surface_provider=lambda: host)
    controller.show()
    until(qapp, lambda: controller._snapshot is not None)
    def switch():
        return controller.panel.temporary_rows.itemAt(0).widget().findChild(AlarmSwitch)
    switch().click()
    until(qapp, lambda: controller._snapshot['next'] is None and not controller._writing)
    switch().click()
    until(qapp, lambda: controller._snapshot['next'] is not None and not controller._writing)
    assert view(env)['next']['id'] == identity and view(env)['next']['due_at'] == due
    env.clock.advance(due - env.clock.at)
    drive(env, 10)
    controller.refresh()
    until(qapp, lambda: bool(controller._snapshot['active']))
    texts = [label.text() for label in controller.panel.temporary_rows.itemAt(0).widget().findChildren(QLabel)]
    assert any('正在叫醒' in text for text in texts)
    assert not any('已结束' in text or '已关闭' in text for text in texts)
    assert switch().isChecked() and not switch().isEnabled()
    controller.shutdown()
    window.close()
    env.alarms.close()


def test_lost_save_reply_retries_the_same_request_without_duplicate_alarm(qapp, tmp_path):
    from ui.controllers.alarm_controller import AlarmController
    env = home(tmp_path)
    window, host = QWidget(), surface(env)
    write, requests = host.home_alarm_manage, []
    def lose_first_reply(*args, **kwargs):
        requests.append(kwargs['request_id'])
        result = write(*args, **kwargs)
        if len(requests) == 1:
            raise TimeoutError('核心响应超时')
        return result
    host.home_alarm_manage = lose_first_reply
    controller = AlarmController(window, surface_provider=lambda: host)
    controller.show()
    until(qapp, lambda: controller._snapshot is not None)
    controller.edit_temporary()
    editor = controller.editor
    editor.save_button.click()
    until(qapp, lambda: '超时' in editor.status.text())
    editor.save_button.click()
    until(qapp, lambda: controller.editor is None)
    assert requests[0] == requests[1]
    assert len(view(env)['temporary']) == 1
    controller.shutdown()
    window.close()
    env.alarms.close()


def test_label_edit_keeps_relative_alarm_seconds(qapp, tmp_path):
    from ui.controllers.alarm_controller import AlarmController
    env = home(tmp_path)
    with env.alarms.conversation('relative', 'desktop', user_text='半小时后叫我'):
        plan = env.alarms.set_from_text(after_minutes=30)
    due = plan['next']['due_at']
    window, host = QWidget(), surface(env)
    controller = AlarmController(window, surface_provider=lambda: host)
    controller.show()
    until(qapp, lambda: controller._snapshot is not None)
    controller.edit_temporary(controller._snapshot['temporary'][0])
    controller.editor.label_edit.setText('小睡')
    controller.editor.save_button.click()
    until(qapp, lambda: controller.editor is None)
    assert view(env)['next']['due_at'] == due
    assert view(env)['temporary'][0]['label'] == '小睡'
    controller.shutdown()
    window.close()
    env.alarms.close()


def test_expired_disabled_temporary_row_reopens_as_new_occurrence(qapp, tmp_path):
    from ui.controllers.alarm_controller import AlarmController
    from ui.widgets.alarm_panel import AlarmSwitch
    from test_home_alarm_management import change
    env = home(tmp_path)
    plan = change(env, 'temp', 'add_temporary', due_at=env.clock.at+60)
    original = plan['next']
    change(env, 'off', 'set_enabled', schedule_id=original['schedule_id'], enabled=False)
    window, host = QWidget(), surface(env)
    controller = AlarmController(window, surface_provider=lambda: host)
    controller.show()
    until(qapp, lambda: controller._snapshot is not None)
    env.clock.advance(61)
    controller.refresh()
    until(qapp, lambda: not controller._snapshot['temporary'][0]['can_enable'])
    controller.panel.temporary_rows.itemAt(0).widget().findChild(AlarmSwitch).click()
    until(qapp, lambda: controller._worker is None and not controller.is_saving)
    assert view(env)['next'] is not None
    assert view(env)['next']['id'] != original['id']
    assert view(env)['next']['due_at'] == original['due_at']+86400
    controller.shutdown()
    window.close()
    env.alarms.close()


def test_stop_persistence_blocks_exit_until_command_completes(qapp):
    from ui.controllers.alarm_controller import AlarmController
    entered, release = threading.Event(), threading.Event()
    def stop(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return {'stopped': True}
    window = QWidget()
    controller = AlarmController(window, surface_provider=lambda: SimpleNamespace(home_alarm_manage=stop))
    try:
        controller.stop_wake()
        until(qapp, entered.is_set)
        assert controller.is_saving
        release.set()
        until(qapp, lambda: not controller.is_saving)
    finally:
        release.set()
        until(qapp, lambda: controller._stop_worker is None)
        controller.shutdown()
        window.close()
