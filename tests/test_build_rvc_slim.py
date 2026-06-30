"""DRY-RUN planner for the RVC (Applio) slim build (RVC Slim Step1, CI-pure).

Exercises ``scripts.local_runtime.build_rvc_slim.plan_build`` against a SYNTHETIC fake
Applio tree -- NO real Applio / GPU / torch / faiss / RVC model, NO real copy (dry-run
only). Covers the would-copy whitelist, training/scratch exclusion, the character pack,
the required/loud-failure guards, realpath containment, the gitignore gate, and the
no-output-dir-created invariant.
"""

import copy
import tempfile
import unittest
from pathlib import Path

from scripts.local_runtime.build_rvc_slim import BuildAbort, plan_build
from spica.local_runtime.rvc.slim_manifest import load_manifest

REAL_MANIFEST = Path(__file__).resolve().parents[1] / "data" / "config" / "rvc_slim_manifest.yaml"

# Fake Applio tree: load-bearing (kept) + training/scratch/webui (dropped) + character.
KEEP_FILES = {
    "core.py": b"# core\n",
    "rvc/configs/config.py": b"# cfg\n",
    "rvc/configs/24000.json": b"{}\n",                 # for the rvc/configs/*.json keep glob
    "rvc/infer/infer.py": b"# infer\n",
    "rvc/infer/pipeline.py": b"# pipe\n",
    "rvc/lib/utils.py": b"# utils\n",
    "rvc/lib/algorithm/synthesizers.py": b"# synth\n",
    "rvc/lib/predictors/RMVPE.py": b"# rmvpe mod\n",
    "rvc/lib/predictors/f0.py": b"# f0\n",
    "rvc/lib/tools/analyzer.py": b"# analyzer\n",
    "rvc/lib/tools/tts_voices.json": b"[]\n",           # for the tts_voices.json keep glob
    "rvc/train/process/model_blender.py": b"# blender\n",      # train dir, but must-keep
    "rvc/train/process/model_information.py": b"# info\n",      # train dir, but must-keep
    "rvc/models/embedders/contentvec/pytorch_model.bin": b"C" * 200,
    "rvc/models/embedders/contentvec/config.json": b"{}\n",
    "rvc/models/predictors/rmvpe.pt": b"R" * 150,
}
DROP_FILES = {
    "rvc/train/train.py": b"# training\n",                # not in keep -> dropped
    "rvc/train/process/extract_model.py": b"# extract\n",  # not in keep -> dropped
    "logs/spica/G_2333333.pth": b"G" * 500,
    "logs/spica/D_2333333.pth": b"D" * 500,
    "logs/spica/sliced_audios/a.wav": b"W" * 500,
    "rvc/models/predictors/fcpe.pt": b"F" * 100,          # only rmvpe kept
    "rvc/models/pretraineds/hifigan/model.pth": b"P" * 500,
    "tabs/infer.py": b"# webui\n",
    "app.py": b"# app\n",
    "rvc/lib/__pycache__/utils.cpython-311.pyc": b"x",    # excluded via **/__pycache__/**
}
CHARACTER_FILES = {
    "logs/spica/spica_200e_57000s.pth": b"M" * 300,       # character model (not in keep -> pack pulls it)
    "logs/spica/spica.index": b"I" * 120,                 # character index
}


def _write(root: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


class RvcDryRunTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.src = self.repo / "Applio"
        _write(self.src, {**KEEP_FILES, **DROP_FILES, **CHARACTER_FILES})
        self.out = self.repo / "out" / "rvc_slim"   # intentionally does NOT exist
        self.manifest = load_manifest(REAL_MANIFEST)

    def tearDown(self):
        self._tmp.cleanup()

    def _plan(self, **over):
        kw = dict(source_root=str(self.src), manifest=self.manifest, output_dir=str(self.out),
                  character="spica", check_ignore=lambda p: True)
        kw.update(over)
        return plan_build(**kw)

    # -- would-copy whitelist -------------------------------------------------
    def test_must_keeps_in_plan(self):
        targets = {e["target"] for e in self._plan()["would_copy"]}
        for good in (
            "base/core.py", "base/rvc/configs/config.py", "base/rvc/configs/24000.json",
            "base/rvc/infer/infer.py", "base/rvc/lib/utils.py", "base/rvc/lib/algorithm/synthesizers.py",
            "base/rvc/lib/predictors/RMVPE.py", "base/rvc/lib/predictors/f0.py",
            "base/rvc/train/process/model_blender.py", "base/rvc/train/process/model_information.py",
            "base/rvc/models/embedders/contentvec/pytorch_model.bin",
            "base/rvc/models/predictors/rmvpe.pt",
        ):
            self.assertIn(good, targets)

    def test_training_scratch_webui_dropped(self):
        targets = {e["target"] for e in self._plan()["would_copy"]}
        for bad in (
            "rvc/train/train.py", "rvc/train/process/extract_model.py",
            "logs/spica/G_2333333.pth", "logs/spica/D_2333333.pth", "logs/spica/sliced_audios/a.wav",
            "rvc/models/predictors/fcpe.pt", "rvc/models/pretraineds/hifigan/model.pth",
            "tabs/infer.py", "app.py",
        ):
            for prefix in ("", "base/"):
                self.assertNotIn(prefix + bad, targets)

    def test_character_model_and_index_in_plan(self):
        by_target = {e["target"]: e for e in self._plan()["would_copy"]}
        self.assertEqual(by_target["characters/spica/model/spica_200e_57000s.pth"]["category"], "character_model")
        self.assertEqual(by_target["characters/spica/index/spica.index"]["category"], "character_index")
        # the character weight is NOT also a base file
        self.assertNotIn("base/logs/spica/spica_200e_57000s.pth", by_target)

    def test_categories_and_pending(self):
        report = self._plan()
        cats = set(report["category_sizes"])
        for c in ("runtime_python", "runtime_model_embedder", "runtime_model_pitch",
                  "config", "character_model", "character_index"):
            self.assertIn(c, cats)
        self.assertEqual(report["parity"]["status"], "PENDING")
        self.assertEqual(report["import_preflight"]["status"], "PENDING")
        self.assertEqual(report["character"], "spica")
        self.assertEqual(report["totals"]["required_missing"], [])
        self.assertIn("total_gb", report["totals"])
        self.assertTrue(report["totals"]["within_cap"])

    # -- loud failure ---------------------------------------------------------
    def test_required_missing_aborts(self):
        (self.src / "rvc/infer/infer.py").unlink()   # required
        with self.assertRaises(BuildAbort):
            self._plan()

    def test_required_shadowed_by_exclude_aborts(self):
        m = copy.deepcopy(self.manifest)
        m["runtime_base"]["exclude"].append("rvc/train/**")   # would shadow model_blender (required)
        with self.assertRaises(BuildAbort):
            self._plan(manifest=m)

    def test_keep_glob_matches_nothing_aborts(self):
        # remove the only file a keep glob matches -> unmatched keep glob.
        (self.src / "rvc/lib/tools/tts_voices.json").unlink()
        with self.assertRaises(BuildAbort):
            self._plan()

    def test_character_model_missing_aborts(self):
        (self.src / "logs/spica/spica_200e_57000s.pth").unlink()
        with self.assertRaises(BuildAbort):
            self._plan()

    # -- safety guards --------------------------------------------------------
    def test_output_not_gitignored_aborts(self):
        with self.assertRaises(BuildAbort):
            self._plan(check_ignore=lambda p: False)

    def test_realpath_containment_aborts(self):
        # output that, after realpath, lands inside the source via a symlink -> abort.
        link = self.repo / "sneaky"
        link.symlink_to(self.src)
        with self.assertRaises(BuildAbort):
            self._plan(output_dir=str(link / "slim"))
        # source inside output -> abort
        with self.assertRaises(BuildAbort):
            self._plan(output_dir=str(self.src.parent))

    def test_invalid_manifest_raises(self):
        with self.assertRaises(ValueError):
            self._plan(manifest={"schema_version": 1})

    def test_dry_run_creates_no_output(self):
        self.assertFalse(self.out.exists())
        self._plan()
        self.assertFalse(self.out.exists())
        self.assertFalse((self.repo / "out").exists())


if __name__ == "__main__":
    unittest.main()
