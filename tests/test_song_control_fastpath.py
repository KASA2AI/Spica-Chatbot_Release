"""B2 (P2): the song control fast path + the AudioOwner arbitration -- the UI
layer the old song tests never covered (F17's gap, closed during the rewrite).

- Fast path: pause/resume/cancel/restart verbs are consumed ONLY while a song
  flow is live; out of a flow they fall through to normal chat. The confirmation
  flow is DEAD: a songless "唱首歌" never hijacks -- it reaches chat verbatim.
- Arbitration (the approved hard test): a READY song must NOT preempt the
  turn's own acknowledgment speech -- it queues on the prelude gate and plays
  exactly once after notify_on_current_stream_done fires.
"""

import os
import unittest
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PySide6 = pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, QThread  # noqa: E402

from agent_tools.function_tools.song import SongState  # noqa: E402
from ui.controllers.song_controller import SongController  # noqa: E402
from ui.controllers.interaction_controller import InteractionController  # noqa: E402


class _FakeAudioController:
    def __init__(self):
        self.played = []
        self.paused = 0
        self.resumed = 0
        self.stopped = 0

    def play_song(self, audio_path, token, on_finished=None, on_error=None):
        self.played.append(str(audio_path))
        self._on_finished = on_finished

    def pause_song(self):
        self.paused += 1
        return True

    def resume_song(self):
        self.resumed += 1
        return True

    def stop_song(self):
        self.stopped += 1

    def stop_owner(self, owner):
        pass


class _FakeWorker(QObject):
    """Stands in for SongWorker: never touches netease/RVC; the test drives the
    controller's ready/error callbacks directly."""

    def __init__(self, request, job_id, parent, config=None):
        super().__init__(parent)
        self.request = request
        self.job_id = job_id
        self.config = config  # P0b 2b: the controller now threads song config
        self.progress = _Signal()
        self.completed = _Signal()
        self.failed = _Signal()
        self.finished = _Signal()

    def start(self):
        pass

    def isRunning(self):
        return False

    def cancel(self):
        pass

    def deleteLater(self):  # noqa: N802 -- Qt name
        pass


class _Signal:
    def connect(self, *_args, **_kwargs):
        pass


class _FakeChatStream:
    def __init__(self, busy=True):
        self._busy = busy
        self.done_callbacks = []
        self.started_chats = []

    def is_busy(self):
        return self._busy

    def notify_on_current_stream_done(self, callback):
        if not self._busy:
            callback()
            return
        self.done_callbacks.append(callback)

    def fire_stream_done(self):
        for callback in self.done_callbacks:
            callback()
        self.done_callbacks = []

    def start_chat(self, message, screen_attachment=None):
        self.started_chats.append(message)

    def stop_current(self):
        pass


def _controller(chat_stream=None):
    statuses = []
    controller = SongController(
        parent=None,
        chat_stream_controller=chat_stream,
        audio_controller=_FakeAudioController(),
        set_song_status=statuses.append,
        set_busy=lambda busy: None,
        focus_input=lambda: None,
        stop_conversation_for_song=lambda: None,
        voice_mode_active_provider=lambda: False,
        schedule_voice_recording=lambda ms: None,
    )
    return controller, statuses


class ControlFastPathTest(unittest.TestCase):
    def test_pause_resume_consumed_only_while_live(self):
        controller, statuses = _controller()
        # IDLE: control words fall through to chat.
        self.assertFalse(controller.try_handle_control_text("暂停"))

        with patch("ui.controllers.song_controller.SongWorker", _FakeWorker):
            controller.start_song_request(_request())
        controller._set_state(SongState.PLAYING)
        self.assertTrue(controller.try_handle_control_text("暂停"))
        self.assertEqual(controller.ui_state.state, SongState.PAUSED)
        self.assertIn("⏸ 已暂停——说「继续」接着唱", statuses)
        self.assertTrue(controller.try_handle_control_text("继续"))
        self.assertEqual(controller.ui_state.state, SongState.PLAYING)

    def test_cancel_verb_and_non_control_falls_through(self):
        controller, _statuses = _controller()
        with patch("ui.controllers.song_controller.SongWorker", _FakeWorker):
            controller.start_song_request(_request())
        controller._set_state(SongState.PLAYING)
        # A normal sentence is NOT consumed by the fast path.
        self.assertFalse(controller.try_handle_control_text("这首歌真好听"))
        self.assertTrue(controller.try_handle_control_text("别唱了"))
        self.assertEqual(controller.ui_state.state, SongState.IDLE)


class HijackDeathTest(unittest.TestCase):
    """The confirmation flow is dead: song-ish chat reaches chat verbatim."""

    def test_songless_request_goes_to_chat_untouched(self):
        chat_stream = _FakeChatStream(busy=False)
        controller, _ = _controller(chat_stream)
        interaction = InteractionController(
            parent=None,
            chat_stream_controller=chat_stream,
            song_controller=controller,
            audio_controller=_FakeAudioController(),
            voice_input_controller=_DummyVoice(),
            focus_input=lambda: None,
            set_busy=lambda busy: None,
        )
        interaction.handle_user_text("唱首歌")  # no song name -> NO hijack (B2)
        self.assertEqual(chat_stream.started_chats, ["唱首歌"])
        self.assertEqual(controller.ui_state.state, SongState.IDLE)


class _DummyVoice:
    voice_mode_active = False  # faithful to VoiceInputController (A: send-path gate)

    def interrupt_current_recording(self):
        pass


def _request():
    from agent_tools.function_tools.song.models import SongRequest

    return SongRequest(query="稻香", title="稻香", artist="周杰伦", user_text="唱稻香")


class AudioOwnerArbitrationTest(unittest.TestCase):
    """The approved hard test: READY song queues behind the acknowledgment."""

    def test_ready_song_waits_for_turn_speech_then_plays_once(self):
        chat_stream = _FakeChatStream(busy=True)
        controller, statuses = _controller(chat_stream)
        audio = controller.audio_controller

        with patch("ui.controllers.song_controller.SongWorker", _FakeWorker):
            controller.handle_song_request_event(query="稻香", title="稻香", artist="周杰伦")

        # Preparing, prelude gate CLOSED (the turn is still speaking).
        self.assertEqual(controller.ui_state.state, SongState.PREPARING)
        self.assertFalse(controller.ui_state.playback_gate.prelude_done)
        self.assertEqual(len(chat_stream.done_callbacks), 1)

        # Song becomes READY while she is still talking -> must NOT play.
        controller.handle_song_ready(
            controller.ui_state.session_id, {"ok": True, "final_audio_path": "/tmp/song.wav"})
        self.assertEqual(audio.played, [])  # no preemption
        self.assertEqual(controller.ui_state.state, SongState.READY)

        # Her speech finishes -> the gate opens -> plays exactly once.
        chat_stream.fire_stream_done()
        self.assertEqual(audio.played, ["/tmp/song.wav"])
        self.assertEqual(controller.ui_state.state, SongState.PLAYING)
        self.assertIn("🎵 唱歌中", statuses)

    def test_speech_already_finished_plays_immediately_on_ready(self):
        chat_stream = _FakeChatStream(busy=False)  # nothing playing -> gate opens now
        controller, _ = _controller(chat_stream)
        with patch("ui.controllers.song_controller.SongWorker", _FakeWorker):
            controller.handle_song_request_event(query="稻香")
        self.assertTrue(controller.ui_state.playback_gate.prelude_done)
        controller.handle_song_ready(
            controller.ui_state.session_id, {"ok": True, "final_audio_path": "/tmp/song.wav"})
        self.assertEqual(controller.audio_controller.played, ["/tmp/song.wav"])


class ProactiveFinishReportTest(unittest.TestCase):
    """P3 first use case: finishing a song submits a song-named directive to the
    injected proactive callback (the arbiter in production)."""

    def test_finish_submits_directive_with_song_name(self):
        requests = []
        chat_stream = _FakeChatStream(busy=False)
        controller, _ = _controller(chat_stream)
        controller.request_proactive_turn = requests.append

        with patch("ui.controllers.song_controller.SongWorker", _FakeWorker):
            controller.handle_song_request_event(query="稻香", title="稻香", artist="周杰伦")
        controller.handle_song_ready(
            controller.ui_state.session_id, {"ok": True, "final_audio_path": "/tmp/song.wav"})
        controller.finish_song_playback()

        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.source, "song")
        self.assertEqual(request.policy, "drop_if_busy")
        self.assertIn("稻香", request.directive)
        self.assertIn("周杰伦", request.directive)
        self.assertIn("唱完", request.directive)

    def test_no_callback_injected_finish_stays_silent(self):
        chat_stream = _FakeChatStream(busy=False)
        controller, _ = _controller(chat_stream)  # request_proactive_turn=None
        with patch("ui.controllers.song_controller.SongWorker", _FakeWorker):
            controller.handle_song_request_event(query="稻香")
        controller.handle_song_ready(
            controller.ui_state.session_id, {"ok": True, "final_audio_path": "/tmp/song.wav"})
        controller.finish_song_playback()  # must not raise
        self.assertEqual(controller.ui_state.state, SongState.IDLE)


if __name__ == "__main__":
    unittest.main()
