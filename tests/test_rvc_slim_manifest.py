"""RVC slim manifest + pure planning logic (RVC Slim Step1, CI-pure).

Synthetic only -- NO real Applio / GPU / torch / faiss / RVC model. Reads the REAL
manifest to validate its schema + self-consistency (required covered by keep, not
shadowed by exclude), then exercises the required/loud-failure helpers, categorization,
report assembly, and the inherited path-safety helper.
"""

import unittest
from pathlib import Path

from spica.local_runtime.rvc.slim_manifest import (
    assemble_report,
    categorize,
    excluded_required,
    load_manifest,
    missing_required,
    uncovered_required_by_keep,
    validate_manifest,
)
from spica.local_runtime.tts.slim_manifest import is_safe_rel  # the shared safety helper the planner uses

REAL_MANIFEST = Path(__file__).resolve().parents[1] / "data" / "config" / "rvc_slim_manifest.yaml"


class RealManifestTest(unittest.TestCase):
    def setUp(self):
        self.m = load_manifest(REAL_MANIFEST)

    def test_parses_and_validates(self):
        validate_manifest(self.m)  # must not raise

    def test_schema_fields(self):
        self.assertEqual(self.m["schema_version"], 1)
        self.assertEqual(self.m["source"]["applio_root"], "agent_tools/function_tools/song/Applio")
        self.assertIs(self.m["output"]["gitignored"], True)
        self.assertIn("size_cap_gb", self.m["output"])
        spica = self.m["character_packs"]["spica"]
        self.assertIn("model", spica)
        self.assertIn("index", spica)
        self.assertTrue(self.m["required"])
        self.assertEqual(self.m["import_preflight"]["status"], "PENDING")
        self.assertEqual(self.m["parity"]["status"], "PENDING")

    def test_required_is_self_consistent(self):
        # every required path is covered by keep AND not shadowed by exclude.
        keep = self.m["runtime_base"]["keep"]
        exclude = self.m["runtime_base"]["exclude"]
        self.assertEqual(uncovered_required_by_keep(self.m["required"], keep), [])
        self.assertEqual(excluded_required(self.m["required"], exclude), [])

    def test_train_process_must_keeps_present(self):
        # the TTS-B1 trap: train-dir files pulled by module-level import must be required.
        self.assertIn("rvc/train/process/model_blender.py", self.m["required"])
        self.assertIn("rvc/train/process/model_information.py", self.m["required"])

    def test_invalid_manifest_raises(self):
        with self.assertRaises(ValueError):
            validate_manifest({"schema_version": 1})  # missing everything else


class RequiredCheckTest(unittest.TestCase):
    def test_uncovered_required_by_keep(self):
        req = ["core.py", "rvc/infer/infer.py", "rvc/lib/utils.py"]
        keep = ["core.py", "rvc/infer/**/*.py"]  # missing rvc/lib coverage
        self.assertEqual(uncovered_required_by_keep(req, keep), ["rvc/lib/utils.py"])
        self.assertEqual(uncovered_required_by_keep(req, keep + ["rvc/lib/**/*.py"]), [])

    def test_excluded_required(self):
        # a broad exclude shadowing a must-keep train file -> conflict.
        req = ["rvc/train/process/model_blender.py"]
        self.assertEqual(excluded_required(req, ["rvc/train/**"]), ["rvc/train/process/model_blender.py"])
        self.assertEqual(excluded_required(req, ["**/__pycache__/**"]), [])

    def test_missing_required(self):
        req = ["core.py", "rvc/infer/infer.py"]
        self.assertEqual(missing_required(req, ["core.py"]), ["rvc/infer/infer.py"])
        self.assertEqual(missing_required(req, ["core.py", "rvc/infer/infer.py"]), [])


class CategorizeTest(unittest.TestCase):
    def test_categories(self):
        self.assertEqual(categorize("core.py"), "runtime_python")
        self.assertEqual(categorize("rvc/lib/predictors/f0.py"), "runtime_python")
        self.assertEqual(categorize("rvc/models/embedders/contentvec/pytorch_model.bin"), "runtime_model_embedder")
        self.assertEqual(categorize("rvc/models/embedders/contentvec/config.json"), "runtime_model_embedder")
        self.assertEqual(categorize("rvc/models/predictors/rmvpe.pt"), "runtime_model_pitch")
        self.assertEqual(categorize("rvc/configs/24000.json"), "config")
        self.assertEqual(categorize("rvc/lib/tools/tts_voices.json"), "config")
        self.assertEqual(categorize("LICENSE"), "license")
        self.assertEqual(categorize("some/data.bin"), "other")


class ReportTest(unittest.TestCase):
    def test_assemble_report_pending_and_categories(self):
        m = load_manifest(REAL_MANIFEST)
        files = [
            {"category": "runtime_python", "source": "/a/core.py", "target": "base/core.py", "size_bytes": 100},
            {"category": "character_model", "source": "/a/m.pth", "target": "characters/spica/model/m.pth", "size_bytes": 50},
        ]
        report = assemble_report(
            manifest=m, character="spica", source_root="/a", output_dir="/out",
            would_copy=files, totals={"file_count": 2, "total_bytes": 150, "total_gb": 0.0,
                                      "size_cap_gb": 1.5, "within_cap": True, "required_missing": []},
        )
        self.assertIs(report["dry_run"], True)
        self.assertEqual(report["parity"]["status"], "PENDING")
        self.assertEqual(report["import_preflight"]["status"], "PENDING")
        self.assertEqual(report["character"], "spica")
        self.assertEqual(report["category_sizes"]["runtime_python"]["files"], 1)
        self.assertEqual(report["category_sizes"]["character_model"]["bytes"], 50)


class InheritedPathSafetyTest(unittest.TestCase):
    """The RVC planner reuses the B1 is_safe_rel for target safety (Windows/UNC/escape)."""

    def test_rejects_windows_unc_escape(self):
        for bad in (r"C:\x", "C:/x", r"\\server\share", "//server/x", "/abs", "../x", "a/../../b"):
            self.assertFalse(is_safe_rel(bad), bad)
        self.assertTrue(is_safe_rel("base/rvc/infer/infer.py"))


if __name__ == "__main__":
    unittest.main()
