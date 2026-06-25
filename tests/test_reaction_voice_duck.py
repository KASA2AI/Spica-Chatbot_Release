"""P5 option A: idle-listen preemption so a galgame reaction can fire in VOICE
mode (before this, the always-listening mic made the P3 arbiter perpetually busy
-> every reaction was busy_drop'd, even correctly-judged worth-8 beats).

Pins:
  - VoiceInputController.is_capturing_user_speech is True ONLY while a worker is
    running AND the hardware VAD has detected speech -- an idle-listening worker
    is NOT "the user speaking" (the audio-layer half is in test_respeaker_audio).
  - ReactionVoiceDuckGate.before_system_speech ducks the mic iff in voice mode;
    after_system_speech is a no-op (resume rides the existing on_chat_done path,
    so ducking AND resuming here would double-start the mic).
"""

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from ui.controllers.voice_input_controller import (
    ReactionVoiceDuckGate,
    VoiceInputController,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _controller(*, voice_mode=False, worker=None):
    controller = VoiceInputController(
        parent=None,
        set_voice_active=lambda *_: None,
        set_busy=lambda *_: None,
        is_conversation_busy=lambda: False,
        set_dialogue_text=lambda *_: None,
        on_recognized_text=lambda *_: None,
        backend_ready=lambda: True,
    )
    controller.voice_mode_active = voice_mode
    controller.speech_worker = worker
    return controller


def _worker(*, running=True, capturing=False, on_interrupt=None):
    return SimpleNamespace(
        isRunning=lambda: running,
        is_capturing_user_speech=lambda: capturing,
        requestInterruption=on_interrupt or (lambda: None),
    )


# -- is_capturing_user_speech (the narrowed "busy" truth) ---------------------

def test_is_capturing_false_when_no_worker(qapp):
    assert _controller().is_capturing_user_speech() is False


def test_is_capturing_false_when_worker_idle(qapp):
    # mic running but VAD has not detected speech -> idle-listening, NOT busy.
    controller = _controller(worker=_worker(running=True, capturing=False))
    assert controller.is_capturing_user_speech() is False


def test_is_capturing_true_when_worker_mid_utterance(qapp):
    controller = _controller(worker=_worker(running=True, capturing=True))
    assert controller.is_capturing_user_speech() is True


def test_is_capturing_false_when_worker_finished(qapp):
    # a stale worker that captured but is no longer running is not "speaking now".
    controller = _controller(worker=_worker(running=False, capturing=True))
    assert controller.is_capturing_user_speech() is False


# -- ReactionVoiceDuckGate (duck the idle mic when she speaks) -----------------

def test_gate_ducks_idle_mic_in_voice_mode(qapp):
    interrupted = []
    controller = _controller(
        voice_mode=True, worker=_worker(on_interrupt=lambda: interrupted.append(True))
    )
    before = controller.voice_session_id
    ReactionVoiceDuckGate(controller).before_system_speech()
    assert interrupted == [True]                       # mic ducked off her TTS
    assert controller.voice_session_id == before + 1   # stale result invalidated


def test_gate_is_noop_outside_voice_mode(qapp):
    interrupted = []
    controller = _controller(
        voice_mode=False, worker=_worker(on_interrupt=lambda: interrupted.append(True))
    )
    before = controller.voice_session_id
    ReactionVoiceDuckGate(controller).before_system_speech()
    assert interrupted == []                            # text mode never holds the mic
    assert controller.voice_session_id == before


def test_gate_after_speech_does_not_touch_mic(qapp):
    # resume is the existing on_chat_done path; a resume here would double-start.
    calls = []
    controller = _controller(
        voice_mode=True, worker=_worker(on_interrupt=lambda: calls.append("int"))
    )
    before = controller.voice_session_id
    ReactionVoiceDuckGate(controller).after_system_speech()
    assert calls == []
    assert controller.voice_session_id == before


def test_gate_none_controller_is_safe():
    gate = ReactionVoiceDuckGate(None)
    gate.before_system_speech()  # must not raise
    gate.after_system_speech()
