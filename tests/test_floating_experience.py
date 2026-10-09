"""Interaction, priority and lifecycle checks for the added pet experience."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip('PySide6')
from PySide6.QtCore import QAbstractAnimation, QEvent, QPoint
from PySide6.QtGui import QGuiApplication

from test_character_folder_import import add_floating_animation, folder
from test_floating_voice import click, floating, pointer
from test_voice_wake import qapp, voice
from spica.core.character import load_character_package
from spica.core.events import DesktopNoticeEvent


@pytest.fixture
def experience(floating, folder):
    from PIL import Image
    directory, spec = add_floating_animation(folder, interactions=True, idle_actions=True)
    frames = [Image.new('RGBA', (16, 16)) for _ in range(2)]
    for frame, color in zip(frames, ('red', 'blue')):
        frame.paste(color, (1, 1, 15, 15))
    frames[0].save(directory / 'idle.webp', save_all=True, append_images=frames[1:], duration=50, loop=0)
    frames[0].save(directory / 'poster.png')
    for state in ('petting', 'thinking', 'peek', 'notice', 'sleep', 'welcome', 'easter_egg',
                  'listening', 'speaking', 'dragged'):
        spec['states'][state] = dict(spec['states']['idle'], keyframes=['poster.png'])
    (directory / 'animation.json').write_text(json.dumps(spec))
    floating.window.host.character_package = load_character_package(folder)
    floating.controller.set_enabled(True)
    return floating


def test_hover_actions_are_direct_and_stay_reachable_until_pointer_leaves(experience, voice, monkeypatch):
    from ui.controllers import floating_controller as module
    controller, calls = experience.controller, voice[1]
    now = [100.]
    point = [controller.view.frameGeometry().center()]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(module, 'QCursor', SimpleNamespace(pos=lambda: point[0]))
    controller.refresh()
    assert not controller.controls.toolbar.isVisible()
    now[0] += .4
    controller.refresh()
    assert controller.controls.toolbar.isVisible()
    point[0] = controller.controls.frameGeometry().center()
    now[0] += 2.
    controller.refresh()
    assert controller.controls.toolbar.isVisible()
    controller.controls.pet_button.click()
    assert controller.view._reaction == 'petting' and not calls.wake and not calls.text
    controller.controls.talk_button.click()
    name = experience.window.host.character_package.char_name
    assert calls.wake == [name] and controller.view._reaction is None
    voice[0].set_manual_muted(True)
    controller.controls.talk_button.click()
    assert calls.wake == [name]
    point[0] = QPoint(-10000, -10000)
    now[0] += .8
    controller.refresh()
    assert controller.controls.toolbar.isVisible()
    controller.view._menu_open = True
    now[0] += 2.
    controller.refresh()
    assert controller.controls.toolbar.isVisible()
    controller.view._menu_open = False
    controller.refresh()
    assert not controller.controls.toolbar.isVisible()
    controller.controls.expand_button.click()
    assert not controller.enabled and not controller.controls.isVisible()


def test_waiting_is_quiet_and_active_voice_uses_a_small_indicator(experience):
    controls = experience.controller.controls
    controls.present(text='待唤醒 · 麦克风开启', mode='waiting', hovered=False)
    assert not controls.isVisible()
    controls.present(text='待唤醒 · 麦克风开启', mode='waiting', hovered=True)
    assert controls.talk_button.isVisible() and not controls.status.isVisible()
    for mode in ('listening', 'thinking', 'speaking'):
        controls.present(text='语音状态', mode=mode, hovered=False)
        assert controls.status.isVisible() and not controls.toolbar.isVisible()
        assert controls.width() <= 36 and controls.height() <= 36
    controls.present(text='待唤醒 · 麦克风开启', mode='waiting', hovered=False)
    assert not controls.isVisible() and not controls.indicator.timer.isActive()


def test_docking_keeps_visible_hit_area_and_restores_on_click_drag_and_voice(experience):
    view = experience.controller.view
    full_width = view.width()
    bounds = QGuiApplication.screenAt(view.frameGeometry().center()).availableGeometry()
    view.set_docked('left')
    assert view.docked_side == 'left' and view.width() == full_width and view.x() == bounds.left()
    assert not view._dock_from.isNull()
    view._dock_animation.setCurrentTime(200)
    assert view._dock_body_rect().x() < 0 and not view.mask().isEmpty()
    view._dock_animation.setCurrentTime(view._dock_animation.duration())
    assert view.width() < full_width and view.height() < full_width and view._dock_from.isNull()
    assert view._rendered_state == 'peek' and not view.mask().isEmpty()
    click(view)
    assert view.docked_side is None and view.width() == full_width and view._reaction == 'welcome'
    view.set_docked('right')
    view._dock_animation.setCurrentTime(view._dock_animation.duration())
    for size in (72, 96):
        view.set_display_size(size)
        assert view.x() + view.width() == bounds.right() + 1
    assert view.x() + view.width() == bounds.right() + 1
    point = view.frameGeometry().center()
    pointer(view, QEvent.Type.MouseButtonPress, point)
    pointer(view, QEvent.Type.MouseMove, point - QPoint(80, 0))
    assert view.docked_side is None and view._dragging and view._rendered_state == 'dragged'
    assert view._dock_from.isNull() and view._dock_animation.state() == QAbstractAnimation.State.Stopped
    pointer(view, QEvent.Type.MouseButtonRelease, point - QPoint(80, 0))
    view.set_docked('right')
    view.set_state('listening')
    assert view.docked_side is None and view.width() == full_width and view._rendered_state == 'listening'
    view._dock_animation.finished.emit()
    assert view.width() == full_width and view._dock_from.isNull()
    view.set_state('idle')
    view.set_docked('right')
    view.hide()
    assert view._dock_animation.state() == QAbstractAnimation.State.Stopped and view._dock_from.isNull()
    view.show()
    assert view.docked_side == 'right' and view.width() < full_width


def test_native_idle_sleeps_and_real_return_welcomes_once_without_voice(experience, voice, monkeypatch):
    controller = experience.controller
    idle = [301.]
    monkeypatch.setattr(controller._input_activity, 'idle_seconds', lambda: idle[0])
    controller.refresh()
    assert controller.view._state == 'sleep' and controller._dozing
    sleeping = controller.view._movie
    controller.refresh()
    assert controller.view._movie is sleeping
    idle[0] = .1
    controller.refresh()
    assert controller.view._reaction == 'welcome' and not controller._dozing
    controller.view._reaction_timer.timeout.emit()
    controller.refresh()
    assert controller.view._reaction is None and controller.view._state == 'idle'
    assert not voice[1].wake and not voice[1].text
    idle[0] = 400.
    experience.window.chat_stream_controller.is_busy = lambda: True
    controller.refresh()
    assert controller.view._state == 'thinking' and not controller._dozing


def test_edge_docking_stays_on_screen_when_work_area_extends_past_it(experience, monkeypatch):
    controller, view = experience.controller, experience.controller.view
    screen = QGuiApplication.screenAt(view.frameGeometry().center())
    geometry = screen.geometry()
    monkeypatch.setattr(screen, 'availableGeometry', lambda: geometry.adjusted(12, 12, 80, 30))
    view.move_visible(QPoint(geometry.right() + 80, geometry.top() + 100))
    assert view.frameGeometry().right() <= geometry.right()
    view.set_reduced_motion(True)
    point = view.frameGeometry().center()
    pointer(view, QEvent.Type.MouseButtonPress, point)
    point += QPoint(40, 0)
    pointer(view, QEvent.Type.MouseMove, point)
    pointer(view, QEvent.Type.MouseButtonRelease, point)
    assert view.docked_side == 'right'
    assert view.frameGeometry().right() == geometry.right()
    controller.controls.present(text='正在聆听', mode='listening', hovered=True)
    controller.controls.place_near(view.bubble_anchor())
    assert geometry.contains(controller.controls.frameGeometry())
    controller.bubble.set_dialogue('Spica', '任务完成')
    controller.bubble.place_near(view.bubble_anchor())
    assert geometry.contains(controller.bubble.frameGeometry())


def test_notice_is_consumed_once_and_its_entry_disappears(experience, voice, monkeypatch):
    from ui.controllers import dialogue_visibility_controller as dialogue_module
    from ui.controllers import floating_controller as floating_module
    now = [100.]
    clock = SimpleNamespace(monotonic=lambda: now[0])
    monkeypatch.setattr(dialogue_module, 'time', clock)
    monkeypatch.setattr(floating_module, 'time', clock)
    controller = experience.controller
    play = Mock(wraps=controller.view.play_interaction)
    monkeypatch.setattr(controller.view, 'play_interaction', play)
    notice = DesktopNoticeEvent('result:1', '任务结果', '已生成本地预览')
    controller.show_notice(notice)
    controller.show_notice(notice)
    controller.refresh()
    assert controller.view._reaction == 'notice'
    assert [c.args for c in play.call_args_list] == [('notice',)]
    now[0] += 8.
    controller.refresh()
    assert not controller.bubble.isVisible() and not controller.controls.notice_button.isVisible()
    assert controller._current_notice is None
    assert not experience.window.dialogue_visibility_controller.has_pending_notices
    controller.show_notice(notice)
    controller.refresh()
    assert not controller.bubble.isVisible() and not controller.controls.notice_button.isVisible()
    assert [c.args for c in play.call_args_list] == [('notice',)]
    second = DesktopNoticeEvent('result:2', '任务结果', '第二张纸条')
    third = DesktopNoticeEvent('result:3', '任务结果', '第三张纸条')
    controller.show_notice(second)
    controller.show_notice(third)
    controller.refresh()
    assert controller.bubble._text == second.message and controller.controls.notice_button.isVisible()
    controller.controls.notice_button.click()
    assert controller.bubble.isVisible() and controller.bubble._text == third.message
    controller.controls.notice_button.click()
    assert not controller.bubble.isVisible() and not voice[1].wake and not voice[1].text
    assert not controller.controls.notice_button.isVisible() and controller._current_notice is None
    assert not experience.window.dialogue_visibility_controller.has_pending_notices




def test_reduced_motion_is_persisted_and_uses_still_pose(experience, tmp_path):
    from ui.overlay_config import load_floating_preferences, save_voice_ui_preference
    controller, view = experience.controller, experience.controller.view
    controller._set_reduced_motion(True)
    assert ('floating_reduced_motion', True) in experience.writes
    assert view._movie is None and not view._idle_timer.isActive()
    assert view.play_interaction('petting')
    assert view._movie is None and view._reaction_timer.isActive()
    view.set_state('thinking')
    assert view._reaction is None and view._movie is None
    view._dragging = view._shaken = True
    view._render_state()
    assert view._rendered_state == 'dizzy_held'
    assert view._frame.toImage().pixelColor(8, 8).name() == '#800080'
    view._reset_gesture()
    view.set_state('idle')
    view.set_docked('left')
    assert view.height() < view._base_size and view._dock_from.isNull()
    assert view._dock_animation.state() == QAbstractAnimation.State.Stopped
    path = tmp_path / 'overlay.json'
    assert save_voice_ui_preference('floating_reduced_motion', True, path)
    assert load_floating_preferences(path)['reduced_motion'] is True


def test_rare_easter_egg_has_cooldown_and_yields_to_speech(experience, monkeypatch):
    from ui.widgets import floating_character as module
    view = experience.controller.view
    monkeypatch.setattr(module.random, 'random', lambda: 0.)
    view._last_easter_at = module.time.monotonic() - 301.
    view._play_idle_action()
    assert view._reaction == 'easter_egg'
    old_movie = view._movie
    view.set_state('speaking')
    assert view._reaction is None and view._rendered_state == 'speaking'
    old_movie.finished.emit()
    assert view._rendered_state == 'speaking'
    view.set_state('idle')
    view._play_idle_action()
    assert view._reaction.startswith('idle:')


def test_status_uses_capture_owner_and_controls_close_with_surface(experience, voice):
    controller = experience.controller
    voice[0].set_manual_muted(True)
    controller._mute_feedback = '采音尚未释放；已禁止新录音，请检查设备连接'
    controller.refresh()
    assert '尚未释放' in controller.controls.status.toolTip()
    controller.set_enabled(False)
    assert not controller.controls.isVisible() and not controller.controls.indicator.timer.isActive()
    controller.set_enabled(True)
    controller.shutdown()
    assert not controller.controls.isVisible() and not controller.controls.indicator.timer.isActive()


def test_settings_cover_pauses_notice_read_timer(experience, monkeypatch):
    from ui.controllers import dialogue_visibility_controller as module
    from PySide6.QtWidgets import QWidget
    now = [100.]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    controller, window = experience.controller, experience.window
    controller.set_enabled(False)
    panel = window.settings_panel = QWidget(window)
    panel.show()
    controller.show_notice(DesktopNoticeEvent('settings-cover', '任务', '结果'))
    now[0] += 10.
    controller.refresh()
    visibility = window.dialogue_visibility_controller
    assert visibility.has_pending_notices and not visibility._notice_until
    panel.hide()
    controller.refresh()
    now[0] += .1
    controller.refresh()
    now[0] += 9.
    controller.refresh()
    assert not visibility.has_pending_notices


def test_feedback_cover_never_counts_as_presented_reply(qapp):
    from ui.controllers.dialogue_visibility_controller import DialogueVisibilityController
    from ui.widgets.dialogue_box import TintedDialogueBox
    widget = TintedDialogueBox()
    controller = DialogueVisibilityController(widget)
    try:
        widget.show()
        controller.show_dialogue_line('角色的回复。')
        assert controller.reply_text_visible
        controller.show_system_message('设置已保存')
        assert not controller.reply_text_visible
        controller.show_dialogue_line('新的回复。')
        assert controller.reply_text_visible
    finally:
        widget.close()
        widget.deleteLater()
