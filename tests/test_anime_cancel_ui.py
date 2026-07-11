"""Qt-only seams for the anime download stop control and event dispatch."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.qt_overlay import OverlayWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _window():
    with patch.object(OverlayWindow, "_init_backend", lambda self: None):
        return OverlayWindow()


class _FakeAnimeController:
    def __init__(self):
        self.button_calls = 0
        self.current_request_id = "A"
        self.requests = []
        self.cancels = []
        self.expected_cancels = []

    def in_flight_state(self):
        return {"request_id": self.current_request_id}

    def cancel_current_download(self, expected_request_id):
        self.button_calls += 1
        self.expected_cancels.append(expected_request_id)
        return True

    def handle_anime_request_event(self, event):
        self.requests.append(event)

    def handle_anime_cancel_event(self, event):
        self.cancels.append(event)

    def shutdown(self, _wait_ms):
        pass


def test_anime_cancel_button_state_and_click_share_controller_entry(qapp):
    window = _window()
    controller = _FakeAnimeController()
    window.anime_controller = controller

    assert window.anime_cancel_button.objectName() == "animeCancelButton"
    assert window.anime_cancel_button.isHidden()

    window._set_anime_cancel_state(True, False)
    assert not window.anime_cancel_button.isHidden()
    assert window.anime_cancel_button.isEnabled()
    assert window.anime_cancel_button.text() == "停止下载"

    window.anime_cancel_button.click()
    assert controller.button_calls == 1
    assert controller.expected_cancels == ["A"]

    window._set_anime_cancel_state(True, True)
    assert not window.anime_cancel_button.isHidden()
    assert not window.anime_cancel_button.isEnabled()
    assert window.anime_cancel_button.text() == "停止中…"
    window.anime_cancel_button.click()
    assert controller.button_calls == 1

    window._set_anime_cancel_state(False, False)
    assert window.anime_cancel_button.isHidden()
    window.close()


def test_anime_cancel_button_click_keeps_request_seen_at_press(qapp):
    window = _window()
    controller = _FakeAnimeController()
    window.anime_controller = controller
    window._set_anime_cancel_state(True, False)

    # Mouse-down happened while A was visible. Before mouse-up/click reaches
    # the GUI, queued ready + next-request handling advances the controller to B.
    window.anime_cancel_button.pressed.emit()
    controller.current_request_id = "B"
    window._set_anime_cancel_state(True, False)
    window.anime_cancel_button.clicked.emit()

    assert controller.expected_cancels == ["A"]
    window.close()


def test_anime_runtime_bridge_dispatches_request_and_cancel(qapp):
    window = _window()
    controller = _FakeAnimeController()
    window.anime_controller = controller
    request = SimpleNamespace(kind="anime_request", request_id="REQ")
    cancel = SimpleNamespace(kind="anime_cancel_request", request_id="REQ")

    window._on_anime_runtime_event(request)
    window._on_anime_runtime_event(cancel)

    assert controller.requests == [request]
    assert controller.cancels == [cancel]
    window.close()


def test_anime_cancel_button_is_laid_out_and_in_click_mask(qapp):
    window = _window()
    window.resize(800, 600)
    window._set_anime_status("⬇ 下载中 20%：测试番")
    window._set_anime_cancel_state(True, False)
    window._layout_overlay()

    geometry = window.anime_cancel_button.geometry()
    assert not geometry.isEmpty()
    assert geometry.right() < window.width()
    assert window.mask().contains(geometry.center())
    window.close()
