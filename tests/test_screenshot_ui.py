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
    attachment = {"kind": "screen_capture", "target": "selected_region", "image_bytes": b"jpeg"}
    with patch.object(OverlayWindow, "_init_backend", lambda self: None):
        window = OverlayWindow()
    with patch.object(window, "_build_selected_region_attachment", return_value=attachment):
        window._capture_selected_region({})

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
