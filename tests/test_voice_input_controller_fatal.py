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


def test_default_mic_backend_is_generic(qapp, monkeypatch):
    import ui.controllers.voice_input_controller as vic

    monkeypatch.setattr(vic.SpeechWorker, "start", lambda self: None)
    harness = _Harness(qapp)
    harness.controller._start_speech_worker()
    assert harness.controller.speech_worker._mic_backend == "generic"


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


def test_capture_release_failure_keeps_native_owners_and_blocks_stale_retry(qapp, monkeypatch):
    from types import SimpleNamespace
    from hardware.respeaker.audio import release_audio_capture
    from ui.workers.speech_worker import SpeechWorker

    class Broken:
        def stop_stream(self):
            raise OSError("stop failed")
        def close(self):
            raise OSError("close failed")
        def terminate(self):
            raise OSError("terminate failed")

    native = Broken()
    harness = _Harness(qapp)
    controller = harness.controller
    worker = SpeechWorker(stt_port=SimpleNamespace())
    controller.speech_worker = worker
    controller.voice_mode_active = True
    monkeypatch.setattr(worker, "_record", lambda **kwargs: release_audio_capture(native, native))
    # A stop happened while capture was unwinding. Late cleanup failures still
    # own resources even when ordinary recognized/error callbacks are stale.
    old_session = controller.voice_session_id
    controller.voice_session_id += 1
    worker.failed.connect(lambda text: controller.handle_error(text, old_session))
    worker.run()
    controller.handle_finished(old_session)
    assert controller._capture_release_error.stream is native
    assert controller._capture_release_error.audio is native
    assert not controller.voice_mode_active
    monkeypatch.setattr(controller, "_start_speech_worker", lambda: pytest.fail("must not reopen capture"))
    controller.start()
    controller.maybe_start_recording()
    assert "采音资源尚未释放" in harness.dialogue[-1]


def test_shutdown_retains_capture_failure_that_arrives_while_waiting(qapp, monkeypatch):
    import threading
    from types import SimpleNamespace
    from hardware.respeaker.audio import ReSpeakerCaptureReleaseError
    from ui.workers.speech_worker import SpeechWorker
    entered = threading.Event()
    native = object()
    def record(worker, **kwargs):
        entered.set()
        while not kwargs["should_stop"]():
            threading.Event().wait(.001)
        raise ReSpeakerCaptureReleaseError(native, native)
    monkeypatch.setattr(SpeechWorker, "_record", record)
    harness = _Harness(qapp)
    controller = harness.controller
    controller.set_stt_port(SimpleNamespace())
    controller.start()
    try:
        assert entered.wait(2)
        assert not controller.shutdown(2000)  # A native release failure is not a clean shutdown.
        qapp.processEvents()
        assert controller._capture_release_error.stream is native
        assert controller._capture_release_error.audio is native
        monkeypatch.setattr(controller, "_start_speech_worker", lambda: pytest.fail("must not reopen"))
        controller.start()
    finally:
        if controller.speech_worker is not None:
            controller.speech_worker.requestInterruption()
            controller.speech_worker.wait(2000)


if __name__ == "__main__":
    import unittest

    unittest.main()
