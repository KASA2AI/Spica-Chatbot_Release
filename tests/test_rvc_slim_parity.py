"""RVC slim parity harness logic (RVC Slim Step2B, CI-pure).

Synthetic only -- NO real Applio / GPU / torch / RVC model. Tests spec construction
(paths point at the right roots), the preflight, and the compare logic on synthetic
wavs (equal / length-mismatch / over-threshold / missing).
"""

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from scripts.local_runtime.verify_rvc_slim_parity import build_specs, run_compare, _preflight


class BuildSpecsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.scratch = Path(self._tmp.name)
        self.original, self.slim = build_specs(self.scratch, seed=1234)

    def tearDown(self):
        self._tmp.cleanup()

    def test_sides(self):
        self.assertEqual(self.original["side"], "original")
        self.assertEqual(self.slim["side"], "slim")

    def test_original_points_at_applio(self):
        self.assertTrue(self.original["applio_root"].endswith("song/Applio"))
        self.assertIn("song/Applio/logs/spica/spica_200e_57000s.pth", self.original["model_path"])
        self.assertIn("song/Applio/logs/spica/spica.index", self.original["index_path"])

    def test_slim_points_at_artifact(self):
        self.assertTrue(self.slim["applio_root"].endswith("artifacts/rvc_slim/base"))
        self.assertIn("artifacts/rvc_slim/characters/spica/model/spica_200e_57000s.pth", self.slim["model_path"])
        self.assertIn("artifacts/rvc_slim/characters/spica/index/spica.index", self.slim["index_path"])

    def test_same_input_and_params(self):
        self.assertEqual(self.original["input_vocal"], self.slim["input_vocal"])
        self.assertEqual(self.original["params"], self.slim["params"])
        self.assertEqual(self.slim["params"]["f0_method"], "rmvpe")
        self.assertEqual(self.original["seed"], self.slim["seed"])

    def test_preflight_flags_missing(self):
        # point both specs at non-existent paths -> preflight must flag them (the real
        # Applio + built slim exist on disk, so use fakes to test the missing branch).
        fake_o = dict(self.original, applio_root="/no/such/applio",
                      model_path="/no/such/m.pth", index_path="/no/such/i.index")
        fake_s = dict(self.slim, applio_root="/no/such/base",
                      model_path="/no/such/m.pth", index_path="/no/such/i.index")
        problems = _preflight(fake_o, fake_s)
        self.assertTrue(problems)
        self.assertTrue(any("missing" in p for p in problems))


class CompareTest(unittest.TestCase):
    def _scratch_with(self, a_old, a_new, sr=32000):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        scratch = Path(d.name)
        if a_old is not None:
            sf.write(str(scratch / "out_original.wav"), a_old, sr)
        if a_new is not None:
            sf.write(str(scratch / "out_slim.wav"), a_new, sr)
        return scratch

    def test_equal_passes_bit_identical(self):
        a = (np.sin(np.linspace(0, 50, 16000)) * 0.3).astype(np.float32)
        scratch = self._scratch_with(a, a.copy())
        rc = run_compare(scratch)
        self.assertEqual(rc, 0)
        rep = json.loads((scratch / "parity_report.json").read_text())
        self.assertEqual(rep["verdict"], "PASS")
        self.assertTrue(rep["length_equal"])
        self.assertTrue(rep["bit_identical"])

    def test_length_mismatch_fails(self):
        a = (np.sin(np.linspace(0, 50, 16000)) * 0.3).astype(np.float32)
        scratch = self._scratch_with(a, a[:8000].copy())
        self.assertEqual(run_compare(scratch), 1)
        rep = json.loads((scratch / "parity_report.json").read_text())
        self.assertEqual(rep["verdict"], "FAIL")
        self.assertFalse(rep["length_equal"])

    def test_over_threshold_fails(self):
        a = (np.sin(np.linspace(0, 50, 16000)) * 0.3).astype(np.float32)
        b = (a + 0.2).astype(np.float32)  # big RMSE, same length
        scratch = self._scratch_with(a, b)
        self.assertEqual(run_compare(scratch), 1)
        rep = json.loads((scratch / "parity_report.json").read_text())
        self.assertEqual(rep["verdict"], "FAIL")
        self.assertGreater(rep["rmse"], 1e-3)

    def test_missing_output_returns_error(self):
        a = (np.sin(np.linspace(0, 50, 16000)) * 0.3).astype(np.float32)
        scratch = self._scratch_with(a, None)  # slim output missing
        self.assertEqual(run_compare(scratch), 1)


if __name__ == "__main__":
    unittest.main()
