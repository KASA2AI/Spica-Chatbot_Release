"""OCR parity comparator (LOCAL_RUNTIME_PLAN §6.5: test_ocr_comparator).

``text_diff`` judgement on synthetic samples covering the four OCR failure shapes
the constitution calls out -- identical / wrong-char / extra-char / missing-char
-- plus empties and non-str coercion. Pure-python, no real model.
"""

import unittest

from spica.local_runtime.parity.comparators import text_diff


class OcrComparatorTest(unittest.TestCase):
    def test_identical_is_match_zero_error(self):
        match, error = text_diff("今日はいい天気", "今日はいい天気")
        self.assertTrue(match)
        self.assertEqual(error, 0.0)

    def test_wrong_char_is_mismatch_with_error(self):
        match, error = text_diff("莉莉子", "莉莉孑")  # substitution
        self.assertFalse(match)
        self.assertGreater(error, 0.0)
        self.assertLessEqual(error, 1.0)
        self.assertAlmostEqual(error, 1 / 3, places=6)

    def test_extra_char_is_mismatch(self):
        match, error = text_diff("はい", "はいい")  # insertion
        self.assertFalse(match)
        self.assertAlmostEqual(error, 1 / 3, places=6)

    def test_missing_char_is_mismatch(self):
        match, error = text_diff("ありがとう", "ありがと")  # deletion
        self.assertFalse(match)
        self.assertAlmostEqual(error, 1 / 5, places=6)

    def test_both_empty_is_match(self):
        match, error = text_diff("", "")
        self.assertTrue(match)
        self.assertEqual(error, 0.0)

    def test_one_empty_is_full_error(self):
        match, error = text_diff("", "abc")
        self.assertFalse(match)
        self.assertEqual(error, 1.0)

    def test_none_is_coerced_not_crashed(self):
        match, error = text_diff(None, None)
        self.assertTrue(match)
        self.assertEqual(error, 0.0)
        match2, _ = text_diff(None, "x")
        self.assertFalse(match2)


if __name__ == "__main__":
    unittest.main()
