"""The miniature is a presentation of the existing session, never another one."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip('PySide6')
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QMouseEvent, QMovie, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from test_character_folder_import import folder, add_floating_animation
from test_voice_wake import voice, qapp
from spica.core.character import load_character_package
from ui.widgets.floating_character import FloatingCharacterWindow


@pytest.fixture
def interactive_view(qapp, folder):
    import json
    directory, spec = add_floating_animation(folder, interactions=True)
    spec['states'].update({state: dict(spec['states']['idle'])
                           for state in ('listening', 'speaking', 'dragged')})
    (directory / 'animation.json').write_text(json.dumps(spec))
    view = FloatingCharacterWindow()
    view.set_package(load_character_package(folder))
    view.show()
    qapp.processEvents()
    yield view
    view.shutdown()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def idle_action_view(qapp, folder):
    add_floating_animation(folder, interactions=True, idle_actions=True)
    view = FloatingCharacterWindow()
    view.set_package(load_character_package(folder))
    view.show()
    yield view
    view.shutdown()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_idle_actions_wait_choose_by_weight_and_play_once(idle_action_view, monkeypatch):
    from ui.widgets import floating_character as module
    view = idle_action_view
    selected = []
    def choose(population, weights, k):
        selected.append(dict(zip(population, weights)))
        return ['breeze']
    monkeypatch.setattr(module.random, 'choices', choose)
    assert view._idle_timer.isActive() and 30000 <= view._idle_timer.interval() <= 45000
    assert view._reaction is None
    view._idle_timer.timeout.emit()
    assert selected == [{'grass_flute': 1, 'pizza': 1, 'breeze': 4}]
    assert view._reaction == 'idle:breeze' and not view._idle_timer.isActive()
    movie = view._movie
    assert movie.cacheMode() == QMovie.CacheMode.CacheNone
    movie.jumpToNextFrame()
    assert view._reaction_timer.isActive()
    view._reaction_timer.timeout.emit()
    assert view._reaction is None and view._rendered_state == 'idle'
    assert view._idle_timer.isActive()
    movie.finished.emit()
    assert view._reaction is None
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert len(view.findChildren(QMovie)) == 1


@pytest.mark.parametrize('interrupt', ['listening', 'speaking', 'press', 'click', 'hide', 'replace'])
def test_idle_actions_yield_and_old_movie_cannot_resume_them(idle_action_view, interrupt):
    view = idle_action_view
    view._idle_timer.timeout.emit()
    old_movie = view._movie
    if interrupt in ('listening', 'speaking'):
        view.set_state(interrupt)
    elif interrupt == 'press':
        pointer(view, QEvent.Type.MouseButtonPress, view.pos() + QPoint(30, 30))
    elif interrupt == 'click':
        click(view)
    elif interrupt == 'hide':
        view.hide()
    else:
        view.set_package(SimpleNamespace(manifest=None))
    assert view._reaction == ('click' if interrupt == 'click' else None)
    assert not view._idle_timer.isActive()
    state, movie = view._rendered_state, view._movie
    old_movie.finished.emit()
    view._idle_timer.timeout.emit()  # A late timer cannot reclaim the presentation.
    assert view._rendered_state == state and view._movie is movie


def test_failed_idle_asset_falls_back_without_repeated_retry(idle_action_view):
    view = idle_action_view
    for action in view._spec.idle_actions.values():
        (view._root / action.file).unlink()
    errors = []
    view.resource_error.connect(errors.append)
    for _ in range(3):
        view._idle_timer.timeout.emit()
        assert view._rendered_state == 'idle' and view._reaction is None
    assert len(errors) == 3 and not view._idle_timer.isActive()


def test_idle_menu_shutdown_never_rearms_a_destroyed_view(idle_action_view):
    view = idle_action_view
    view._idle_timer.timeout.emit()
    def exit_from_menu(_point):
        assert view._reaction is None and not view._idle_timer.isActive()
        view._play_idle_action()
        assert view._reaction is None
        view.shutdown()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    view.menu_requested.connect(exit_from_menu)
    view.contextMenuEvent(SimpleNamespace(globalPos=lambda: QPoint()))
    assert view._closed
    view._play_idle_action()


def pointer(view, event_type, point):
    button = Qt.MouseButton.NoButton if event_type == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
    buttons = Qt.MouseButton.NoButton if event_type == QEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton
    event = QMouseEvent(event_type, QPointF(point - view.pos()), QPointF(point), button,
                        buttons, Qt.KeyboardModifier.NoModifier)
    method = {QEvent.Type.MouseButtonPress: view.mousePressEvent,
              QEvent.Type.MouseButtonDblClick: view.mouseDoubleClickEvent,
              QEvent.Type.MouseMove: view.mouseMoveEvent,
              QEvent.Type.MouseButtonRelease: view.mouseReleaseEvent}[event_type]
    method(event)


def click(view):
    point = view.pos() + QPoint(30, 30)
    pointer(view, QEvent.Type.MouseButtonPress, point)
    pointer(view, QEvent.Type.MouseButtonRelease, point)


def test_click_restarts_once_then_restores_latest_voice_state(interactive_view):
    view = interactive_view
    click(view)
    assert view._rendered_state == 'click'
    first = view._movie
    first.jumpToFrame(1)
    assert view._reaction_timer.isActive()
    click(view)
    current = view._movie
    assert current is not first and current.currentFrameNumber() == 0
    first.finished.emit()  # A superseded animation cannot end the new click.
    assert view._movie is current and view._reaction == 'click'
    view.set_state('speaking')
    assert view._rendered_state == 'click'
    QTest.qWait(200)  # The authored fixture loops forever; a click must not.
    assert view._rendered_state == 'speaking' and view._reaction is None
    assert not view._reaction_timer.isActive()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert len(view.findChildren(QMovie)) == 1


def test_three_clicks_including_native_double_click_request_one_wake(interactive_view):
    view, wakes = interactive_view, []
    view.wake_requested.connect(lambda: wakes.append(True))
    point = view.pos() + QPoint(30, 30)
    for event_type in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick,
                       QEvent.Type.MouseButtonPress):
        pointer(view, event_type, point)
        pointer(view, QEvent.Type.MouseButtonRelease, point)
    assert wakes == [True] and view._reaction is None
    click(view)
    assert wakes == [True] and view._reaction == 'click'




@pytest.mark.parametrize('interruption', ['drag', 'slow', 'hold', 'hide'])
def test_interrupted_click_sequence_does_not_wake(interactive_view, monkeypatch, interruption):
    from ui.widgets import floating_character as module
    view, now, wakes = interactive_view, [1.], []
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    view.wake_requested.connect(lambda: wakes.append(True))
    click(view)
    click(view)
    point = view.pos() + QPoint(30, 30)
    if interruption == 'drag':
        pointer(view, QEvent.Type.MouseButtonPress, point)
        pointer(view, QEvent.Type.MouseMove, point + QPoint(20, 0))
        pointer(view, QEvent.Type.MouseButtonRelease, point + QPoint(20, 0))
    elif interruption == 'slow':
        now[0] += module.QApplication.doubleClickInterval() / 1000 + .1
    elif interruption == 'hold':
        pointer(view, QEvent.Type.MouseButtonPress, point)
        now[0] += module.QApplication.doubleClickInterval() / 1000 + .1
        pointer(view, QEvent.Type.MouseButtonRelease, point)
    else:
        view.hide()
        view.show()
    click(view)
    assert wakes == []


def test_repeated_shaking_holds_pose_and_release_recovers(interactive_view, monkeypatch):
    from ui.widgets import floating_character as module
    view, now = interactive_view, [1.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    point = QPoint(300, 300)
    pointer(view, QEvent.Type.MouseButtonPress, point)
    for index, dx in enumerate((80, -80, 80, -80, 80)):
        now[0] += .1
        point += QPoint(dx, 0)
        pointer(view, QEvent.Type.MouseMove, point)
        if index == 0:
            dragged_movie = view._movie
        if index < 4:
            assert view._rendered_state == 'dragged', 'Keep the lifted pose until dizzy; no normal-eye intermediate.'
            assert view._movie is dragged_movie
    assert view._rendered_state == 'dizzy_held' and view._movie is None
    assert view._frame.toImage() == QPixmap(str(view._root / view._spec.dizzy_held)).toImage()
    view.set_state('listening')
    now[0] += 3.0
    point += QPoint(80, 0)
    pointer(view, QEvent.Type.MouseMove, point)
    assert view._rendered_state == 'dizzy_held', 'A triggered held pose latches until release.'
    positions = []
    view.position_changed.connect(positions.append)
    pointer(view, QEvent.Type.MouseButtonRelease, point)
    assert view._rendered_state == 'dizzy' and len(positions) == 1
    QTest.qWait(200)
    assert view._rendered_state == 'listening' and view._reaction is None


def test_dizzy_release_starts_at_authored_recovery_frame(qapp, folder):
    import json
    directory, spec = add_floating_animation(folder, interactions=True)
    spec['states']['dizzy']['start_frame'] = 1
    (directory / 'animation.json').write_text(json.dumps(spec))
    view = FloatingCharacterWindow()
    try:
        view.set_package(load_character_package(folder))
        view.show()
        point = QPoint(300, 300)
        pointer(view, QEvent.Type.MouseButtonPress, point)
        view._dragging = view._shaken = True
        view._render_state()
        pointer(view, QEvent.Type.MouseButtonRelease, point)
        assert view._rendered_state == 'dizzy'
        assert view._movie.currentFrameNumber() == 1
        assert view._frame.toImage().pixelColor(5, 5).blue() > 240
        QTest.qWait(100)
        assert view._rendered_state == 'idle'
    finally:
        view.shutdown()


def test_pet_size_tracks_monitor_height_and_preserves_drag(interactive_view, monkeypatch):
    from ui.widgets import floating_character as module
    class Screen:
        availableGeometryChanged = SimpleNamespace(connect=lambda cb: None, disconnect=lambda cb: None)
        geometryChanged = availableGeometryChanged
        logicalDotsPerInchChanged = availableGeometryChanged
        def __init__(self, rect, dpr):
            self.rect, self.dpr = rect, dpr
        def availableGeometry(self):
            return self.rect
        def geometry(self):
            return self.rect
        def devicePixelRatio(self):
            return self.dpr
    current = [Screen(QRect(0, 0, 1920, 1080), 1.)]
    monkeypatch.setattr(module, 'QGuiApplication', SimpleNamespace(
        screenAt=lambda point: current[0], primaryScreen=lambda: current[0]))
    view = interactive_view
    view.set_display_size(128)
    view.move_visible(QPoint(300, 300))
    assert view.height() == 216  # Large is 20% of the screen's logical height.
    pointer(view, QEvent.Type.MouseButtonPress, QPoint(350, 350))
    pointer(view, QEvent.Type.MouseMove, QPoint(430, 350))
    current[0] = Screen(QRect(1920, 0, 3840, 2160), 1.)
    pointer(view, QEvent.Type.MouseMove, QPoint(2500, 350))
    assert view.height() == 432 and view._dragging and view._rendered_state == 'dragged'
    assert current[0].rect.contains(view.geometry())
    current[0] = Screen(QRect(0, 0, 1920, 1080), 2.)
    view.move_visible(QPoint(300, 300))
    assert view.height() == 216, 'Qt already supplies logical coordinates at 200% scaling.'
    view.set_display_size(72)
    assert view.height() < 216


@pytest.mark.parametrize('steps,delay', [
    ([8, -8] * 8, .05),  # Small shakes do not accumulate into full legs.
    ([80] * 8, .1),      # Moving in one direction is an ordinary drag.
    ([80, -80] * 4, 1.), # Slow back-and-forth movement is also an ordinary drag.
    ([160, -160] * 4, .7), # Fast legs, but not enough reversals inside 1.8 seconds.
])
def test_ordinary_drag_never_clicks_or_gets_dizzy(interactive_view, monkeypatch, steps, delay):
    from ui.widgets import floating_character as module
    view, now = interactive_view, [1.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    point = QPoint(300, 300)
    pointer(view, QEvent.Type.MouseButtonPress, point)
    for dx in steps:
        now[0] += delay
        point += QPoint(dx, 0)
        pointer(view, QEvent.Type.MouseMove, point)
        assert not view._shaken
    pointer(view, QEvent.Type.MouseButtonRelease, point)
    assert view._rendered_state == 'idle' and view._reaction is None


@pytest.mark.parametrize('cancel', ['hide', 'replace'])
def test_interaction_cancellation_ignores_old_release_and_timer(interactive_view, cancel):
    view = interactive_view
    click(view)
    old = view._movie
    old.jumpToFrame(1)
    point = view.pos() + QPoint(30, 30)
    pointer(view, QEvent.Type.MouseButtonPress, point)
    if cancel == 'hide':
        view.hide()
    else:
        view.set_package(SimpleNamespace(manifest=None))
    old.finished.emit()
    pointer(view, QEvent.Type.MouseButtonRelease, point)
    QTest.qWait(150)
    assert view._movie is None and view._reaction is None and view._press is None
    assert not view._reaction_timer.isActive()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not view.findChildren(QMovie)
    if cancel == 'hide':
        view.show()
        assert view._rendered_state == 'idle'


def test_opaque_poster_keeps_an_explicit_click_region(qapp):
    view = FloatingCharacterWindow()
    poster = QPixmap(16, 16)
    poster.fill(Qt.GlobalColor.red)
    assert not poster.hasAlphaChannel()
    try:
        view.set_package(SimpleNamespace(manifest=None), poster)
        view.show()
        qapp.processEvents()
        assert view.mask().boundingRect() == view.rect()
        assert view.mask().contains(view.rect().center())
    finally:
        view.shutdown()
        qapp.processEvents()


def test_one_visible_decoder_drag_uses_current_state_and_old_roles_fall_back(qapp, folder):
    directory, spec = add_floating_animation(folder)
    import json
    from PIL import Image
    # Exercise a real silhouette, rather than an opaque rectangular movie.
    frames = [Image.new('RGBA', (16, 16)) for _ in range(2)]
    for frame, color in zip(frames, ('red', 'blue')):
        frame.paste(color, (4, 4, 12, 12))
    frames[0].save(directory / 'idle.webp', save_all=True, append_images=frames[1:], duration=50, loop=0)
    spec['states'].update({state: dict(spec['states']['idle']) for state in ('listening', 'speaking', 'dragged')})
    (directory / 'animation.json').write_text(json.dumps(spec))
    package = load_character_package(folder)
    view = FloatingCharacterWindow()
    fallback = QPixmap(16, 16)
    fallback.fill(Qt.GlobalColor.green)
    try:
        view.set_package(package, fallback)
        assert view._movie is None
        view.show()
        qapp.processEvents()
        assert view._movie is not None and view._movie.cacheMode() == QMovie.CacheMode.CacheNone
        idle = view._movie
        click(view)
        assert view._movie is idle, 'Older packages without click assets keep their current animation.'
        def mouse(event_type, point, button=Qt.MouseButton.LeftButton):
            return QMouseEvent(event_type, QPointF(20, 20), QPointF(point), button,
                               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        view.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, view.pos() + QPoint(20, 20)))
        view.mouseMoveEvent(mouse(QEvent.Type.MouseMove, view.pos() + QPoint(60, 60)))
        assert view._rendered_state == 'dragged' and view._movie is not idle
        assert not view.mask().isEmpty(), 'Dragging must retain the current silhouette on X11.'
        view.set_state('speaking')
        assert view._rendered_state == 'dragged'
        view.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, view.pos() + QPoint(20, 20)))
        assert view._rendered_state == 'speaking'
        current = view._movie
        view.hide()
        assert view._movie is None and current.state() == QMovie.MovieState.NotRunning
        view.set_package(SimpleNamespace(manifest=None, character_id='other'), fallback)
        view.show()
        assert view._movie is None
        assert view._frame.toImage().pixelColor(2, 2) == Qt.GlobalColor.green
        view.move_visible(QPoint(-1000000, -1000000))
        assert view.screen().availableGeometry().contains(view.geometry())
    finally:
        view.shutdown()
        qapp.processEvents()


def test_broken_state_keeps_same_role_static_image(qapp, folder):
    directory, _ = add_floating_animation(folder)
    package = load_character_package(folder)
    view = FloatingCharacterWindow()
    try:
        view.set_package(package)
        (directory / 'idle.webp').unlink()
        errors = []
        view.resource_error.connect(errors.append)
        view.show()
        assert view._movie is None and not view._frame.isNull() and len(errors) == 1
        view.set_state('listening')  # missing state also falls back
        assert view._movie is None and not view._frame.isNull()
    finally:
        view.shutdown()
        qapp.processEvents()


@pytest.fixture
def floating(voice, monkeypatch):
    from PySide6.QtCore import QObject, Signal
    from ui.controllers import floating_controller as module
    controller, _ = voice
    window = QWidget()
    window.host = SimpleNamespace(character_package=SimpleNamespace(
        character_id='spica', char_name='Spica', revision='one', package_root=None, manifest=None))
    window.current_pixmap = QPixmap()
    window.agent = object()
    window.voice_input_controller = controller
    window.audio_controller = SimpleNamespace(is_voice_playing=lambda: False)
    from ui.controllers.dialogue_visibility_controller import DialogueVisibilityController
    from ui.widgets.dialogue_box import TintedDialogueBox
    window.dialogue_visibility_controller = DialogueVisibilityController(TintedDialogueBox(window))
    window.dialogue_visibility_controller.show_system_message = Mock()
    window.dialogue_visibility_controller.show_notice_message = Mock(
        wraps=window.dialogue_visibility_controller.show_notice_message)
    window._bind_voice_reply = Mock()
    window._layout_overlay = Mock()
    window._load_voice_wake_preferences = Mock()
    window.open_settings_panel = Mock()
    window.chat_stream_controller = SimpleNamespace(is_busy=lambda: False, stream_session_id=1)
    writes = []
    monkeypatch.setattr(module, 'load_floating_preferences', lambda: dict(
        enabled=False, muted=False, size=96, position=None))
    monkeypatch.setattr(module, 'save_voice_ui_preference', lambda k, v: writes.append((k, v)) or True)
    floating = module.FloatingController(window)
    monkeypatch.setattr(floating._input_activity, 'idle_seconds', lambda: 0.)
    notices = []
    monkeypatch.setattr(floating, 'notify', notices.append)
    try:
        yield SimpleNamespace(controller=floating, window=window, writes=writes, notices=notices)
    finally:
        controller.speech_worker = None
        floating.shutdown()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_switching_surface_preserves_session_and_mute_waits_for_capture_owner(voice, floating):
    controller, _ = voice
    window, host, writes = floating.window, floating.window.host, floating.writes
    floating = floating.controller
    try:
        window.show()
        controller.start()
        floating.set_enabled(True)
        assert controller.daily_active and not controller.voice_mode_active
        assert not window.isVisible() and floating.view.isVisible()
        floating.set_enabled(False)
        assert window.isVisible() and not floating.view.isVisible()
        assert window.host is host and window.voice_input_controller is controller
        assert controller.daily_active and not controller.voice_mode_active
        worker = SimpleNamespace(isRunning=lambda: True, requestInterruption=Mock())
        controller.speech_worker = worker
        controller.set_manual_muted(True)
        assert worker.requestInterruption.called and '正在关闭' in floating.microphone_status()
        worker.isRunning = lambda: False
        floating._check_mute_release()
        assert floating.microphone_status() == '麦克风已完全禁用'
        assert not any(key == 'microphone_muted' for key, _ in writes)  # Overlay owns persistence.
        floating.set_enabled(True)
        floating._power._prepare_sleep(True)
        floating._power._prepare_sleep(False)
        assert controller.manual_muted and not controller.daily_active
    finally:
        controller.speech_worker = None


def test_silent_generation_and_response_window_block_idle_actions(voice, floating, folder):
    add_floating_animation(folder, idle_actions=True)
    controller, window = floating.controller, floating.window
    window.host.character_package = load_character_package(folder)
    controller.set_enabled(True)
    view = controller.view
    assert view._idle_timer.isActive()
    view._idle_timer.timeout.emit()
    assert view._reaction.startswith('idle:')
    window.chat_stream_controller.is_busy = lambda: True
    controller.refresh()
    assert view._state == 'thinking' and view._reaction is None and not view._idle_timer.isActive()
    view._idle_timer.timeout.emit()
    assert view._reaction is None
    window.chat_stream_controller.is_busy = lambda: False
    voice[0].wait_for_wake_reply()
    controller.refresh()
    assert not view._idle_timer.isActive()
    voice[0].end_daily_conversation(stop_reply=False)
    controller.refresh()
    assert view._idle_timer.isActive()
    voice[0].set_suspended(True)
    controller.refresh()
    assert not view._idle_timer.isActive()




def test_floating_gesture_uses_current_role_and_respects_mute(voice, floating):
    controller, calls = voice
    floating.controller.set_enabled(True)
    floating.controller.view.wake_requested.emit()
    assert calls.wake == ['Spica']
    controller.set_manual_muted(True)
    floating.controller.view.wake_requested.emit()
    assert calls.wake == ['Spica'] and '恢复采音' in floating.notices[-1]


def test_bubble_tracks_current_turn_and_closes_with_conversation(voice, floating):
    controller, _ = voice
    window, floating = floating.window, floating.controller
    bubble = floating.bubble
    floating.set_enabled(True)
    assert not bubble.isVisible()
    controller.wait_for_wake_reply()
    window.dialogue_visibility_controller.line_changed.emit('我在，怎么啦？')
    assert bubble.isVisible() and bubble._text == '我在，怎么啦？'
    assert bubble._speaker == 'Spica'
    old_position = bubble.pos()
    floating.view.move_visible(floating.view.pos() - QPoint(100, 0))
    assert bubble.pos() != old_position
    floating.set_enabled(False)
    assert not bubble.isVisible()
    floating.set_enabled(True)
    assert bubble.isVisible() and bubble._text == '我在，怎么啦？'
    window.chat_stream_controller.stream_session_id += 1
    floating.refresh()
    assert bubble._text == '…', 'A new turn must not resurrect the old caption.'
    window.dialogue_visibility_controller.line_changed.emit('第二轮回答')
    assert bubble._text == '第二轮回答'
    controller.end_daily_conversation(stop_reply=False)
    floating.refresh()
    assert not bubble.isVisible() and not window.dialogue_visibility_controller._floating_text
    controller.wait_for_wake_reply()
    floating.refresh()
    assert bubble._text == '…'
    controller.set_suspended(True)
    floating.refresh()
    assert not bubble.isVisible()


def test_bubble_renders_plain_text_and_stays_inside_screen(qapp):
    from ui.widgets.floating_bubble import FloatingDialogueBubble
    parent = QWidget()
    bubble = FloatingDialogueBubble(parent)
    try:
        text = '<b>这是普通文字</b>\n' + '较长的一句话，气泡跟随显示末尾。' * 20
        bubble.set_dialogue('Spica', text)
        assert bubble._document.toPlainText() == text
        assert bubble.height() <= 170 and bubble.width() <= 290
        available = qapp.primaryScreen().availableGeometry()
        for corner in (available.topLeft(), available.topRight(),
                       available.bottomLeft(), available.bottomRight()):
            bubble.place_near(QRect(corner, parent.size()))
            assert available.contains(bubble.geometry())
        assert bubble.windowFlags() & Qt.WindowType.WindowTransparentForInput
        bubble.show()
        qapp.processEvents()
        assert not bubble.grab().isNull()
    finally:
        parent.deleteLater()


def test_reply_reading_outlives_mic_but_not_role_or_sleep(voice, floating, monkeypatch):
    from ui.controllers import dialogue_visibility_controller as module
    now = [100.]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    mic, _ = voice
    window, controller = floating.window, floating.controller
    dialogue = window.dialogue_visibility_controller
    controller.set_enabled(True)
    mic.wait_for_wake_reply()
    dialogue.show_dialogue_line('最后一段')
    dialogue.retain_reply()
    mic.end_daily_conversation(stop_reply=False)
    now[0] = 109.
    controller.refresh()
    assert controller.bubble.isVisible() and not mic.conversation_active and not mic.capture_ready
    assert controller.bubble._text == '最后一段'
    assert dialogue._dialogue_line == '最后一段'
    now[0] = 116.
    controller.refresh()
    assert not controller.bubble.isVisible()
    mic.wait_for_wake_reply()
    dialogue.show_dialogue_line('旧角色回复')
    dialogue.retain_reply()
    window.host.character_package.revision = 'new'
    controller.refresh()
    assert controller.bubble._text != '旧角色回复'
    mic.set_suspended(True)
    controller.refresh()
    assert not controller.bubble.isVisible()


def test_late_dialogue_after_shutdown_cannot_touch_deleted_bubble(floating):
    controller = floating.controller
    controller.set_enabled(True)
    controller.shutdown()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    controller._subtitle('关闭后才到达的文字')
    controller._position_bubble()
    controller._refresh_bubble()
    controller.shutdown()
    assert not floating.window.dialogue_visibility_controller._floating_text


def test_task_notice_is_silent_deduplicated_and_yields_to_dialogue(voice, floating, monkeypatch):
    from spica.core.events import DesktopNoticeEvent
    from ui.controllers import dialogue_visibility_controller as module
    voice, calls = voice
    controller, window = floating.controller, floating.window
    now = [100.]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    controller.set_enabled(True)
    sid = voice.voice_session_id
    notice = DesktopNoticeEvent('task:one', 'Codex · Spica', '修复完成')
    controller.show_notice(notice)
    controller.show_notice(notice)
    assert len(window.dialogue_visibility_controller._notices) == 1
    assert controller.bubble.isVisible() and controller.bubble._text == '修复完成'
    assert controller.bubble._speaker == 'Codex · Spica'
    assert not controller.reply_text_visible
    assert voice.voice_session_id == sid and not voice.conversation_active
    assert not calls.wake and not calls.text
    voice.wait_for_wake_reply()
    window.dialogue_visibility_controller.line_changed.emit('我在，怎么啦？')
    assert controller.bubble._text == '我在，怎么啦？'
    assert controller.reply_text_visible
    now[0] += 30
    controller.refresh()
    assert len(window.dialogue_visibility_controller._notices) == 1, 'A conversation must not consume the notice display interval.'
    voice.end_daily_conversation(stop_reply=False)
    controller.refresh()
    assert controller.bubble._text == '修复完成'
    now[0] += 8
    controller.refresh()
    assert not controller.bubble.isVisible() and not window.dialogue_visibility_controller._notices
    controller.show_notice(notice)
    assert not window.dialogue_visibility_controller._notices, 'A repeated delivery ID cannot display again.'




def test_distinct_notifications_with_same_words_both_reveal_full_mode(floating, monkeypatch):
    from spica.core.events import DesktopNoticeEvent
    from ui.controllers import dialogue_visibility_controller as module
    now = [100.]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    controller, window = floating.controller, floating.window
    window.show()
    controller.show_notice(DesktopNoticeEvent('one', 'Codex', '任务完成'))
    controller.show_notice(DesktopNoticeEvent('two', 'Codex', '任务完成'))
    now[0] = 109.
    controller.refresh()
    shown = [call.args[0] for call in window.dialogue_visibility_controller.show_notice_message.call_args_list
             if call.args[0] is not None]
    assert shown == ['Codex：任务完成', 'Codex：任务完成']


def test_capture_cleanup_failure_survives_worker_exit_until_native_owner_is_gone(qapp, monkeypatch):
    from hardware.audio_input.keyword_capture import CaptureReleaseError
    from ui.workers.wake_word_worker import WakeWordWorker
    alive, interrupted = [True], [False]
    process = SimpleNamespace(is_alive=lambda: alive[0], close=Mock())
    failure = CaptureReleaseError(process)
    def broken_capture(*args, **kwargs):
        interrupted[0] = True
        raise failure
        yield b''
    monkeypatch.setattr('ui.workers.wake_word_worker.microphone_frames', broken_capture)
    worker = WakeWordWorker(detector=SimpleNamespace(create_stream=lambda _: object()),
                            words=('Spica',), mic_backend='generic')
    monkeypatch.setattr(worker, 'isInterruptionRequested', lambda: interrupted[0])
    failures = []
    worker.failed.connect(failures.append)
    worker.run()
    assert not worker.isRunning() and not worker.capture_released
    assert len(failures) == 1 and '尚未释放' in failures[0]
    alive[0] = False
    assert worker.capture_released and worker.capture_released
    process.close.assert_called_once_with()
    worker.deleteLater()
