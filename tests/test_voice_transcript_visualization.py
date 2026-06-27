"""Voice-mode visualisation (display-only) acceptance pins.

In voice mode the recognized WHOLE sentence is mirrored into the input box before
the existing auto-submit, then cleared after a brief linger -- a purely visual
"Apple voice-typing" effect. Hard constraint: it must NOT change the
voice->auto-submit semantics (handle_user_text is always called with the same text),
must be voice-mode only, and must never clobber a user draft or wipe newer content.

All UI-only: speech_worker / VoiceInputController / STT / run_turn are untouched
(this feature reuses the existing on_recognized_text indirection point, which already
runs on the GUI thread). Qt offscreen, mirroring test_stop_button_interrupt.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from unittest.mock import Mock, patch  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.qt_overlay import OverlayWindow  # noqa: E402
from ui.widgets.input_panel import InputPanel  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ---------------- InputPanel widget level (the two thin display methods) ---------------- #

def test_set_voice_transcript_writes_input(qapp):
    panel = InputPanel()
    panel.set_voice_transcript("你好世界")
    assert panel.input.text() == "你好世界"


def test_clear_voice_transcript_empties_input(qapp):
    panel = InputPanel()
    panel.set_voice_transcript("你好世界")
    panel.clear_voice_transcript()
    assert panel.input.text() == ""


# ---------------- qt_overlay glue ---------------- #

def _window():
    with patch.object(OverlayWindow, "_init_backend", lambda self: None):
        return OverlayWindow()


def _voice_window(qapp, *, active=True):
    """A backend-less OverlayWindow in (or out of) voice mode, with handle_user_text
    replaced by a Mock so the display behaviour is isolated from the chat backend."""
    window = _window()
    window.voice_input_controller.voice_mode_active = active
    window.interaction_controller.handle_user_text = Mock()
    return window


def test_recognized_text_wired_to_visualization_seam(qapp):
    # The on_recognized_text indirection point must route through the visualisation
    # wrapper (not straight to handle_user_text) -- otherwise nothing would ever show.
    window = _window()
    assert window.voice_input_controller.on_recognized_text == window._on_voice_recognized_text
    window.close()


def test_voice_mode_shows_transcript_in_box(qapp):  # (a)
    window = _voice_window(qapp)
    window._on_voice_recognized_text("她刚才为什么生气")
    assert window.input_panel.input.text() == "她刚才为什么生气"
    window.close()


def test_voice_mode_still_auto_submits(qapp):  # (c) semantics unchanged
    window = _voice_window(qapp)
    window._on_voice_recognized_text("她刚才为什么生气")
    window.interaction_controller.handle_user_text.assert_called_once_with("她刚才为什么生气")
    window.close()


def test_clear_callback_empties_box(qapp):  # (b)
    window = _voice_window(qapp)
    window._on_voice_recognized_text("你好")
    assert window.input_panel.input.text() == "你好"
    window._clear_voice_transcript()  # what the linger timer invokes
    assert window.input_panel.input.text() == ""
    window.close()


def test_non_voice_mode_does_not_write_box(qapp):  # (①) display strictly voice-gated
    window = _voice_window(qapp, active=False)
    assert window.input_panel.input.text() == ""
    window._on_voice_recognized_text("不该出现")
    assert window.input_panel.input.text() == ""  # not written outside voice mode
    # ...but the auto-submit path is unconditional (semantics never gated)
    window.interaction_controller.handle_user_text.assert_called_once_with("不该出现")
    window.close()


def test_manual_draft_not_clobbered_but_still_submits(qapp):  # (d)
    window = _voice_window(qapp)
    window.input_panel.input.setText("用户手输草稿")
    window._on_voice_recognized_text("语音整句")
    # the draft is preserved (preview skipped), yet the voice sentence still submits
    assert window.input_panel.input.text() == "用户手输草稿"
    window.interaction_controller.handle_user_text.assert_called_once_with("语音整句")
    window.close()


def test_clear_does_not_wipe_changed_content(qapp):  # (②)
    window = _voice_window(qapp)
    window._on_voice_recognized_text("你好")
    assert window.input_panel.input.text() == "你好"
    # the box content changes after the preview was written (e.g. a newer sentence /
    # the user typing); the stale linger timer must NOT wipe it.
    window.input_panel.input.setText("之后的新内容")
    window._clear_voice_transcript()
    assert window.input_panel.input.text() == "之后的新内容"
    window.close()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
