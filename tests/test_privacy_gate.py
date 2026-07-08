"""PrivacyGate unit contract (OO migration Phase 8-c2, 设计裁决 5).

Pins: the ocr purpose (check_safety passthrough + OVERLAY_COVERS, moved
verbatim from OcrStreamRunner._evaluate_safety), the watch purpose (state gate
ONLY -- the historical asymmetry: no check_safety), the owner_domain loud
ValueError (foreign targets are wiring bugs), and the unknown-purpose guard.
Behavioral integration stays pinned by tests/test_ocr_loop.py and
tests/test_watch_game_screen.py (both drive the real consumers).
"""

import unittest
from types import SimpleNamespace

from spica.galgame.privacy_gate import PrivacyGate
from spica.galgame.session import WATCH_SAFE_STATES, GalgameState
from spica.ports.window_locator import WindowGeometry, WindowSafetyResult
from spica.runtime.window import WindowTarget

_TARGET = WindowTarget(window_id="0x1", owner_domain="galgame", game_id="g1", match_rule="RULE")


class _RecordingLocator:
    def __init__(self, safety=WindowSafetyResult(ok=True), geometry=WindowGeometry(0, 0, 100, 100)):
        self.safety = safety
        self.geometry = geometry
        self.check_safety_calls = []
        self.geometry_calls = 0

    def check_safety(self, window_id, rule, overlay_window_id=None):
        self.check_safety_calls.append((window_id, rule, overlay_window_id))
        return self.safety

    def get_window_geometry(self, window_id):
        self.geometry_calls += 1
        return self.geometry


class OcrPurposeTest(unittest.TestCase):
    def test_check_safety_called_with_target_fields_and_ok_passthrough(self):
        locator = _RecordingLocator()
        result = PrivacyGate(locator, safe_states=WATCH_SAFE_STATES).evaluate(
            _TARGET, None, "ocr", overlay_window_id="0x5a"
        )
        self.assertTrue(result.ok)
        self.assertEqual(locator.check_safety_calls, [("0x1", "RULE", "0x5a")])

    def test_unsafe_result_passes_through_unchanged(self):
        unsafe = WindowSafetyResult(ok=False, reason_code="WINDOW_NOT_FOCUSED", reason="x")
        locator = _RecordingLocator(safety=unsafe)
        result = PrivacyGate(locator, safe_states=WATCH_SAFE_STATES).evaluate(
            _TARGET, None, "ocr"
        )
        self.assertIs(result, unsafe)  # verbatim passthrough, no re-wrap
        self.assertEqual(locator.geometry_calls, 0)  # short-circuits before overlay math

    def test_overlay_covering_dialog_region_returns_overlay_covers(self):
        locator = _RecordingLocator()
        result = PrivacyGate(locator, safe_states=WATCH_SAFE_STATES).evaluate(
            _TARGET, None, "ocr",
            overlay_rect=(0, 0, 100, 100),          # overlay covers the whole window
            dialog_ratios=(0.0, 0.0, 1.0, 1.0),     # dialog region = whole window
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "OVERLAY_COVERS")
        self.assertEqual(result.reason, "Spica overlay 覆盖了 OCR 对白区域。")

    def test_overlay_inputs_are_per_call_not_frozen(self):
        # 修正 4: the SAME gate instance sees different dynamic inputs per call
        # (the UI pushes overlay rects at runtime).
        locator = _RecordingLocator()
        gate = PrivacyGate(locator, safe_states=WATCH_SAFE_STATES)
        covered = gate.evaluate(
            _TARGET, None, "ocr",
            overlay_rect=(0, 0, 100, 100), dialog_ratios=(0.0, 0.0, 1.0, 1.0),
        )
        clear = gate.evaluate(_TARGET, None, "ocr", overlay_rect=None, dialog_ratios=(0.0, 0.0, 1.0, 1.0))
        self.assertFalse(covered.ok)
        self.assertTrue(clear.ok)


class WatchPurposeTest(unittest.TestCase):
    def test_unsafe_state_refuses_without_check_safety(self):
        locator = _RecordingLocator()
        result = PrivacyGate(locator, safe_states=WATCH_SAFE_STATES).evaluate(
            _TARGET, GalgameState.WINDOW_LOST, "watch"
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "GAME_WINDOW_NOT_SAFE")
        # The historical asymmetry: watch NEVER calls check_safety.
        self.assertEqual(locator.check_safety_calls, [])

    def test_safe_state_ok_without_check_safety(self):
        locator = _RecordingLocator()
        result = PrivacyGate(locator, safe_states=WATCH_SAFE_STATES).evaluate(
            _TARGET, GalgameState.PLAYING, "watch"
        )
        self.assertTrue(result.ok)
        self.assertEqual(locator.check_safety_calls, [])


class WiringGuardTest(unittest.TestCase):
    def test_foreign_owner_domain_raises_loud(self):
        # 修正 5c: the galgame gate never evaluates another domain's window --
        # a foreign target is a wiring bug and must be LOUD, not a quiet refusal.
        foreign = WindowTarget(window_id="0x9", owner_domain="cowatch")
        with self.assertRaises(ValueError):
            PrivacyGate(_RecordingLocator(), safe_states=WATCH_SAFE_STATES).evaluate(
                foreign, GalgameState.PLAYING, "watch"
            )

    def test_unknown_purpose_raises_loud(self):
        with self.assertRaises(ValueError):
            PrivacyGate(_RecordingLocator(), safe_states=WATCH_SAFE_STATES).evaluate(
                _TARGET, GalgameState.PLAYING, "screenshot"
            )


if __name__ == "__main__":
    unittest.main()
