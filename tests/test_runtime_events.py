"""Phase 6A: RuntimeEvent <-> legacy dict bidirectional equivalence.

Locks the lossless round-trip in both directions for every event kind the
streaming pipeline emits, so the typed boundary and the legacy dict boundary are
interchangeable (the UI path routes dicts through RuntimeEvent and back).
"""

import unittest

from spica.core.events import (
    DoneEvent,
    ErrorEvent,
    GenericEvent,
    RuntimeEvent,
    StatusEvent,
    UnitReadyEvent,
    event_from_legacy,
)

# Representative legacy dicts matching the exact shapes emitted by
# agent/streaming_pipeline.py (status, unit_text_ready, unit_visual_ready,
# unit_audio_started, unit_audio_ready, unit_ready [+/- audio_error], done, error).
LEGACY_EVENTS = [
    {"event": "status", "data": {"state": "thinking", "message": "thinking"}},
    {"event": "status", "data": {"state": "tools", "message": "inspecting_screen"}},
    {
        "event": "unit_text_ready",
        "data": {"index": 0, "display_text": "あ。", "tts_text": "あ。", "emotion": "happy",
                 "timing": {"unit_created_ms": 1.0}},
    },
    {
        "event": "unit_visual_ready",
        "data": {"index": 0, "visual": {"expression_id": "002", "selection_source": "local_vote_classifier"},
                 "cue": {"index": 0, "expression_id": "002"}, "visual_error": None,
                 "timing": {"visual_ms": 3.0, "visual_ready_ms": 4.0}},
    },
    {
        "event": "unit_audio_started",
        "data": {"index": 0, "tts_text": "あ。", "emotion": "happy", "timing": {"tts_start_ms": 1.0}},
    },
    {
        "event": "unit_audio_ready",
        "data": {"index": 0, "audio_url": "/a.wav", "audio_path": "/tmp/a.wav", "audio_error": None,
                 "timing": {"tts_ms": 2.0, "tts_start_ms": 1.0, "tts_done_ms": 3.0}},
    },
    {
        "event": "unit_ready",
        "data": {"index": 0, "display_text": "あ。", "tts_text": "あ。", "emotion": "happy",
                 "visual": {"expression_id": "002"}, "audio_url": "/a.wav", "audio_path": "/tmp/a.wav",
                 "timing": {"visual_ms": 3.0, "tts_ms": 2.0, "unit_ready_ms": 5.0}},
    },
    {  # unit_ready WITH the optional audio_error key
        "event": "unit_ready",
        "data": {"index": 1, "display_text": "い。", "tts_text": "い。", "emotion": "sad",
                 "visual": {}, "audio_url": None, "audio_path": None,
                 "timing": {"unit_ready_ms": 6.0}, "audio_error": "TTS boom"},
    },
    {
        "event": "done",
        "data": {"answer": "あ。い。", "emotion": "happy", "emotion_label": "喜/乐",
                 "emotion_reason": "r", "units_count": 2, "timing": {"done_ms": 9.0}},
    },
    {"event": "error", "data": {"message": "boom"}},
]


class RuntimeEventEquivalenceTest(unittest.TestCase):
    def test_dict_to_event_to_dict_is_identity(self):
        for legacy in LEGACY_EVENTS:
            with self.subTest(kind=legacy["event"], has_err="audio_error" in legacy["data"]):
                event = event_from_legacy(legacy)
                self.assertIsInstance(event, RuntimeEvent)
                self.assertEqual(event.to_legacy_dict(), legacy)

    def test_event_to_dict_to_event_is_identity(self):
        for legacy in LEGACY_EVENTS:
            with self.subTest(kind=legacy["event"]):
                event = event_from_legacy(legacy)
                self.assertEqual(event_from_legacy(event.to_legacy_dict()), event)

    def test_kind_and_typed_fields(self):
        status = event_from_legacy(LEGACY_EVENTS[0])
        self.assertIsInstance(status, StatusEvent)
        self.assertEqual(status.kind, "status")
        self.assertEqual(status.state, "thinking")

        done = event_from_legacy(LEGACY_EVENTS[-2])
        self.assertIsInstance(done, DoneEvent)
        self.assertEqual(done.units_count, 2)
        self.assertEqual(done.answer, "あ。い。")

        err = event_from_legacy(LEGACY_EVENTS[-1])
        self.assertIsInstance(err, ErrorEvent)
        self.assertEqual(err.message, "boom")

    def test_unit_ready_audio_error_is_omitted_when_absent(self):
        without = event_from_legacy(LEGACY_EVENTS[6])  # no audio_error
        self.assertIsInstance(without, UnitReadyEvent)
        self.assertIsNone(without.audio_error)
        self.assertNotIn("audio_error", without.to_legacy_dict()["data"])

        with_err = event_from_legacy(LEGACY_EVENTS[7])
        self.assertEqual(with_err.audio_error, "TTS boom")
        self.assertIn("audio_error", with_err.to_legacy_dict()["data"])

    def test_unknown_kind_falls_back_to_generic_losslessly(self):
        weird = {"event": "future_event", "data": {"x": 1, "y": [1, 2]}}
        event = event_from_legacy(weird)
        self.assertIsInstance(event, GenericEvent)
        self.assertEqual(event.to_legacy_dict(), weird)

    def test_passthrough_runtime_event(self):
        e = StatusEvent(state="thinking")
        self.assertIs(event_from_legacy(e), e)


if __name__ == "__main__":
    unittest.main()
