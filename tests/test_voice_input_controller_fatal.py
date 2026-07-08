"""W3 voice-loop wiring: mic_backend string injection reaches SpeechWorker, and
``handle_error`` STOPS the voice loop on fatal speech errors (P2-3) while
non-fatal errors keep the loop alive (retry unit = next recording)."""

import pytest
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication

from ui.controllers.voice_input_controller import VoiceInputController


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _Harness:
    def __init__(self, qapp):
        self.voice_active_calls = []
        self.busy_calls = []
        self.dialogue = []
        self.parent = QObject()  # kept alive: a temp parent would GC the C++ side
        self.controller = VoiceInputController(
            self.parent,
            set_voice_active=self.voice_active_calls.append,
            set_busy=self.busy_calls.append,
            is_conversation_busy=lambda: False,
            set_dialogue_text=self.dialogue.append,
            on_recognized_text=lambda text: None,
            backend_ready=lambda: True,
        )


def test_mic_backend_reaches_speech_worker(qapp, monkeypatch):
    # start() is a no-op here: this pins the INJECTION CHAIN, not the recording.
    import ui.controllers.voice_input_controller as vic

    monkeypatch.setattr(vic.SpeechWorker, "start", lambda self: None)
    harness = _Harness(qapp)
    harness.controller.set_mic_backend("generic")
    harness.controller._start_speech_worker()
    assert harness.controller.speech_worker._mic_backend == "generic"


def test_default_mic_backend_is_respeaker(qapp, monkeypatch):
    import ui.controllers.voice_input_controller as vic

    monkeypatch.setattr(vic.SpeechWorker, "start", lambda self: None)
    harness = _Harness(qapp)
    harness.controller._start_speech_worker()
    assert harness.controller.speech_worker._mic_backend == "respeaker"


def test_fatal_error_stops_voice_loop(qapp):
    harness = _Harness(qapp)
    controller = harness.controller
    controller.voice_mode_active = True
    session = controller.voice_session_id

    controller.handle_error("语音识别失败：无法打开麦克风（默认输入设备）：boom", session)

    assert controller.voice_mode_active is False  # loop STOPPED
    assert controller.voice_session_id == session + 1  # stale workers invalidated
    assert harness.voice_active_calls[-1] is False
    assert harness.busy_calls[-1] is False
    assert harness.dialogue  # the cause reached the dialogue


def test_non_fatal_error_keeps_loop_alive(qapp):
    harness = _Harness(qapp)
    controller = harness.controller
    controller.voice_mode_active = True
    session = controller.voice_session_id

    controller.handle_error("语音识别失败：麦克风读取异常：transient", session)

    assert controller.voice_mode_active is True  # loop survives; next take retries
    assert controller.voice_session_id == session


if __name__ == "__main__":
    import unittest

    unittest.main()
