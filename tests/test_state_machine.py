"""Phase 6E: ChatStateMachine transitions."""

import unittest

from spica.core.events import (
    DoneEvent,
    ErrorEvent,
    StatusEvent,
    UnitReadyEvent,
    UnitTextReadyEvent,
)
from spica.core.state_machine import ChatState, ChatStateMachine


def _unit_ready(index=0):
    return UnitReadyEvent(
        index=index, display_text="あ", tts_text="あ", emotion="happy",
        visual={}, audio_url="/a.wav", audio_path="/tmp/a.wav", timing={},
    )


class ChatStateMachineTest(unittest.TestCase):
    def test_starts_idle(self):
        self.assertEqual(ChatStateMachine().state, ChatState.IDLE)

    def test_turn_lifecycle_generating_streaming_speaking_idle(self):
        sm = ChatStateMachine()
        self.assertEqual(sm.start_turn(), ChatState.GENERATING)
        self.assertEqual(sm.on_runtime_event(StatusEvent(state="thinking")), ChatState.GENERATING)
        self.assertEqual(sm.on_runtime_event(UnitTextReadyEvent(0, "あ", "あ", "happy")), ChatState.STREAMING)
        self.assertEqual(sm.on_playback_started(), ChatState.SPEAKING)
        # done while still speaking -> remains SPEAKING (generation_done flag set)
        self.assertEqual(sm.on_runtime_event(DoneEvent("あ", "happy", "喜/乐", "r", 1)), ChatState.SPEAKING)
        self.assertTrue(sm.generation_done)
        # audio finishes after generation done -> IDLE
        self.assertEqual(sm.on_playback_finished(), ChatState.IDLE)
        self.assertFalse(sm.is_busy)

    def test_units_keep_speaking_when_already_playing(self):
        sm = ChatStateMachine()
        sm.start_turn()
        sm.on_playback_started()
        # later units arriving must not knock us out of SPEAKING
        self.assertEqual(sm.on_runtime_event(_unit_ready(1)), ChatState.SPEAKING)

    def test_playback_finished_before_done_returns_to_streaming(self):
        sm = ChatStateMachine()
        sm.start_turn()
        sm.on_runtime_event(_unit_ready(0))
        sm.on_playback_started()
        # unit 0 audio finished but generation not done yet -> back to STREAMING
        self.assertEqual(sm.on_playback_finished(), ChatState.STREAMING)

    def test_pause_resume(self):
        sm = ChatStateMachine()
        sm.start_turn()
        sm.on_playback_started()
        self.assertEqual(sm.pause(), ChatState.PAUSED)
        # units arriving while paused stay paused
        self.assertEqual(sm.on_runtime_event(_unit_ready(1)), ChatState.PAUSED)
        self.assertEqual(sm.resume(), ChatState.SPEAKING)

    def test_pause_only_from_speaking(self):
        sm = ChatStateMachine()
        sm.start_turn()  # GENERATING
        self.assertEqual(sm.pause(), ChatState.GENERATING)  # no-op

    def test_error_is_sticky_until_stop(self):
        sm = ChatStateMachine()
        sm.start_turn()
        self.assertEqual(sm.on_runtime_event(ErrorEvent("boom")), ChatState.ERROR)
        self.assertFalse(sm.is_busy)
        # further events don't leave ERROR
        self.assertEqual(sm.on_runtime_event(_unit_ready(0)), ChatState.ERROR)
        self.assertEqual(sm.on_playback_started(), ChatState.ERROR)
        # explicit reset clears it
        self.assertEqual(sm.stop(), ChatState.IDLE)

    def test_listening(self):
        sm = ChatStateMachine()
        self.assertEqual(sm.start_listening(), ChatState.LISTENING)
        self.assertTrue(sm.is_busy)
        self.assertEqual(sm.on_runtime_event(StatusEvent(state="thinking")), ChatState.GENERATING)

    def test_stop_resets(self):
        sm = ChatStateMachine()
        sm.start_turn()
        sm.on_playback_started()
        self.assertEqual(sm.stop(), ChatState.IDLE)
        self.assertFalse(sm.is_busy)


if __name__ == "__main__":
    unittest.main()
