"""Parity harness self-verification (LOCAL_RUNTIME_PLAN §6.5).

CI uses SYNTHETIC stub providers ONLY -- never a real OCR model / GPU / image
(§6.5: GitHub-hosted CI has no GPU). Two pins:
- old-vs-old: two identical stubs MUST report ~0 diff / verdict=pass with all
  report fields present. This is the "harness is correct" proof: if the core or
  comparator were buggy, identical-in -> identical-out would not hold.
- it must also be ABLE to fail: a differing stub MUST drop match_rate and flip
  verdict to fail (a harness that is always-green is worthless as a gate).
"""

import unittest

from spica.local_runtime.parity import run_parity
from spica.local_runtime.parity.comparators import text_diff


class _StubOcr:
    """A deterministic OCR stand-in: maps a reference key -> recognized text."""

    def __init__(self, name: str, mapping: dict[str, str]) -> None:
        self.name = name
        self._mapping = mapping

    def recognize_text(self, key: str) -> str:
        return self._mapping.get(key, "")


# A fixed, versioned synthetic reference set (covers normal line / name box /
# punctuation / 【】 boundary -- the known OCR edge cases, as plain strings).
REFERENCE_KEYS = ["normal_line", "name_box", "long_sentence", "punct", "brackets"]
GOLDEN = {
    "normal_line": "今日はいい天気ですね",
    "name_box": "莉莉子",
    "long_sentence": "ふぅん……男の価値観って結局そういうものなのね。",
    "punct": "本当に！？",
    "brackets": "【選択肢】はい / いいえ",
}


class ParityHarnessSelfVerifyTest(unittest.TestCase):
    def test_identical_providers_report_zero_diff_and_pass(self):
        old = _StubOcr("rapidocr", GOLDEN)
        new = _StubOcr("rapidocr_ort", dict(GOLDEN))  # identical content

        report = run_parity(
            REFERENCE_KEYS,
            run_old=old.recognize_text,
            run_new=new.recognize_text,
            comparator=text_diff,
            model="ocr",
            provider_old="rapidocr",
            provider_new="rapidocr_ort",
        )

        self.assertEqual(report.verdict, "pass")
        self.assertTrue(report.is_pass)
        self.assertEqual(report.aggregate["count"], len(REFERENCE_KEYS))
        self.assertEqual(report.aggregate["match_rate"], 1.0)
        self.assertEqual(report.aggregate["max_error"], 0.0)
        self.assertEqual(report.aggregate["mean_error"], 0.0)
        # Every input present, each fully populated.
        self.assertEqual(len(report.per_input), len(REFERENCE_KEYS))
        for item in report.per_input:
            self.assertTrue(item.match)
            self.assertEqual(item.error_value, 0.0)
        # Report is JSON-shaped (gate is script-judged).
        as_dict = report.to_dict()
        for key in ("model", "provider_old", "provider_new", "per_input", "aggregate", "threshold", "verdict"):
            self.assertIn(key, as_dict)

    def test_differing_provider_drops_match_rate_and_fails(self):
        old = _StubOcr("rapidocr", GOLDEN)
        corrupted = dict(GOLDEN)
        corrupted["name_box"] = "莉莉孑"  # one-character OCR error
        corrupted["punct"] = "本当に。"  # different punctuation
        new = _StubOcr("rapidocr_ort", corrupted)

        report = run_parity(
            REFERENCE_KEYS,
            run_old=old.recognize_text,
            run_new=new.recognize_text,
            comparator=text_diff,
            model="ocr",
            provider_old="rapidocr",
            provider_new="rapidocr_ort",
        )

        self.assertEqual(report.verdict, "fail")
        self.assertFalse(report.is_pass)
        self.assertLess(report.aggregate["match_rate"], 1.0)
        self.assertGreater(report.aggregate["max_error"], 0.0)

    def test_clock_injection_drives_timings(self):
        # Deterministic clock -> deterministic per-input timings (no real wall clock).
        ticks = iter([0.0, 0.001, 0.0035] * len(REFERENCE_KEYS))
        old = _StubOcr("a", GOLDEN)
        new = _StubOcr("b", GOLDEN)
        report = run_parity(
            REFERENCE_KEYS,
            run_old=old.recognize_text,
            run_new=new.recognize_text,
            comparator=text_diff,
            model="ocr",
            provider_old="a",
            provider_new="b",
            clock=lambda: next(ticks),
        )
        self.assertAlmostEqual(report.per_input[0].old_ms, 1.0, places=3)
        self.assertAlmostEqual(report.per_input[0].new_ms, 2.5, places=3)


if __name__ == "__main__":
    unittest.main()
