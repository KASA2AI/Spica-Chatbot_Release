import os
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PySide6 = pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.controllers.chat_stream_controller import ChatStreamController  # noqa: E402
from ui.qt_overlay import OverlayWindow  # noqa: E402
from ui.widgets.input_panel import InputPanel  # noqa: E402
from ui.widgets.screenshot_selector import ScreenshotSelectionOverlay  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


def test_input_panel_has_screenshot_button_and_signal(qapp):
    panel = InputPanel()
    assert hasattr(panel, "screenshot_requested")
    assert panel.screenshot_button.objectName() == "screenshotButton"
    assert panel.screenshot_button.isCheckable()
    assert panel.screenshot_button.toolTip() == "截图并随下一条消息发送给 Spica 查看"

    emitted = []
    panel.screenshot_requested.connect(lambda: emitted.append(True))
    panel.screenshot_button.click()
    assert emitted == [True]

    panel.set_screenshot_pending(True)
    assert panel.screenshot_button.isChecked()
    panel.set_busy(True)
    assert not panel.screenshot_button.isEnabled()
    panel.set_busy(False)
    assert panel.screenshot_button.isEnabled()


def test_overlay_sets_and_cancels_pending_screenshot(qapp):
    attachment = {"kind": "screen_capture", "target": "selected_region", "mode": "region", "image_bytes": b"png"}
    with patch.object(OverlayWindow, "_init_backend", lambda self: None):
        window = OverlayWindow()
    window._handle_screenshot_worker_done(attachment)

    assert window.pending_screen_attachment is attachment
    assert window.input_panel.screenshot_button.isChecked()

    window.toggle_screenshot_selection()
    assert window.pending_screen_attachment is None
    assert not window.input_panel.screenshot_button.isChecked()
    window.close()


def test_screenshot_selector_small_rect_cancel_reason(qapp):
    selector = ScreenshotSelectionOverlay()
    reasons = []
    selector.selection_cancelled.connect(reasons.append)
    selector._origin = selector.rect().topLeft()
    selector._current = selector._origin
    selector._finish_selection()
    assert reasons == ["截图区域太小"]


def test_chat_stream_status_inspecting_screen_text(qapp):
    class FakeTypewriter:
        def __init__(self):
            self.calls = []

        def start(self, text, interval_ms=0, **kwargs):
            self.calls.append({"text": text, "interval_ms": interval_ms, **kwargs})

    controller = ChatStreamController.__new__(ChatStreamController)
    controller.playback_active = False
    controller.typewriter_controller = FakeTypewriter()

    ChatStreamController._handle_stream_status(
        controller,
        {"state": "tools", "message": "inspecting_screen"},
    )

    assert controller.typewriter_controller.calls[-1]["text"] == "正在查看屏幕..."


def test_main_primes_env_before_any_construction():
    """F19 (CLAUDE.md #10): main() must prime the environment (load_secrets ->
    dotenv) BEFORE constructing anything. SongController's intent classifier is
    built inside OverlayWindow.__init__ and reads env at construction -- when
    priming happened later (inside AppHost.initialize), it read an un-primed
    environment and stayed disabled forever."""
    import inspect

    import ui.qt_overlay as qt_overlay_module

    source = inspect.getsource(qt_overlay_module.main)
    prime = source.index("load_secrets()")
    qapp_pos = source.index("QApplication(")
    window_pos = source.index("OverlayWindow(")
    assert prime < qapp_pos < window_pos


def test_chat_stream_status_watch_game_screen_text(qapp):
    class FakeTypewriter:
        def __init__(self):
            self.calls = []

        def start(self, text, interval_ms=0, **kwargs):
            self.calls.append({"text": text, "interval_ms": interval_ms, **kwargs})

    controller = ChatStreamController.__new__(ChatStreamController)
    controller.playback_active = False
    controller.typewriter_controller = FakeTypewriter()

    ChatStreamController._handle_stream_status(
        controller,
        {"state": "tools", "message": "tool:watch_game_screen"},
    )
    # review #9 (修C): the copy used to read 「尸检屏幕」(autopsy) -- fixed.
    assert controller.typewriter_controller.calls[-1]["text"] == "Spica正在查看屏幕..."

    # Other tools fall back to the minimal "..." status (shortened from "正在处理工具...").
    ChatStreamController._handle_stream_status(
        controller,
        {"state": "tools", "message": "tool:other_tool"},
    )
    assert controller.typewriter_controller.calls[-1]["text"] == "..."
