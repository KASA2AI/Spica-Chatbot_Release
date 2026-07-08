"""Moondream parity harness logic (cut 4, CI-pure).

Synthetic only -- NO real model / GPU / torch. Tests spec construction, the fixed
image generation, the path/import preflight wiring, and the compare verdict logic
on synthetic result JSONs (bit-identical / normalized / similar / divergent /
structural-mismatch / errors / seam-not-routed / missing).
"""

import json
import tempfile
import unittest
from pathlib import Path

from scripts.local_runtime.verify_moondream_parity import (
    build_specs,
    make_test_image,
    run_compare,
    _preflight,
)


def _result(side, provider, installed, text, **over):
    base = {
        "side": side,
        "provider": provider,
        "installed_provider": installed,
        "text": text,
        "errors": [],
        "schema": "screen_observation.v1",
        "schema_version": "screen_observation.v1",
        "type": "screen_observation",
        "visual_summary_meta": {"engine": "moondream", "model": "vikhyatk/moondream2", "revision": "2025-06-21"},
        "observation_keys": ["answer", "capture", "visual_summary"],
    }
    base.update(over)
    return base


def _legacy(text, **over):
    return _result("legacy", "moondream_local", None, text, **over)


def _hf(text, **over):
    return _result("hf", "moondream_hf", "MoondreamHfProvider", text, **over)


class BuildSpecsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.scratch = Path(self._tmp.name)
        self.legacy, self.hf = build_specs(self.scratch, seed=1234)

    def tearDown(self):
        self._tmp.cleanup()

    def test_sides_and_providers(self):
        self.assertEqual(self.legacy["provider"], "moondream_local")
        self.assertFalse(self.legacy["install_hf"])
        self.assertEqual(self.hf["provider"], "moondream_hf")
        self.assertTrue(self.hf["install_hf"])

    def test_same_image_question_seed_and_base_config(self):
        self.assertEqual(self.legacy["image"], self.hf["image"])
        self.assertEqual(self.legacy["question"], self.hf["question"])
        self.assertEqual(self.legacy["seed"], self.hf["seed"])
        # configs identical except the provider field
        lc, hc = dict(self.legacy["config"]), dict(self.hf["config"])
        self.assertEqual(lc.pop("provider"), "moondream_local")
        self.assertEqual(hc.pop("provider"), "moondream_hf")
        self.assertEqual(lc, hc)
        self.assertFalse(lc["ocr_enabled"])  # OCR disabled -> isolate Moondream
        self.assertEqual(lc["revision"], "2025-06-21")  # pinned

    def test_make_test_image_is_deterministic(self):
        a = self.scratch / "a.png"
        b = self.scratch / "b.png"
        make_test_image(a)
        make_test_image(b)
        self.assertEqual(a.read_bytes(), b.read_bytes())  # no randomness

    def test_preflight_flags_missing_image(self):
        problems = _preflight(self.legacy, self.hf)  # image not built yet
        self.assertTrue(any("missing input image" in p for p in problems))


class CompareTest(unittest.TestCase):
    def _scratch_with(self, legacy, hf):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        scratch = Path(d.name)
        if legacy is not None:
            (scratch / "result_legacy.json").write_text(json.dumps(legacy), encoding="utf-8")
        if hf is not None:
            (scratch / "result_hf.json").write_text(json.dumps(hf), encoding="utf-8")
        return scratch

    def _report(self, scratch):
        return json.loads((scratch / "parity_report.json").read_text(encoding="utf-8"))

    def test_bit_identical_passes(self):
        scratch = self._scratch_with(_legacy("A window titled Untitled."), _hf("A window titled Untitled."))
        self.assertEqual(run_compare(scratch), 0)
        rep = self._report(scratch)
        self.assertEqual(rep["verdict"], "PASS")
        self.assertEqual(rep["text_verdict"], "BIT_IDENTICAL")
        self.assertTrue(rep["seam_routed"])

    def test_normalized_identical_passes(self):
        scratch = self._scratch_with(_legacy("A Window  Titled Untitled."), _hf("a window titled untitled."))
        self.assertEqual(run_compare(scratch), 0)
        rep = self._report(scratch)
        self.assertEqual(rep["text_verdict"], "NORMALIZED_IDENTICAL")

    def test_high_similarity_passes(self):
        base = "The screen shows a document editor with a Save and Cancel button visible."
        near = "The screen shows a document editor with a Save and Cancel buttons visible."  # 1-char drift
        scratch = self._scratch_with(_legacy(base), _hf(near))
        self.assertEqual(run_compare(scratch), 0)
        rep = self._report(scratch)
        self.assertEqual(rep["text_verdict"], "SIMILAR")
        self.assertGreaterEqual(rep["similarity"], 0.98)

    def test_divergent_fails(self):
        scratch = self._scratch_with(_legacy("A red error dialog."), _hf("A green success screen with charts."))
        self.assertEqual(run_compare(scratch), 1)
        rep = self._report(scratch)
        self.assertEqual(rep["verdict"], "FAIL")
        self.assertEqual(rep["text_verdict"], "DIVERGENT")

    def test_structural_mismatch_fails(self):
        scratch = self._scratch_with(
            _legacy("same text"), _hf("same text", schema_version="screen_observation.v2")
        )
        self.assertEqual(run_compare(scratch), 1)
        rep = self._report(scratch)
        self.assertFalse(rep["structural_equal"])
        self.assertEqual(rep["verdict"], "FAIL")

    def test_errors_present_fails(self):
        scratch = self._scratch_with(
            _legacy("same text"),
            _hf("same text", errors=[{"stage": "moondream", "code": "X", "message": "boom"}]),
        )
        self.assertEqual(run_compare(scratch), 1)
        rep = self._report(scratch)
        self.assertFalse(rep["no_errors"])

    def test_seam_not_routed_fails(self):
        # hf side did not actually install the provider -> the seam never engaged.
        scratch = self._scratch_with(_legacy("same text"), _hf("same text", installed_provider=None))
        self.assertEqual(run_compare(scratch), 1)
        rep = self._report(scratch)
        self.assertFalse(rep["seam_routed"])

    def test_both_empty_fails(self):
        scratch = self._scratch_with(_legacy(""), _hf(""))
        self.assertEqual(run_compare(scratch), 1)
        rep = self._report(scratch)
        self.assertFalse(rep["both_nonempty"])

    def test_missing_output_returns_error(self):
        scratch = self._scratch_with(_legacy("x"), None)
        self.assertEqual(run_compare(scratch), 1)


if __name__ == "__main__":
    unittest.main()
