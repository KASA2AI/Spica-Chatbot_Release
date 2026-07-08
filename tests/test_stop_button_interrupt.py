"""A (send-to-interrupt) + B (cross-mode stop button) acceptance pins.

B is a UI affordance that lets the user stop Spica mid-speech in EITHER input mode
(no voice barge-in): a stop button visible exactly while a chat/reaction turn is in
flight, whose click rides the same stop_current as a new turn -- so #1's
worker.cancel halts the backend producer cleanly (that backend chain is pinned in
test_cancellation.py; here we pin the UI -> stop_current bridge + cross-mode
visibility). A relaxes the busy input-lock so the user can also type to interrupt,
while a mic segment in flight still locks input (double-turn窄缝).

All UI-only -- the backend golden (test_chat_tool_round / test_proactive_turn) and
the #1 cancellation chain are untouched. Qt offscreen, mirroring test_screenshot_ui.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from unittest.mock import patch  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.qt_overlay import OverlayWindow  # noqa: E402
from ui.widgets.input_panel import InputPanel  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ---------------- InputPanel widget level (B + A) ---------------- #

def test_stop_button_exists_and_hidden_by_default(qapp):
    panel = InputPanel()
    assert panel.stop_button.objectName() == "stopButton"
    assert hasattr(panel, "stop_requested")
    assert panel.stop_button.isHidden()  # not shown until a turn is active


def test_set_turn_active_toggles_stop_button_visibility(qapp):
    panel = InputPanel()
    panel.set_turn_active(True)
    assert not panel.stop_button.isHidden()
    panel.set_turn_active(False)
    assert panel.stop_button.isHidden()


def test_stop_button_visibility_is_orthogonal_to_busy_and_voice(qapp):
    # B cross-mode (widget level): visibility is governed ONLY by set_turn_active,
    # independent of set_busy / voice_enabled / input_enabled -- so nothing about the
    # input mode can hide the stop affordance while a turn is live.
    panel = InputPanel()
    panel.set_busy(True, voice_enabled=False, input_enabled=False)  # most-locked state
    panel.set_turn_active(True)
    assert not panel.stop_button.isHidden()
    panel.set_busy(False)
    panel.set_turn_active(True)
    assert not panel.stop_button.isHidden()


def test_stop_button_click_emits_stop_requested(qapp):
    panel = InputPanel()
    panel.set_turn_active(True)
    emitted = []
    panel.stop_requested.connect(lambda: emitted.append(True))
    panel.stop_button.click()
    assert emitted == [True]


def test_set_busy_input_enabled_decoupled_from_busy(qapp):
    # A: input/send follow input_enabled; screenshot still follows busy.
    panel = InputPanel()
    # turn active (she speaks): typeable even though busy
    panel.set_busy(True, input_enabled=True)
    assert panel.input.isEnabled() and panel.send_button.isEnabled()
    assert not panel.screenshot_button.isEnabled()  # screenshot still locked on busy
    # mic segment in flight: input locked (double-turn窄缝)
    panel.set_busy(True, input_enabled=False)
    assert not panel.input.isEnabled() and not panel.send_button.isEnabled()
    # idle
    panel.set_busy(False)
    assert panel.input.isEnabled() and panel.send_button.isEnabled()
    # default (no input_enabled) preserves pre-A `not busy` (test_screenshot_ui contract)
    panel.set_busy(True)
    assert not panel.input.isEnabled() and not panel.send_button.isEnabled()


# ---------------- qt_overlay glue (B + A) ---------------- #

class _FakeCSC:
    def __init__(self):
        self._busy = False
        self.stops = 0

    def is_busy(self):
        return self._busy

    def stop_current(self):
        self.stops += 1

    def shutdown(self, *args, **kwargs):  # closeEvent teardown
        pass


def _window():
    with patch.object(OverlayWindow, "_init_backend", lambda self: None):
        return OverlayWindow()


def test_set_busy_drives_stop_button_from_chat_busy_cross_mode(qapp):
    window = _window()
    window._is_song_busy = lambda: False
    csc = _FakeCSC()
    window.chat_stream_controller = csc

    # turn in flight -> stop shown in TEXT mode...
    csc._busy = True
    window.voice_input_controller.voice_mode_active = False
    window.set_busy(True)
    assert not window.input_panel.stop_button.isHidden()
    # ...and in VOICE mode (cross-mode: same chat-busy truth, not the input mode)
    window.voice_input_controller.voice_mode_active = True
    window.set_busy(True)
    assert not window.input_panel.stop_button.isHidden()

    # no turn (e.g. a mic recording segment sets busy=True, but chat is not busy) -> hidden
    csc._busy = False
    window.set_busy(True)
    assert window.input_panel.stop_button.isHidden()
    window.close()


def test_on_stop_requested_stops_and_resumes_voice(qapp):
    window = _window()
    csc = _FakeCSC()
    window.chat_stream_controller = csc
    resumed = []
    window._schedule_next_voice_recording = lambda delay_ms=320: resumed.append(delay_ms)

    # text mode: pure stop, NO voice resume
    window.voice_input_controller.voice_mode_active = False
    window._on_stop_requested()
    assert csc.stops == 1 and resumed == []

    # voice mode: stop + resume mic monitoring (mirrors _handle_chat_stream_done)
    window.voice_input_controller.voice_mode_active = True
    window._on_stop_requested()
    assert csc.stops == 2 and resumed == [320]
    window.close()


def test_set_busy_input_enabled_tracks_recording(qapp):
    window = _window()
    window._is_song_busy = lambda: False
    window.chat_stream_controller = _FakeCSC()  # not chat-busy
    # not recording -> input enabled (typeable while she speaks)
    window._is_recording = lambda: False
    window.set_busy(True)
    assert window.input_panel.input.isEnabled()
    # mic segment in flight IN VOICE MODE -> input locked (double-turn窄缝)
    window.voice_input_controller.voice_mode_active = True
    window._is_recording = lambda: True
    window.set_busy(True)
    assert not window.input_panel.input.isEnabled()
    # voice mode OFF but a lingering worker still "recording" -> input MUST stay usable.
    # The recording lock only guards the voice-mode double-turn race; with voice off the
    # worker's result is discarded, so the box must not be held disabled (regression:
    # a voice on/off toggle used to leave it permanently disabled).
    window.voice_input_controller.voice_mode_active = False
    window.set_busy(True)
    assert window.input_panel.input.isEnabled()
    window.close()


def test_voice_toggle_off_re_enables_text_input(qapp):
    # Regression (user-reported): enabling then disabling voice mode left the chat box
    # permanently unusable. Cause: stop() bumps voice_session_id and the in-flight
    # worker is still running when stop() calls set_busy (input locked), then the
    # worker's finished -> handle_finished early-returns on the now-stale session id,
    # so the re-enable never runs. Fix: the recording lock is voice-mode-gated.
    window = _window()
    window._is_song_busy = lambda: False
    window.chat_stream_controller = _FakeCSC()
    vc = window.voice_input_controller

    class _FakeWorker:
        def __init__(self):
            self._run = True

        def isRunning(self):
            return self._run

        def requestInterruption(self):
            pass

        def deleteLater(self):
            pass

    vc.speech_worker = _FakeWorker()  # voice ON, worker actively listening
    vc.voice_mode_active = True
    vc.voice_session_id = 1

    vc.stop()  # user toggles voice OFF while the worker is still running
    assert window.input_panel.input.isEnabled(), "text input disabled right after stop()"

    vc.speech_worker._run = False
    vc.handle_finished(1)  # stale worker finishes (old session id -> early return)
    assert window.input_panel.input.isEnabled(), "text input stayed disabled after toggle"
    window.close()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
