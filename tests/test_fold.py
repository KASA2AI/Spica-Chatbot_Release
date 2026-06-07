"""Unit tests for the C2 sync fold (spica/runtime/fold.py).

Feeds hand-built RuntimeEvent streams (no orchestrator) and locks:
- the consumed whitelist fold guarantees on the success path;
- the difference whitelist it intentionally drops (documented lossiness);
- the error payload shape when a turn produces error + no done.
"""

import unittest

from spica.core.events import (
    DoneEvent,
    ErrorEvent,
    StatusEvent,
    UnitReadyEvent,
    UnitTextReadyEvent,
)
from spica.runtime.fold import fold_events


def _success_stream():
    return [
        StatusEvent(state="thinking"),
        UnitTextReadyEvent(0, "あ。", "あ。", "happy"),
        UnitReadyEvent(0, "あ。", "あ。", "happy", {"expression_id": "002"}, "/a0.wav", "/tmp/a0.wav"),
        UnitReadyEvent(1, "い。", "い。", "happy", {"expression_id": "003"}, "/a1.wav", "/tmp/a1.wav"),
        DoneEvent("あ。い。", "happy", "喜/乐", "r", 2, {"done_ms": 9.0}),
    ]


class FoldSuccessTest(unittest.TestCase):
    def test_consumed_whitelist_fields(self):
        payload = fold_events(_success_stream(), conversation_id="c1")
        self.assertEqual(payload["answer"], "あ。い。")
        self.assertEqual(payload["conversation_id"], "c1")
        self.assertEqual(payload["emotion"], {"name": "happy", "label": "喜/乐", "reason": "r"})
        # audio + visual come from the FIRST unit (documented representative).
        self.assertEqual(payload["audio_url"], "/a0.wav")
        self.assertEqual(payload["audio_path"], "/tmp/a0.wav")
        self.assertEqual(payload["visual"], {"expression_id": "002"})
        self.assertEqual(payload["timing"], {"done_ms": 9.0})

    def test_difference_whitelist_fields_are_dropped(self):
        # Intentionally lossy vs the old build_response_node payload; no consumer.
        payload = fold_events(_success_stream())
        for dropped in ("tts_chunks", "tts_chunk_audio", "tts_params", "tools"):
            self.assertNotIn(dropped, payload)
        self.assertNotIn("error", payload)  # success path carries no error

    def test_done_without_units_yields_null_audio(self):
        events = [StatusEvent(state="thinking"), DoneEvent("x", "happy", "喜/乐", "r", 0, {})]
        payload = fold_events(events)
        self.assertEqual(payload["answer"], "x")
        self.assertIsNone(payload["audio_url"])
        self.assertEqual(payload["visual"], {})


class FoldErrorTest(unittest.TestCase):
    def test_error_event_yields_error_payload(self):
        payload = fold_events([StatusEvent(state="thinking"), ErrorEvent("boom")], conversation_id="c1")
        self.assertIn("error", payload)
        self.assertEqual(payload["error"]["message"], "boom")
        self.assertEqual(payload["conversation_id"], "c1")
        self.assertIsNone(payload["audio_url"])
        self.assertIsNone(payload["audio_path"])
        self.assertTrue(payload["answer"])  # shaped, non-empty

    def test_no_done_no_error_is_still_error_shaped(self):
        payload = fold_events([StatusEvent(state="thinking")])
        self.assertIn("error", payload)
        self.assertIsNone(payload["audio_url"])


if __name__ == "__main__":
    unittest.main()
