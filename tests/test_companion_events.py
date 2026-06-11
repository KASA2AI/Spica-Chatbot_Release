"""Phase 4: galgame companion events round-trip (typed, registered).

Each event is a RuntimeEvent subclass registered into events._FROM_DATA on import,
so event_from_legacy reconstructs the concrete type (not GenericEvent) losslessly.
"""

import unittest

from spica.core import companion_events as ce
from spica.core.events import GenericEvent, event_from_legacy

SAMPLES = [
    ce.GalgameStatusChangedEvent(state="playing", previous="paused", message="m"),
    ce.GalgameWindowLostEvent(reason="occluded"),
    ce.GalgameWindowRecoveredEvent(),
    ce.GalgameSummaryStartedEvent(reason="end"),
    ce.GalgameSummaryProgressEvent(progress=0.5, message="half"),
    ce.GalgameSummaryDoneEvent(summary_id="SM1"),
    ce.GalgameStableLineCommittedEvent(line_id="L1", speaker="朱比華", text="こんにちは"),
    ce.GalgameChoiceDetectedEvent(choice_id="C1", options=[{"index": 1, "text": "a"}]),
    ce.GalgameChoiceRecordedEvent(choice_id="C1", selected_index=2, selected_text="b"),
    ce.GalgameErrorEvent(message="boom", code="X", session_id="S1", target_state="paused"),
    # Phase 5 binding events
    ce.GalgameWindowCandidatesEvent(candidates=[{"window_id": "0x1", "title": "t"}], mode="pick"),
    ce.GalgameGameBoundEvent(game_id="ABC", window_id="0x1", title="t"),
    ce.GalgameBindFailedEvent(reason="r", code="C", options=["cancel"]),
    # Phase 6 OCR calibration events
    ce.GalgameOcrPreviewReadyEvent(region="dialog", image_png=b"\x89PNGabc", width=10, height=5, suspect_blank=True),
    ce.GalgameOcrTestResultEvent(dialog_text="x", speaker_text="y", speaker_strategy="region"),
]


class CompanionEventRoundTripTest(unittest.TestCase):
    def test_round_trip_typed_and_lossless(self):
        for event in SAMPLES:
            with self.subTest(kind=event.kind):
                legacy = event.to_legacy_dict()
                back = event_from_legacy(legacy)
                self.assertEqual(back, event)  # typed reconstruction equals original
                self.assertNotIsInstance(back, GenericEvent)  # registered -> strongly typed
                self.assertEqual(back.to_legacy_dict(), legacy)  # lossless

    def test_summary_done_none_round_trips(self):
        back = event_from_legacy(ce.GalgameSummaryDoneEvent().to_legacy_dict())
        self.assertIsNone(back.summary_id)

    def test_error_event_carries_full_context(self):
        data = ce.GalgameErrorEvent(
            message="m", code="C", session_id="S1", target_state="ending"
        ).to_legacy_dict()["data"]
        self.assertEqual(data["session_id"], "S1")
        self.assertEqual(data["target_state"], "ending")
        self.assertEqual(data["code"], "C")


if __name__ == "__main__":
    unittest.main()
