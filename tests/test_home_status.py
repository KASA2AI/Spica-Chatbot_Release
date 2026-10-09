"""Home status is a read-only view, not a second automation owner."""
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout

from ui.controllers.home_status_controller import HomeStatusController
from ui.widgets.home_status_page import HomeStatusPage


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def snapshot(now):
    return dict(updated_at=now, running=True, daily_detection_enabled=True,
        connection='connected', room_occupied=None, door_contact=False, illuminance=2,
        sensors={name: dict(observed_at=now-2, valid_until=now+30) for name in ('presence', 'door', 'illuminance')},
        camera_running=True, camera_reasons=['occupancy'], camera_error='',
        room=dict(desk_occupied=False, bed_occupied=True, outside_bed_occupied=False, reason='observed'),
        next_alarm=dict(due_at=now+3600, kind='fixed'),
        display_result=dict(status='wake_requested'),
        commands=[dict(kind='light', action='on', source='wake_alarm', requested_at=now-10, press_status='requested')],
        wake=dict(enabled=True, instances=[], last_execution=dict(due_at=now-60, state='completed',
            reason='bed_empty_with_person_outside', first_sound_at=now-59)),
        greetings=dict(welcome=dict(at=now-20, state='skipped', reason='busy')))


def idle(qapp, predicate):
    deadline = time.monotonic()+3
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(.005)
    assert predicate()


def test_status_distinguishes_unknown_receipts_and_stale_observations(qapp):
    page = HomeStatusPage()
    try:
        value = snapshot(1000)
        page.load_snapshot(value, {'available': True, 'name': '床边音箱', 'fallback': True}, now=1000)
        assert page.values['presence'].text().startswith('未知')
        assert '床边音箱' in page.values['output'].text() and '备用' in page.values['output'].text()
        assert '尚无物理亮屏回执' in page.values['display'].text()
        assert '实际灯态未确认' in page.values['light'].text()
        assert '床区无人且床外持续有人' in page.values['wake'].text()
        assert '已跳过' in page.values['welcome'].text() and '当前有活动' in page.values['welcome'].text()
        value['room_occupied'] = False
        page.load_snapshot(value, {'available': False, 'detail': '指定音箱离线'}, now=1000)
        assert page.values['presence'].text().startswith('未检出人')
        assert page.values['output'].text() == '指定音箱离线'
        page.load_snapshot(value, {'available': False}, now=1006)
        assert '上次状态' in page.values['mode'].text()
        assert '过期' in page.values['presence'].text()
        assert '过期' in page.values['regions'].text()
        page.unavailable('连接失败')
        assert page.values['regions'].text() == '—'
        assert page.values['mode'].text() == '连接失败'
    finally:
        page.deleteLater()
        qapp.sendPostedEvents(page, QEvent.Type.DeferredDelete)


def test_status_polling_stops_when_hidden_and_rejects_late_results(qapp):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def read():
        calls.append(threading.current_thread())
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        return snapshot(time.time())
    window = QWidget()
    window.host = SimpleNamespace(home_status=read, plugin_host=SimpleNamespace(errors=lambda: {}), config=SimpleNamespace(home=SimpleNamespace(enabled=True)))
    window.audio_controller = SimpleNamespace(output_status=lambda _: {'available': True, 'name': '测试音箱'})
    page = HomeStatusPage(window)
    QVBoxLayout(window).addWidget(page)
    controller = HomeStatusController(window, page)
    try:
        window.show()
        idle(qapp, entered.is_set)
        controller.refresh()
        assert len(calls) == 1 and calls[0] is not threading.main_thread()
        page.hide()
        assert not controller._timer.isActive()
        release.set()
        idle(qapp, lambda: controller._worker is None)
        assert page.values['mode'].text() == '正在读取 Home 状态…'
        page.show()
        idle(qapp, lambda: controller._worker is None)
        assert len(calls) == 2 and controller._timer.isActive()
        assert page.values['mode'].text() == '日常运行'
        window.host.home_status = lambda: None
        controller.refresh()
        idle(qapp, lambda: controller._worker is None)
        assert controller._snapshot is None
        assert '未启动' in page.values['mode'].text()
        window.host.home_status = Mock(side_effect=ConnectionError('offline'))
        controller.refresh()
        idle(qapp, lambda: controller._worker is None)
        assert '读取失败' in page.values['mode'].text()
        assert page.values['next'].text() == '—'
        controller.shutdown()
        controller.refresh()
        assert not controller._timer.isActive() and controller._worker is None
    finally:
        release.set()
        controller.shutdown()
        window.close()
        window.deleteLater()
        qapp.sendPostedEvents(window, QEvent.Type.DeferredDelete)


def test_status_expires_while_a_read_is_pending_and_setup_never_starts_core(qapp):
    window = QWidget()
    window._setup_only = True
    window.host = SimpleNamespace(home_status=Mock(side_effect=AssertionError('setup must not start core')))
    page = HomeStatusPage(window)
    QVBoxLayout(window).addWidget(page)
    controller = HomeStatusController(window, page)
    try:
        window.show()
        qapp.processEvents()
        assert '配置模式' in page.values['mode'].text()
        window.host.home_status.assert_not_called()
        window._setup_only = False
        controller._snapshot = snapshot(time.time()-10)
        controller._audio = {'available': False}
        controller._worker = object()  # A pending request cannot keep the last result fresh.
        controller._tick()
        assert '过期' in page.status.text()
        assert '上次状态' in page.values['mode'].text()
    finally:
        controller.shutdown()
        window.close()
        window.deleteLater()
        qapp.sendPostedEvents(window, QEvent.Type.DeferredDelete)


def test_audio_status_uses_the_real_binding_without_opening_a_player(qapp, monkeypatch):
    from ui.controllers import audio_controller as module
    def device(identity):
        return SimpleNamespace(id=lambda: identity.encode(), description=lambda: identity, isNull=lambda: False)
    desk, bed, backup = [device(name) for name in ('desk', 'bed', 'backup')]
    outputs = [desk, bed, backup]
    provider = SimpleNamespace(audioOutputs=lambda: outputs, defaultAudioOutput=lambda: desk)
    monkeypatch.setattr(module, 'QMediaDevices', provider)
    monkeypatch.setattr(module, 'QMediaPlayer', Mock(side_effect=AssertionError('status cannot play')))
    monkeypatch.setattr(module, 'QAudioOutput', Mock(side_effect=AssertionError('status cannot open output')))
    audio = module.AudioController(None, output_device_id=b'desk'.hex())
    audio.set_home_output_device(b'bed'.hex(), fallback_device_id=b'backup'.hex())
    assert audio.output_status()['name'] == 'bed'
    outputs.remove(bed)
    assert audio.output_status()['name'] == 'backup' and audio.output_status()['fallback']
    outputs.remove(backup)
    assert audio.output_status()['available'] is False
    assert audio.output_status('daily')['name'] == 'desk'
    audio.deleteLater()
