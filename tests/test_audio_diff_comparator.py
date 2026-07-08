"""TTS audio_diff comparator (LOCAL_RUNTIME_PLAN cut 2 / §6.2, A1, CI-pure).

Synthetic arrays only -- NO TTS model / GPU (§6.5). Pins: identical -> match/0;
near-identical (seeded re-driver) -> match; noise/gain/length drift -> mismatch;
int16 PCM (what get_tts_wav yields) coerced; the report-bound metrics are computed
(not the raw audio).
"""

import unittest

import numpy as np

from spica.local_runtime.parity.comparators import audio_diff, audio_metrics


def _sine(n=4000, freq=220, sr=32000):
    t = np.arange(n) / sr
    return 0.5 * np.sin(2 * np.pi * freq * t)


class AudioDiffTest(unittest.TestCase):
    def test_identical_is_match_zero(self):
        a = _sine()
        match, err = audio_diff((32000, a), (32000, a.copy()))
        self.assertTrue(match)
        self.assertAlmostEqual(err, 0.0, places=9)

    def test_near_identical_within_tolerance_matches(self):
        # a faithful seeded re-driver: tiny numerical noise -> still a match.
        a = _sine()
        b = a + 1e-5 * np.random.RandomState(0).randn(a.size)
        match, err = audio_diff((32000, a), (32000, b))
        self.assertTrue(match)
        self.assertLess(err, 1e-3)

    def test_noise_above_tolerance_is_mismatch(self):
        a = _sine()
        b = a + 0.05 * np.random.RandomState(1).randn(a.size)
        match, err = audio_diff((32000, a), (32000, b))
        self.assertFalse(match)
        self.assertGreater(err, 1e-3)

    def test_gain_change_is_mismatch(self):
        # global gain differs -> faithful parity should flag it (no per-signal rescale).
        a = _sine()
        match, err = audio_diff((32000, a), (32000, a * 0.9))
        self.assertFalse(match)
        self.assertGreater(err, 1e-3)

    def test_length_drift_is_mismatch_even_if_overlap_matches(self):
        a = _sine(n=4000)
        b = a[: int(4000 * 0.9)]  # 10% shorter -> synthesis diverged
        match, _ = audio_diff((32000, a), (32000, b))
        self.assertFalse(match)

    def test_int16_pcm_is_coerced(self):
        a = (_sine() * 32767).astype(np.int16)  # what get_tts_wav yields
        match, err = audio_diff((32000, a), (32000, a.copy()))
        self.assertTrue(match)
        self.assertAlmostEqual(err, 0.0, places=9)

    def test_metrics_include_mel_and_lengths(self):
        a, b = _sine(), _sine(freq=330)
        m = audio_metrics((32000, a), (32000, b))
        for key in ("waveform_rmse", "waveform_max", "len_old", "len_new", "len_ratio"):
            self.assertIn(key, m)
        self.assertEqual(m["len_old"], a.size)
        # librosa is a project dep -> mel metrics present for real audio shapes.
        self.assertIsNotNone(m["mel_mean_db"])
        self.assertGreater(m["mel_max_db"], 0.0)  # different pitch -> nonzero mel error

    def test_bare_array_without_sample_rate(self):
        a = _sine()
        match, err = audio_diff(a, a.copy())
        self.assertTrue(match)


if __name__ == "__main__":
    unittest.main()
