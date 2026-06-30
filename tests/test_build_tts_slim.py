"""DRY-RUN planner for the TTS slim build (LOCAL_RUNTIME_PLAN B1 step2, CI-pure).

Exercises ``scripts.local_runtime.build_tts_slim.plan_build`` against a SYNTHETIC
fake vendored tree + synthetic tts.yaml dict -- NO real GPT-SoVITS, model, GPU,
torch or transformers, and NO real copy (the planner is dry-run only). Covers the
would-copy list, bloat exclusion, the character pack (weights + ref wav/prompt),
the size cap, the gitignore gate, source/target realpath containment, target
escape rejection, and the no-output-dir-created invariant.
"""

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.local_runtime.build_tts_slim import BuildAbort, execute_build, plan_build
from spica.local_runtime.tts.slim_manifest import load_manifest

REAL_MANIFEST = Path(__file__).resolve().parents[1] / "data" / "config" / "tts_slim_manifest.yaml"

# Files at the manifest's real keep paths (so the real keep/exclude globs apply).
BASE_FILES = {
    "config.py": b"x=1\n",
    "GPT_SoVITS/inference_webui.py": b"# infer\n",
    "GPT_SoVITS/module/models.py": b"# models\n",
    "GPT_SoVITS/text/opencpop-strict.txt": b"strict\n",
    "GPT_SoVITS/text/ja_userdic/userdict.csv": b"a,b\n",
    "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large/pytorch_model.bin": b"R" * 100,
    "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large/LICENSE": b"MIT\n",
    "GPT_SoVITS/pretrained_models/chinese-hubert-base/config.json": b"{}\n",
    "GPT_SoVITS/pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt": b"S" * 50,
    "GPT_SoVITS/pretrained_models/fast_langdetect/lid.176.bin": b"L" * 50,
    "tools/i18n/i18n.py": b"# i18n\n",
    "LICENSE": b"top-level license\n",
}
BLOAT_FILES = {
    "logs/spcia/checkpoint.ckpt": b"B" * 1000,
    "runtime/python.exe": b"B" * 1000,
    "tools/asr/model.bin": b"B" * 1000,
    "tools/uvr5/weights.pth": b"B" * 1000,
    "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth": b"B" * 1000,
    "GPT_SoVITS/pretrained_models/s1v3.ckpt": b"B" * 1000,
    "webui.py": b"# webui\n",
    "api_v2.py": b"# api\n",
    "x.ipynb": b"{}\n",
    "GPT_SoVITS/module/__pycache__/models.cpython-311.pyc": b"B" * 100,
}
WEIGHT_FILES = {
    "GPT_weights_v2ProPlus/spcia-e25.ckpt": b"G" * 200,
    "SoVITS_weights_v2ProPlus/spcia_e12_s1932.pth": b"V" * 200,
}

FAKE_TTS = {
    "ref_language": "日文",
    "target_language": "日文",
    "emotions": {
        "happy": {
            "ref_audio_path": "../../spica_data/voice/happy/happy.wav",
            "prompt_text_path": "../../spica_data/voice/happy/prompt.txt",
            "inp_refs_path": "../../spica_data/voice/happy/refs",
        },
        "angry": {
            "ref_audio_path": "../../spica_data/voice/angry/angry.wav",
            "prompt_text": "怒ってる",
        },
    },
}


def _write_tree(root: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


class _SlimFixture(unittest.TestCase):
    """Synthetic fake vendored tree + spica_data refs. Shared by the dry-run and
    real-build suites (no test_ methods here, so it contributes no tests itself)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.src = self.repo / "vendored"
        _write_tree(self.src, {**BASE_FILES, **BLOAT_FILES, **WEIGHT_FILES})
        # ref wav/prompt live under repo/spica_data so the "../../spica_data/..."
        # paths resolve relative to config_dir (= repo/data/config), like production.
        for emo in ("happy", "angry"):
            d = self.repo / "spica_data" / "voice" / emo
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{emo}.wav").write_bytes(b"W" * 80)
            (d / "prompt.txt").write_bytes(b"prompt\n")
        # happy declares inp_refs_path -> populate its dedicated refs/ subdir with wavs
        # (FAKE_TTS gives only happy an inp_refs_path; angry has none).
        happy_refs = self.repo / "spica_data" / "voice" / "happy" / "refs"
        happy_refs.mkdir(parents=True, exist_ok=True)
        for i in range(4):
            (happy_refs / f"r{i}.wav").write_bytes(b"I" * (120 + i))
        self.config_dir = self.repo / "data" / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.out = self.repo / "out" / "tts_slim"  # intentionally does NOT exist
        self.manifest = load_manifest(REAL_MANIFEST)

    def tearDown(self):
        self._tmp.cleanup()

    def _plan(self, **over):
        kw = dict(
            source_root=str(self.src),
            manifest=self.manifest,
            tts_yaml=FAKE_TTS,
            config_dir=str(self.config_dir),
            output_dir=str(self.out),
            character="spcia",
            check_ignore=lambda p: True,
        )
        kw.update(over)
        return plan_build(**kw)

    def _build(self, **over):
        kw = dict(
            source_root=str(self.src), manifest=self.manifest, tts_yaml=FAKE_TTS,
            config_dir=str(self.config_dir), output_dir=str(self.out),
            character="spcia", check_ignore=lambda p: True,
        )
        kw.update(over)
        return execute_build(**kw)

    def _staging_leftovers(self):
        parent = self.repo / "out"
        return list(parent.glob(".tts_slim.staging-*")) if parent.exists() else []


class DryRunPlanTest(_SlimFixture):
    # -- would-copy list ------------------------------------------------------
    def test_would_copy_includes_base_keep(self):
        targets = {e["target"] for e in self._plan()["would_copy"]}
        for good in (  # base/license live under base/ in the slim layout
            "base/config.py",
            "base/GPT_SoVITS/inference_webui.py",
            "base/GPT_SoVITS/module/models.py",
            "base/tools/i18n/i18n.py",
            "base/GPT_SoVITS/text/opencpop-strict.txt",
            "base/GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large/pytorch_model.bin",
            "base/GPT_SoVITS/pretrained_models/fast_langdetect/lid.176.bin",
        ):
            self.assertIn(good, targets)
        base = [e for e in self._plan()["would_copy"] if e["category"] in ("base", "license")]
        self.assertTrue(all(e["exists"] for e in base))
        self.assertTrue(all(e["target"].startswith("base/") for e in base))

    def test_bloat_not_in_plan(self):
        targets = {e["target"] for e in self._plan()["would_copy"]}
        for bad in (
            "logs/spcia/checkpoint.ckpt",
            "runtime/python.exe",
            "tools/asr/model.bin",
            "tools/uvr5/weights.pth",
            "webui.py",
            "api_v2.py",
            "x.ipynb",
            "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth",
            "GPT_SoVITS/pretrained_models/s1v3.ckpt",
            "GPT_SoVITS/module/__pycache__/models.cpython-311.pyc",
            # weights are character-pack files, never base targets:
            "GPT_weights_v2ProPlus/spcia-e25.ckpt",
            "SoVITS_weights_v2ProPlus/spcia_e12_s1932.pth",
        ):
            self.assertNotIn(bad, targets)

    # -- character pack -------------------------------------------------------
    def test_character_pack_weights_and_refs_in_plan(self):
        by_target = {e["target"]: e for e in self._plan()["would_copy"]}
        self.assertEqual(by_target["characters/spcia/GPT_weights/spcia-e25.ckpt"]["category"], "character_gpt")
        self.assertEqual(
            by_target["characters/spcia/SoVITS_weights/spcia_e12_s1932.pth"]["category"], "character_sovits"
        )
        for ref in (
            "characters/spcia/reference/happy/happy.wav",
            "characters/spcia/reference/happy/prompt.txt",
            "characters/spcia/reference/angry/angry.wav",
        ):
            self.assertIn(ref, by_target)
            self.assertEqual(by_target[ref]["category"], "character_reference")
            self.assertTrue(by_target[ref]["exists"])  # resolved against config_dir

    def test_character_config_preview_is_portable(self):
        preview = self._plan()["character_config_preview"]
        blob = json.dumps(preview, ensure_ascii=False)
        self.assertNotIn("..", blob)
        self.assertNotIn("/home", blob)
        self.assertNotIn("spica_data", blob)
        # inp_refs_path is pack-relative (dedicated refs/ subdir).
        self.assertEqual(preview["emotions"]["happy"]["inp_refs_path"], "reference/happy/refs")

    def test_inp_refs_packed_into_pack(self):
        report = self._plan()
        by_target = {e["target"]: e for e in report["would_copy"]}
        for i in range(4):
            t = f"characters/spcia/reference/happy/refs/r{i}.wav"
            self.assertIn(t, by_target)
            self.assertEqual(by_target[t]["category"], "character_inp_refs")
            self.assertTrue(by_target[t]["exists"])
        self.assertEqual(report["inp_refs_packed"], 4)
        self.assertNotIn("unpacked_inp_refs", report)  # the deferred field is gone

    def test_inp_refs_subdir_isolation(self):
        by_target = {e["target"] for e in self._plan()["would_copy"]}
        # primary ref sits one level under reference/happy/ ; inp_refs under refs/.
        self.assertIn("characters/spcia/reference/happy/happy.wav", by_target)
        self.assertIn("characters/spcia/reference/happy/refs/r0.wav", by_target)
        # primary ref NOT inside refs/ ; inp_refs NOT at the primary level.
        self.assertNotIn("characters/spcia/reference/happy/refs/happy.wav", by_target)
        self.assertNotIn("characters/spcia/reference/happy/r0.wav", by_target)
        # glob reference/happy/refs/*.wav would hit exactly the 4 inp_refs, never primary.
        in_refs = [t for t in by_target if t.startswith("characters/spcia/reference/happy/refs/")]
        self.assertEqual(len(in_refs), 4)

    def test_inp_refs_missing_dir_is_loud_failure(self):
        tts = {"emotions": {"happy": {
            "ref_audio_path": "../../spica_data/voice/happy/happy.wav",
            "inp_refs_path": "../../spica_data/voice/happy/NOPE_REFS",  # declared but absent
        }}}
        with self.assertRaises(BuildAbort):
            self._plan(tts_yaml=tts)

    def test_inp_refs_empty_dir_is_loud_failure(self):
        empty = self.repo / "spica_data" / "voice" / "happy" / "emptyrefs"
        empty.mkdir(parents=True, exist_ok=True)  # exists but has no audio
        tts = {"emotions": {"happy": {
            "ref_audio_path": "../../spica_data/voice/happy/happy.wav",
            "inp_refs_path": "../../spica_data/voice/happy/emptyrefs",
        }}}
        with self.assertRaises(BuildAbort):
            self._plan(tts_yaml=tts)

    def test_license_status_in_report(self):
        licenses = self._plan()["licenses"]
        self.assertIn(
            "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large", licenses["copied"]
        )
        self.assertIn("GPT_SoVITS/pretrained_models/chinese-hubert-base", licenses["missing"])

    # -- loud failure: every declared + used dependency missing -> blocking --------
    def test_missing_gpt_weight_is_loud_failure(self):
        (self.src / "GPT_weights_v2ProPlus" / "spcia-e25.ckpt").unlink()
        with self.assertRaises(BuildAbort):
            self._plan()

    def test_missing_sovits_weight_is_loud_failure(self):
        (self.src / "SoVITS_weights_v2ProPlus" / "spcia_e12_s1932.pth").unlink()
        with self.assertRaises(BuildAbort):
            self._plan()

    def test_missing_primary_ref_is_loud_failure(self):
        # FLIP of the old "recorded not aborted": a missing primary ref now aborts.
        tts = {"emotions": {"happy": {"ref_audio_path": "../../spica_data/voice/happy/NOPE.wav"}}}
        with self.assertRaises(BuildAbort):
            self._plan(tts_yaml=tts)

    def test_missing_prompt_text_path_is_loud_failure(self):
        tts = {"emotions": {"happy": {
            "ref_audio_path": "../../spica_data/voice/happy/happy.wav",        # exists
            "prompt_text_path": "../../spica_data/voice/happy/NOPE_PROMPT.txt",  # absent
        }}}
        with self.assertRaises(BuildAbort):
            self._plan(tts_yaml=tts)

    def test_inline_prompt_text_needs_no_file(self):
        # ref exists + INLINE prompt_text (no prompt_text_path, no inp_refs) -> succeeds,
        # and no prompt file is required/added.
        tts = {"emotions": {"happy": {
            "ref_audio_path": "../../spica_data/voice/happy/happy.wav",
            "prompt_text": "はい。",
        }}}
        targets = {e["target"] for e in self._plan(tts_yaml=tts)["would_copy"]}
        self.assertIn("characters/spcia/reference/happy/happy.wav", targets)
        self.assertFalse(any(t.endswith("/prompt.txt") for t in targets))

    def test_base_required_glob_missing_is_loud_failure(self):
        # remove a load-path-confirmed base asset -> its keep glob matches nothing.
        (self.src / "GPT_SoVITS" / "pretrained_models" / "fast_langdetect" / "lid.176.bin").unlink()
        with self.assertRaises(BuildAbort):
            self._plan()

    def test_license_missing_is_warning_not_blocking(self):
        # the fake tree has a LICENSE only under chinese-roberta; hubert/sv/fast_langdetect
        # have none -> licenses.missing is populated but the plan still SUCCEEDS.
        report = self._plan()
        self.assertTrue(report["licenses"]["missing"])  # warning present
        self.assertTrue(report["dry_run"])              # not blocking
        self.assertNotIn("missing_sources", report)     # the silent-defer field is gone

    # -- guards ---------------------------------------------------------------
    def test_size_cap_aborts(self):
        tiny = copy.deepcopy(self.manifest)
        tiny["output"]["size_cap_gb"] = 1e-9  # ~1 byte -> any real file exceeds it
        with self.assertRaises(BuildAbort):
            self._plan(manifest=tiny)

    def test_output_must_be_gitignored(self):
        with self.assertRaises(BuildAbort):
            self._plan(check_ignore=lambda p: False)
        # mockable pass-through:
        self.assertTrue(self._plan(check_ignore=lambda p: True)["dry_run"])

    def test_invalid_manifest_raises(self):
        with self.assertRaises(ValueError):
            self._plan(manifest={"version": 1})

    def test_source_target_realpath_containment(self):
        # (a) output dir that, AFTER realpath, lands inside the source tree via a
        #     symlink -> must abort. normpath alone would not catch this; realpath does.
        link = self.repo / "sneaky"
        link.symlink_to(self.src)            # sneaky -> vendored
        with self.assertRaises(BuildAbort):
            self._plan(output_dir=str(link / "slim"))  # realpath -> vendored/slim
        # (b) source inside output -> must abort.
        with self.assertRaises(BuildAbort):
            self._plan(output_dir=str(self.src.parent))  # output = repo, src under it

    def test_target_escape_rejected(self):
        # a malicious emotion key would build a "reference/../escape/..." target.
        evil = {"emotions": {"../escape": {"ref_audio_path": "../../spica_data/voice/happy/happy.wav"}}}
        with self.assertRaises(BuildAbort):
            self._plan(tts_yaml=evil)

    def test_dry_run_creates_no_output_dir(self):
        self.assertFalse(self.out.exists())
        self.assertFalse((self.repo / "out").exists())
        self._plan()
        self.assertFalse(self.out.exists())          # output dir not created
        self.assertFalse((self.repo / "out").exists())  # nor its parent


class ExecuteBuildTest(_SlimFixture):
    """Real copy into a SYNTHETIC out dir (no real models). Reuses the fake tree."""

    def test_execute_build_materializes_pack(self):
        report, out = self._build()
        out = Path(out)
        self.assertTrue(out.is_dir())
        # base/license under base/ ; character pack under characters/spcia/
        self.assertTrue((out / "base" / "config.py").is_file())
        self.assertTrue((out / "base" / "GPT_SoVITS" / "inference_webui.py").is_file())
        self.assertTrue((out / "characters" / "spcia" / "GPT_weights" / "spcia-e25.ckpt").is_file())
        self.assertTrue((out / "characters" / "spcia" / "SoVITS_weights" / "spcia_e12_s1932.pth").is_file())
        self.assertTrue((out / "characters" / "spcia" / "reference" / "happy" / "happy.wav").is_file())
        self.assertTrue((out / "characters" / "spcia" / "reference" / "happy" / "refs" / "r0.wav").is_file())
        # generated, self-contained
        self.assertTrue((out / "characters" / "spcia" / "character.yaml").is_file())
        self.assertTrue((out / "build_report.json").is_file())
        self.assertEqual(report["inp_refs_packed"], 4)
        self.assertEqual(self._staging_leftovers(), [])  # staging consumed by the rename

    def test_execute_build_sha256_matches_copied_files(self):
        report, out = self._build()
        out = Path(out)
        self.assertTrue(report["files"])
        for f in report["files"]:
            data = (out / f["target"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), f["sha256"], f["target"])
            self.assertEqual(len(data), f["size_bytes"], f["target"])

    def test_execute_build_character_yaml_is_relocatable(self):
        _, out = self._build()
        import yaml
        cfg = yaml.safe_load((Path(out) / "characters" / "spcia" / "character.yaml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["emotions"]["happy"]["inp_refs_path"], "reference/happy/refs")
        blob = json.dumps(cfg, ensure_ascii=False)
        for forbidden in ("/home", "spica_data", "..", str(self.repo)):
            self.assertNotIn(forbidden, blob)

    def test_execute_build_refuses_existing_output(self):
        self.out.mkdir(parents=True)  # pre-existing -> must refuse to clobber
        with self.assertRaises(BuildAbort):
            self._build()

    def test_execute_build_no_partial_on_missing_dependency(self):
        # missing weight -> plan aborts BEFORE any copy -> no output, no staging.
        (self.src / "GPT_weights_v2ProPlus" / "spcia-e25.ckpt").unlink()
        with self.assertRaises(BuildAbort):
            self._build()
        self.assertFalse(self.out.exists())
        self.assertEqual(self._staging_leftovers(), [])

    def test_execute_build_rolls_back_on_copy_error(self):
        # force a failure partway through copying -> staging removed, no partial output.
        real_copy = shutil.copy2
        state = {"n": 0}

        def boom(src, dst, *a, **k):
            state["n"] += 1
            if state["n"] == 3:
                raise OSError("simulated copy failure")
            return real_copy(src, dst, *a, **k)

        with patch("scripts.local_runtime.build_tts_slim.shutil.copy2", side_effect=boom):
            with self.assertRaises(OSError):
                self._build()
        self.assertFalse(self.out.exists())           # final never published
        self.assertEqual(self._staging_leftovers(), [])  # rollback cleaned staging


if __name__ == "__main__":
    unittest.main()
