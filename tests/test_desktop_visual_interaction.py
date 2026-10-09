"""Native widget contracts for the transparent galgame desktop frame."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest
from test_application_settings import _wait_for_controller

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QSize, Qt  # noqa: E402
from PySide6.QtGui import QInputMethodEvent, QMouseEvent, QPixmap, QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.overlay_config import load_dialogue_opacity, save_dialogue_opacity  # noqa: E402
from ui.qt_overlay import OverlayWindow  # noqa: E402
from ui.widgets.common import DEFAULT_DIALOGUE_OPACITY  # noqa: E402
from ui.widgets.dialogue_box import blue_veil  # noqa: E402
from ui.widgets.input_panel import InputPanel  # noqa: E402
from ui.widgets.settings_panel import SettingsPanel  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, isolated_runtime_config):
    with patch.object(OverlayWindow, "_init_backend", lambda self: None):
        overlay = OverlayWindow()
    overlay.resize(1000, 800)
    overlay.show()
    qapp.processEvents()
    yield overlay
    overlay.close()
    overlay.deleteLater()
    qapp.processEvents()


def test_opacity_persistence_preserves_other_preferences_and_corrupt_file(tmp_path):
    path = tmp_path / "overlay.json"
    path.write_text('{"default_ui_scale": 1.2, "custom": "keep"}', encoding="utf-8")
    assert load_dialogue_opacity(path) == DEFAULT_DIALOGUE_OPACITY
    assert save_dialogue_opacity(0.35, path)
    assert load_dialogue_opacity(path) == 0.35
    assert json.loads(path.read_text()) == {
        "default_ui_scale": 1.2, "custom": "keep", "dialogue_opacity": 0.35,
    }
    assert not save_dialogue_opacity(float("nan"), path)
    path.write_text('{"dialogue_opacity": true}', encoding="utf-8")
    assert load_dialogue_opacity(path) == DEFAULT_DIALOGUE_OPACITY
    path.write_text("broken", encoding="utf-8")
    assert not save_dialogue_opacity(0.5, path)
    assert path.read_text() == "broken"


def test_costume_controls_wait_for_the_whole_reply_before_changing(window, qapp):
    window.open_settings_panel()
    _wait_for_controller(qapp, window.application_settings_controller)
    writes = []
    window.visual_tool = SimpleNamespace(set_costume=lambda value: writes.append(value) or value)
    window.available_costumes = ["school", "summer"]
    window.selected_costume = "school"
    with patch.object(window, "_set_default_character_for_costume"), patch.object(window, "_is_conversation_busy", return_value=True):
        window.set_busy(True)
        assert not window.settings_panel.costume_box.isEnabled()
        window.set_costume("summer")
        assert writes == [] and window.selected_costume == "school"
    with patch.object(window, "_set_default_character_for_costume"), patch.object(window, "_is_conversation_busy", return_value=False):
        window.set_busy(False)
        assert window.settings_panel.costume_box.isEnabled()
        window.set_costume("summer")
    assert writes == ["summer"] and window.selected_costume == "summer"


def test_interlocutor_edit_does_not_change_the_running_conversation(window, qapp, tmp_path):
    from spica.config.manager import ConfigManager
    from spica.host.management import ManagementSurface
    window.host = SimpleNamespace(management_surface=ManagementSurface(
        registry=None, plugin_host=None, config_manager=ConfigManager(tmp_path / "app.yaml"), characters_root=tmp_path))
    names = []
    window.interlocutor_name = "kasa"
    window.agent = SimpleNamespace(
        set_interlocutor_name=lambda name: names.append(name) or name,
    )
    window.open_settings_panel()
    _wait_for_controller(qapp, window.application_settings_controller)
    field = window.settings_panel.name_input
    field.setFocus()
    qapp.processEvents()
    field.selectAll()
    QTest.keyClicks(field, "Ren")
    assert field.hasFocus()
    assert window.interlocutor_name == "kasa"

    assert names == []

    # Editing within a name must preserve spaces and the caret; an empty draft
    # must remain editable rather than being replaced with the default name.
    QTest.keyClicks(field, " San")
    assert field.text() == "Ren San"
    assert window.interlocutor_name == "kasa"
    field.selectAll()
    QTest.keyClick(field, Qt.Key.Key_Backspace)
    assert field.text() == ""
    QTest.keyClicks(field, "Haru")
    assert window.interlocutor_name == "kasa"
    window.host = None


def test_interlocutor_edit_is_saved_for_restart(window, qapp, tmp_path, monkeypatch):
    from spica.config.manager import ConfigManager
    from spica.host.management import ManagementSurface

    monkeypatch.setattr(ConfigManager, "_ensure_env_loaded", lambda self: None)
    monkeypatch.delenv("SPICA_USER_NAME", raising=False)
    manager = ConfigManager(tmp_path / "app.yaml")
    surface = ManagementSurface(
        registry=None, config_manager=manager, plugin_host=None,
        characters_root=tmp_path / "characters",
    )
    surface.write_config({"character": {"package_dir": "selected-role"}})
    window.host = SimpleNamespace(management_surface=surface)
    window.interlocutor_name = "kasa"
    window.open_settings_panel()
    _wait_for_controller(qapp, window.application_settings_controller)
    field = window.settings_panel.name_input
    field.setFocus()
    qapp.processEvents()
    field.selectAll()
    QTest.keyClicks(field, "Ren")
    QTest.keyClick(field, Qt.Key.Key_Return)
    restarted = ConfigManager(manager.config_path).load()
    assert restarted.character.interlocutor_name == "Ren"
    assert restarted.character.package_dir == "selected-role"
    assert window.interlocutor_name == "kasa"
    assert "重启" in window.settings_panel.interlocutor_name_status.text()

    field.selectAll()
    commit = QInputMethodEvent()
    commit.setCommitString("伞")
    QApplication.sendEvent(field, commit)
    window._close_settings_panel()
    window.settings_panel.motion_animation.setCurrentTime(
        window.settings_panel.motion_animation.duration()
    )
    qapp.processEvents()
    window.open_settings_panel()
    _wait_for_controller(qapp, window.application_settings_controller)
    assert field.text() == "伞"
    assert ConfigManager(manager.config_path).load().character.interlocutor_name == "伞"
    assert window.interlocutor_name == "kasa"
    window.host = None


@pytest.mark.parametrize("save_fails", [False, True])
def test_restart_saves_pending_name_and_keeps_window_open_on_save_failure(window, qapp, tmp_path, monkeypatch, save_fails):
    from spica.config.manager import ConfigManager
    from spica.host.management import ManagementSurface

    monkeypatch.setattr(ConfigManager, "_ensure_env_loaded", lambda self: None)
    monkeypatch.delenv("SPICA_USER_NAME", raising=False)
    manager = ConfigManager(tmp_path / "app.yaml")
    surface = ManagementSurface(registry=None, config_manager=manager, plugin_host=None,
                                characters_root=tmp_path / "characters")
    window.host = SimpleNamespace(management_surface=surface)
    window.open_settings_panel()
    _wait_for_controller(qapp, window.application_settings_controller)
    panel = window.settings_panel
    panel.name_input.setText("伞")
    if save_fails:
        monkeypatch.setattr(surface, "write_config", Mock(side_effect=OSError("read-only config")))
    QTest.mouseClick(panel.restart_button, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    assert window._restart_requested is not save_fails
    assert window.isVisible() is save_fails
    if save_fails:
        assert "保存失败" in panel.interlocutor_name_status.text()
        assert panel.restart_button.isEnabled()
    else:
        assert ConfigManager(manager.config_path).load().character.interlocutor_name == "伞"
        assert panel.restart_button.text() == "正在重启…"
    window.host = None


def test_restart_waits_for_package_operation(window, qapp):
    window.open_settings_panel()
    _wait_for_controller(qapp, window.application_settings_controller)
    controller = window.character_settings_controller
    controller.worker = object()
    window.settings_panel.set_character_busy(True)
    assert not window.settings_panel.restart_button.isEnabled()
    assert not window.settings_panel.name_input.isEnabled()
    with patch.object(window, "close") as close:
        window.settings_panel.restart_button.click()
        window.restart_application()
        close.assert_not_called()
    assert not window._restart_requested
    controller.worker = None
    window.settings_panel.set_character_busy(False)
    assert window.settings_panel.restart_button.isEnabled()
    assert window.settings_panel.name_input.isEnabled()


@pytest.mark.parametrize("owner", ["chat", "startup_warmup"])
def test_restart_button_finishes_after_a_busy_worker_exits(window, qapp, tmp_path, monkeypatch, owner):
    import threading
    import time
    from PySide6.QtCore import QThread
    from spica.config.manager import ConfigManager
    from spica.host.management import ManagementSurface

    release = threading.Event()

    class Worker(QThread):
        def run(self):
            release.wait(10)

    worker = Worker(window)
    worker.start()
    monkeypatch.setattr(ConfigManager, "_ensure_env_loaded", lambda self: None)
    monkeypatch.delenv("SPICA_USER_NAME", raising=False)
    window.host = SimpleNamespace(management_surface=ManagementSurface(
        registry=None, plugin_host=None, config_manager=ConfigManager(tmp_path / "app.yaml"),
        characters_root=tmp_path / "characters"))
    if owner == "chat":
        window.chat_stream_controller = SimpleNamespace(
            shutdown=lambda wait_ms: worker.wait(wait_ms), is_busy=worker.isRunning)
    else:
        window.startup_warmup_worker = worker
    try:
        window.open_settings_panel()
        _wait_for_controller(qapp, window.application_settings_controller)
        panel = window.settings_panel
        with patch("ui.qt_overlay._force_process_exit") as force_exit:
            QTest.mouseClick(panel.restart_button, Qt.MouseButton.LeftButton)
            assert window.isVisible()
            assert window._restart_requested, "busy shutdown discarded the restart request"
            assert not window._forced_close_armed
            assert "等待" in panel.restart_button.text()
            release.set()
            deadline = time.monotonic() + 2
            while window.isVisible() and time.monotonic() < deadline:
                QTest.qWait(20)
            assert not window.isVisible(), "restart must continue automatically after the worker exits"
            assert window._restart_requested
            force_exit.assert_not_called()
    finally:
        release.set()
        worker.wait(1000)
        window.chat_stream_controller = None
        window.startup_warmup_worker = None
        window.host = None
        window._forced_close_armed = False


def test_name_save_does_not_claim_success_when_environment_wins(window, qapp, tmp_path, monkeypatch):
    from spica.config.manager import ConfigManager
    from spica.host.management import ManagementSurface
    monkeypatch.setattr(ConfigManager, "_ensure_env_loaded", lambda self: None)
    monkeypatch.setenv("SPICA_USER_NAME", "OldAlias")
    manager = ConfigManager(tmp_path / "app.yaml")
    window.host = SimpleNamespace(management_surface=ManagementSurface(
        registry=None, plugin_host=None, config_manager=manager, characters_root=tmp_path / "characters"))
    window.open_settings_panel()
    _wait_for_controller(qapp, window.application_settings_controller)
    assert not window.character_settings_controller.save_interlocutor_name("NewAlias")
    assert "SPICA_USER_NAME" in window.settings_panel.interlocutor_name_status.text()
    assert not manager.config_path.exists()
    window.host = None


def test_restart_does_not_relaunch_or_force_exit_while_shutdown_is_pending(window):
    window.chat_stream_controller = SimpleNamespace(shutdown=lambda _wait_ms: False)
    try:
        with patch("ui.qt_overlay._force_process_exit") as force_exit:
            window.restart_application()
            assert window._restart_requested
            assert window.isVisible() and not window._forced_close_armed
            assert window._restart_timer.isActive()
            window.restart_application()
            QTest.qWait(300)
            assert window.isVisible() and window._restart_requested
            force_exit.assert_not_called()
    finally:
        window.chat_stream_controller = None
        window._restart_timer.stop()
        window._forced_close_armed = False


def test_pending_restart_rejects_a_queued_system_turn(window):
    start = Mock(return_value=None)
    window.chat_stream_controller = SimpleNamespace(start_system_turn=start)
    window._restart_requested = True
    try:
        window._start_system_turn_gui(object())
        start.assert_not_called()
    finally:
        window.chat_stream_controller = None
        window._restart_requested = False


@pytest.fixture
def desktop_main(monkeypatch):
    from ui import qt_overlay

    app = Mock()
    app.exec.return_value = 0
    overlay = SimpleNamespace(_restart_requested=False, floating_controller=SimpleNamespace(show_initial=lambda: None))
    startup = SimpleNamespace(restart_environment=lambda: {"PATH": "original-path"})
    monkeypatch.setattr(qt_overlay, "load_secrets", lambda **kwargs: startup)
    monkeypatch.setattr(qt_overlay, "QApplication", lambda _argv: app)
    monkeypatch.setattr(qt_overlay, "OverlayWindow", lambda **kwargs: overlay)
    change_dir, execute, error = Mock(), Mock(), Mock()
    monkeypatch.setattr(qt_overlay.os, "chdir", change_dir)
    monkeypatch.setattr(qt_overlay.os, "execve", execute)
    monkeypatch.setattr(qt_overlay.QMessageBox, "critical", error)
    return SimpleNamespace(module=qt_overlay, app=app, window=overlay, change_dir=change_dir, execute=execute, error=error)


@pytest.mark.parametrize("frozen,original,current,expected", [
    (False, ["python", "-u", "/project with spaces/webui_qt.py"], ["webui_qt.py"], ["-u", "/project with spaces/webui_qt.py"]),
    (False, ["python", "-m", "ui.qt_overlay"], ["ui/qt_overlay.py"], ["-m", "ui.qt_overlay"]),
    (False, ["python", "-X", "utf8", "桌宠 程序/webui_qt.py"], ["桌宠 程序/webui_qt.py"], ["-X", "utf8", "桌宠 程序/webui_qt.py"]),
    (True, ["Spica.exe"], ["Spica.exe", "-style", "Fusion"], ["-style", "Fusion"]),
])
def test_restart_reexecutes_same_launch_only_after_event_loop_stops(desktop_main, monkeypatch, tmp_path, frozen, original, current, expected):
    from pathlib import Path
    import sys

    monkeypatch.setattr(sys, "orig_argv", original)
    monkeypatch.setattr(sys, "argv", current.copy())
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(Path, "cwd", classmethod(lambda _cls: tmp_path))
    desktop_main.window._restart_requested = True

    def stop_loop():
        desktop_main.execute.assert_not_called()
        sys.argv[:] = ["Qt consumed the arguments"]
        return 0

    desktop_main.app.exec.side_effect = stop_loop
    assert desktop_main.module.main() == 0
    expected_directories = [] if frozen else [call(Path(desktop_main.module.__file__).resolve().parents[1])]
    assert desktop_main.change_dir.call_args_list == [*expected_directories, call(tmp_path)]
    desktop_main.execute.assert_called_once_with(sys.executable, [sys.executable, *expected], {"PATH": "original-path"})
    desktop_main.error.assert_not_called()


@pytest.mark.parametrize("requested,exit_code", [(False, 0), (True, 1)])
def test_normal_exit_and_failed_event_loop_do_not_restart(desktop_main, requested, exit_code):
    desktop_main.window._restart_requested = requested
    desktop_main.app.exec.return_value = exit_code
    assert desktop_main.module.main() == exit_code
    desktop_main.execute.assert_not_called()


def test_relaunch_failure_reports_manual_start_without_retry_loop(desktop_main):
    desktop_main.window._restart_requested = True
    desktop_main.execute.side_effect = OSError("executable unavailable")
    assert desktop_main.module.main() == 1
    desktop_main.execute.assert_called_once()
    desktop_main.error.assert_called_once()
    assert "手动启动" in desktop_main.error.call_args.args[2]


def test_stop_does_not_shift_send_or_input(qapp):
    panel = InputPanel()
    panel.resize(800, 60)
    panel.show()
    qapp.processEvents()
    before = (panel.input.geometry(), panel.send_button.geometry())
    panel.set_turn_active(True)
    qapp.processEvents()
    assert not panel.stop_button.isHidden()
    assert (panel.input.geometry(), panel.send_button.geometry()) == before
    panel.set_turn_active(False)
    qapp.processEvents()
    assert (panel.input.geometry(), panel.send_button.geometry()) == before
    panel.close()


def test_ime_candidate_enter_never_sends_an_unfinished_message(qapp):
    panel = InputPanel()
    submitted = []
    panel.send_requested.connect(lambda: submitted.append(panel.input.text()))
    panel.input.setText("已经输入的文字")
    QApplication.sendEvent(panel.input, QInputMethodEvent("hou", []))
    QTest.keyClick(panel.input, Qt.Key.Key_Return)
    assert submitted == []
    commit = QInputMethodEvent()
    commit.setCommitString("候")
    QApplication.sendEvent(panel.input, commit)
    assert submitted == []
    QTest.keyClick(panel.input, Qt.Key.Key_Return)
    assert submitted == ["已经输入的文字候"]
    panel.close()


def test_settings_are_readable_and_wheel_does_not_change_values(qapp):
    panel = SettingsPanel()
    panel.resize(380, 350)
    panel.set_voice_volume(0.7)
    panel.show()
    qapp.processEvents()
    image = panel.grab().toImage()
    assert image.pixelColor(10, 10).alpha() >= 230
    values = []
    panel.opacity_changed.connect(values.append)
    panel.set_opacity(0.4)
    assert panel.opacity_slider.value() == 60
    assert values == []
    panel.opacity_slider.setValue(70)
    assert values == pytest.approx([0.3])
    wheel = QWheelEvent(
        QPointF(10, 10), QPointF(10, 10), QPoint(), QPoint(0, -120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    QApplication.sendEvent(panel.voice_volume_slider, wheel)
    assert panel.voice_volume_slider.value() == 70
    assert panel.scroll_area.verticalScrollBar().value() > 0
    panel.close()


@pytest.mark.parametrize("size", [(1000, 800), (460, 360)])
def test_frame_settings_and_character_share_safe_geometry(window, qapp, size):
    window.resize(*size)
    qapp.processEvents()
    frame, footer = window.dialogue.geometry(), window.input_panel.geometry()
    assert frame.x() == footer.x() and frame.width() == footer.width()
    assert frame.bottom() + 1 == footer.top()
    assert window.input_panel.input.height() >= window.input_panel.input.minimumSizeHint().height()
    assert window.character_label.geometry().bottom() == footer.bottom()
    assert abs(window.character_label.geometry().center().x() - frame.center().x()) <= 1
    assert not window.mask().contains(QPoint(window.width() // 2, footer.bottom() + 2))
    character_before = window.character_label.geometry()
    window.open_settings_panel()
    _wait_for_controller(qapp, window.application_settings_controller)
    window.settings_panel.stop_motion_for_layout()
    qapp.processEvents()
    assert window.settings_panel.geometry().bottom() < footer.top()
    assert window.character_label.geometry() == character_before
    assert window.rect().contains(window.settings_panel.geometry())


def test_dialogue_scale_is_independent_of_character_size(window, qapp):
    window.resize(1336, 970)
    window.set_overall_scale(1.0)
    qapp.processEvents()
    character_size = window.character_label.size()
    dialogue_height = window.dialogue.height()
    window.set_overall_scale(1.3)
    qapp.processEvents()
    assert window.character_label.size() == character_size
    assert window.dialogue.height() > dialogue_height


def test_desktop_reading_area_fits_three_lines_and_stays_aligned(window, qapp):
    window.resize(1336, 970)
    window.set_overall_scale(1.2)
    qapp.processEvents()
    dialogue = window.dialogue
    text = dialogue.text_label
    assert text.height() >= text.fontMetrics().lineSpacing() * 3
    assert text.mapTo(window, QPoint()).x() == dialogue.speaker_label.mapTo(window, QPoint()).x()
    # QLineEdit paints its glyphs 3px inside the transparent border; the
    # approved layout aligns those glyphs, rather than the editor's outer box.
    assert text.mapTo(window, QPoint()).x() == window.input_panel.input.mapTo(window, QPoint(3, 0)).x()
    assert window.mask().contains(text.mapTo(window, QPoint(1, text.height() - 1)))
    assert not text.geometry().translated(dialogue.pos()).intersects(window.window_controls.geometry())
    width = text.width()
    dialogue.set_dialogue_text("おかえり。")
    qapp.processEvents()
    assert text.width() == width
    dialogue.set_dialogue_text("今日も、お疲れさまでした。\n少しだけ、ここで一緒に休みませんか？\nあなたの話を、聞かせてください。")
    qapp.processEvents()
    assert text.width() == width
    assert abs(window.character_label.geometry().center().x() - dialogue.geometry().center().x()) <= 1


def test_editors_and_buttons_do_not_drag_and_small_motion_is_ignored(window):
    assert not window.mask().contains(QPoint(window.width() // 2, 2))
    original = window.pos()
    for widget in (window.input_panel.input, window.window_controls.settings_button):
        QTest.mousePress(widget, Qt.MouseButton.LeftButton)
        assert window._drag_press_pos is None
        QTest.mouseRelease(widget, Qt.MouseButton.LeftButton)
    window._close_settings_panel()
    window.settings_panel.stop_motion_for_layout()
    QTest.mousePress(window.dialogue.speaker_label, Qt.MouseButton.LeftButton)
    assert window._drag_press_pos is not None
    tiny_move = QMouseEvent(
        QEvent.Type.MouseMove, QPointF(2, 2), QPointF(window._drag_press_pos + QPoint(1, 1)),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(window.dialogue.speaker_label, tiny_move)
    assert window.pos() == original
    QTest.mouseRelease(window.dialogue.speaker_label, Qt.MouseButton.LeftButton)
    assert window._drag_press_pos is None


@pytest.mark.parametrize("target", ["character", "name", "frame"])
def test_intuitive_drag_surfaces_move_the_window(window, qapp, target, monkeypatch):
    window.resize(600, 500)
    window.move(50, 50)
    qapp.processEvents()
    # This checks unconstrained dragging; don't accidentally hit the real
    # monitor boundary on a high-DPI headless/Windows test desktop.
    monkeypatch.setattr("ui.qt_overlay.QGuiApplication.screenAt", lambda _point: SimpleNamespace(
        availableGeometry=lambda: QRect(0, 0, 1920, 1080),
    ))
    widget = {
        "character": window.character_label,
        "name": window.dialogue.speaker_label,
        "frame": window.dialogue,
    }[target]
    before = window.pos()
    QTest.mousePress(widget, Qt.MouseButton.LeftButton)
    start = window._drag_press_pos
    assert start is not None
    move = QMouseEvent(
        QEvent.Type.MouseMove, QPointF(60, 25), QPointF(start + QPoint(60, 25)),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, move)
    assert window.pos() == before + QPoint(60, 25)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton)


@pytest.mark.parametrize("action", ["import_folder", "import_style_folder", "export_folder"])
def test_package_picker_keeps_animation_running_and_cancel_keeps_selection(window, qapp, tmp_path, monkeypatch, action):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QFileDialog
    from spica.config.manager import ConfigManager
    from spica.host.management import ManagementSurface

    surface = ManagementSurface(
        registry=None, plugin_host=None, config_manager=ConfigManager(tmp_path / "app.yaml"), characters_root=tmp_path,
    )
    window.host = SimpleNamespace(
        management_surface=surface, character_package=SimpleNamespace(manifest=True, character_id="test"),
    )
    window.open_settings_panel()
    _wait_for_controller(qapp, window.application_settings_controller)
    controller = window.character_settings_controller
    before = surface.read_config()
    window.dialogue.set_dialogue_text("选择文件夹时，动画继续。")
    window.dialogue.show_tail()
    ticks = []
    window.dialogue.tail.timer.timeout.connect(lambda: ticks.append(True))
    method = "getSaveFileName" if action == "export_folder" else "getExistingDirectory"
    original = getattr(QFileDialog, method)

    def choose(*args, **kwargs):
        options = kwargs.get("options", args[3] if len(args) > 3 else QFileDialog.Option(0))
        # Fail before entering a Windows native modal loop: its timers would
        # also prevent this test's automatic cancel from firing.
        assert options & QFileDialog.Option.DontUseNativeDialog
        args = [*args]
        args[2] = str(tmp_path)
        return original(*args, **kwargs)

    monkeypatch.setattr(QFileDialog, method, choose)
    closer = QTimer(window)
    closer.setInterval(160)
    closer.timeout.connect(lambda: qapp.activeModalWidget().reject() if isinstance(qapp.activeModalWidget(), QFileDialog) else None)
    closer.start()
    try:
        if action == "export_folder":
            controller.export_folder(False)
        else:
            getattr(controller, action)()
        assert ticks, "the file picker suspended the dialogue animation"
        assert controller.worker is None
        assert surface.read_config() == before
    finally:
        closer.stop()
        window.host = None


def test_reply_focus_and_settings_close_preserve_draft(window, qapp, tmp_path):
    from spica.config.manager import ConfigManager
    from spica.host.management import ManagementSurface
    window.host = SimpleNamespace(management_surface=ManagementSurface(
        registry=None, plugin_host=None, config_manager=ConfigManager(tmp_path / "app.yaml"), characters_root=tmp_path))
    window.input_panel.input.setText("还没写完的消息")
    window.open_settings_panel()
    _wait_for_controller(qapp, window.application_settings_controller)
    panel = window.settings_panel
    panel.stop_motion_for_layout()
    window.activateWindow()
    panel.name_input.setFocus()
    qapp.processEvents()
    window._focus_input()
    assert QApplication.focusWidget() is panel.name_input
    assert window.input_panel.input.text() == "还没写完的消息"
    QTest.keyClick(panel.name_input, Qt.Key.Key_Escape)
    panel.stop_motion_for_layout()
    assert panel.isHidden()
    assert window.isVisible()
    assert window.input_panel.input.text() == "还没写完的消息"
    window.open_settings_panel()
    _wait_for_controller(qapp, window.application_settings_controller)
    panel.stop_motion_for_layout()
    window.input_panel.input.setFocus()
    QTest.keyClick(window.input_panel.input, Qt.Key.Key_Escape)
    panel.stop_motion_for_layout()
    assert panel.isHidden()
    with patch.object(window, "isActiveWindow", return_value=False), patch.object(window.input_panel.input, "setFocus") as focus:
        window._focus_input()
    focus.assert_not_called()
    window.host = None


def test_opacity_preview_reuses_surface_and_commits_at_end(window):
    with patch("ui.qt_overlay.save_dialogue_opacity") as save:
        window.set_dialogue_opacity(0.35)
        window.set_dialogue_opacity(0.3)
        assert window._opacity_save_timer.isActive()
        save.assert_not_called()
        window.dialogue.grab()
        cached_surface = window.dialogue._surface.cacheKey()
        window.dialogue.set_dialogue_text("新的一句话")
        window.dialogue.grab()
        assert window.dialogue._surface.cacheKey() == cached_surface
        assert window.input_panel._opacity == 0.3
        window.persist_dialogue_opacity()
        save.assert_called_once_with(0.3)
        assert not window._opacity_save_timer.isActive()


def test_opacity_change_keeps_newly_visible_edges_inside_window_mask(window):
    window.set_dialogue_opacity(0.2)
    window.set_dialogue_opacity(1.0)
    for widget in (window.dialogue, window.input_panel):
        visible = widget.hit_region().translated(widget.pos())
        assert visible.subtracted(window.mask()).isEmpty()


def test_reading_band_is_denser_than_its_edges_and_obeys_opacity(qapp):
    surface = blue_veil(QSize(600, 140), 2.0, DEFAULT_DIALOGUE_OPACITY).toImage()
    center = surface.pixelColor(600, 140).alpha()
    assert center > surface.pixelColor(0, 140).alpha()
    assert center > surface.pixelColor(600, 0).alpha()
    stronger = blue_veil(QSize(600, 140), 2.0, 1.0).toImage()
    assert stronger.pixelColor(600, 140).alpha() > center


def test_async_status_never_covers_open_settings(window, qapp):
    settings_button = window.window_controls.settings_button
    assert window.childAt(settings_button.mapTo(window, settings_button.rect().center())) is settings_button
    window.open_settings_panel()
    _wait_for_controller(qapp, window.application_settings_controller)
    panel = window.settings_panel
    panel.stop_motion_for_layout()
    overlap = panel.geometry().intersected(window.dialogue.geometry()).center()
    for update in (
        lambda: window.dialogue_visibility_controller.show_system_message("状态已更新"),
    ):
        update()
        qapp.processEvents()
        topmost = window.childAt(overlap)
        assert topmost is panel or panel.isAncestorOf(topmost)


def test_sentence_tail_obeys_completion_stop_and_window_visibility(window, qapp):
    writer, tail = window.typewriter_controller, window.dialogue.tail
    writer.set_speed(3)
    writer.start("好。")
    assert not tail.timer.isActive()
    QTest.qWait(180)
    assert not writer.is_active()
    assert tail.timer.isActive()
    before = tail._frame_index
    QTest.qWait(90)
    assert tail._frame_index != before
    window.hide()
    assert not tail.timer.isActive()
    window.show()
    qapp.processEvents()
    assert tail.timer.isActive()
    window._on_stop_requested()
    assert not tail.timer.isActive()
    writer.start("还没有说完的话")
    writer.stop()
    assert not tail.timer.isActive()


def test_long_dialogue_scrolls_without_moving_input_or_hiding_last_line(window, qapp):
    footer = window.input_panel.geometry()
    text = window.dialogue.text_label
    window.dialogue.set_dialogue_text("这一段需要上下查看。\n" * 20 + "最后一句。")
    window.dialogue.show_tail()
    qapp.processEvents()
    assert not window.dialogue.tail.timer.isActive()
    wheel = QWheelEvent(
        QPointF(20, 20), QPointF(20, 20), QPoint(), QPoint(0, -12000),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    QApplication.sendEvent(text, wheel)
    assert window.input_panel.geometry() == footer
    assert window.dialogue.tail.isVisible()
    assert text.rect().contains(window.dialogue.tail.geometry())
    assert window.dialogue.tail.timer.isActive()


def test_dialogue_keeps_the_lower_character_visible(window):
    pixmap = QPixmap(200, 400)
    pixmap.fill(Qt.GlobalColor.white)
    window.current_pixmap = pixmap
    window.current_pixmap_cache_key = "opaque-character"
    window._rescale_character()
    rendered = window.character_label.pixmap().toImage()
    assert not window.dialogue.isHidden()
    assert rendered.pixelColor(rendered.width() // 2, rendered.height() - 1).alpha() == 255


def test_high_dpi_character_hit_mask_keeps_the_entire_visible_silhouette(window):
    pixmap = QPixmap(200, 200)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.white)
    window.character_label.setGeometry(100, 100, 100, 100)
    window.character_label.setPixmap(pixmap)
    rect = window._character_pixmap_rect(pixmap)
    assert rect.size() == QSize(100, 100)
    region = window._character_hit_region()
    assert region.contains(rect.topLeft())
    assert region.contains(rect.bottomRight())


def test_typing_more_text_keeps_existing_wrapped_lines_in_place(window, qapp):
    window.resize(824, 720)
    window.set_overall_scale(1.0)
    qapp.processEvents()
    text = window.dialogue.text_label
    window.dialogue.set_dialogue_text("啊" * 70)
    before = text._text_layout.lineAt(1).textLength()
    window.dialogue.set_dialogue_text("啊" * 85)
    assert text._text_layout.lineAt(1).textLength() == before
