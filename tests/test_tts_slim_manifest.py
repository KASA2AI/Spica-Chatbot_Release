"""TTS slim runtime manifest + planner logic (LOCAL_RUNTIME_PLAN B1 step1, CI-pure).

Synthetic fake source trees / file lists only -- NO real GPT-SoVITS, model, GPU,
torch or transformers. Reads the REAL manifest to validate its schema, then
exercises keep/exclude matching, bloat guards, path-escape safety, symlink
no-follow, the gitignore check, the relocatable character config, and the build
report schema.
"""

import hashlib
import tempfile
import unittest
from pathlib import Path

import pytest

from spica.local_runtime.tts.slim_manifest import (
    assemble_build_report,
    build_character_config,
    character_reference_files,
    collect_files,
    is_safe_rel,
    is_within,
    license_status,
    load_manifest,
    output_is_gitignored,
    plan_includes,
    sha256_of,
    should_include,
    validate_manifest,
    within_size_cap,
)

REAL_MANIFEST = Path(__file__).resolve().parents[1] / "data" / "config" / "tts_slim_manifest.yaml"


class RealManifestTest(unittest.TestCase):
    def setUp(self):
        self.m = load_manifest(REAL_MANIFEST)

    def test_real_manifest_parses_and_validates(self):
        validate_manifest(self.m)  # must not raise

    def test_language_profile_is_ja_only(self):
        self.assertEqual(self.m["language_profile"], "ja_only")

    def test_character_pack_schema(self):
        spcia = self.m["character_packs"]["spcia"]
        self.assertEqual(spcia["version"], "v2ProPlus")
        for key in ("gpt_weight", "sovits_weight", "config_source"):
            self.assertIn(key, spcia)

    def test_writable_paths_has_weight_json_p0(self):
        wj = [w for w in self.m["writable_paths"] if w["path"] == "./weight.json"]
        self.assertTrue(wj)
        self.assertEqual(wj[0]["risk"], "P0")

    def test_licenses_parse(self):
        self.assertIn("LICENSE*", self.m["licenses"]["keep"])
        self.assertIn(
            "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large",
            self.m["licenses"]["expect_license_for"],
        )

    def test_output_must_be_gitignored_flag(self):
        self.assertIs(self.m["output"]["must_be_gitignored"], True)

    def test_invalid_manifest_raises_with_all_problems(self):
        with self.assertRaises(ValueError):
            validate_manifest({"version": 1})  # missing everything else


class KeepExcludeTest(unittest.TestCase):
    def setUp(self):
        self.m = load_manifest(REAL_MANIFEST)

    def test_inference_files_included(self):
        keep = [
            "config.py",
            "GPT_SoVITS/inference_webui.py",          # top-level .py
            "GPT_SoVITS/module/models.py",            # nested .py
            "GPT_SoVITS/text/ja_userdic/userdict.csv",
            "GPT_SoVITS/text/opencpop-strict.txt",
            "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large/pytorch_model.bin",
            "GPT_SoVITS/pretrained_models/chinese-hubert-base/config.json",
            "GPT_SoVITS/pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt",
            "GPT_SoVITS/pretrained_models/fast_langdetect/lid.176.bin",
            "tools/i18n/i18n.py",
        ]
        plan = plan_includes(keep, self.m)
        self.assertEqual(plan["excluded"], [], f"unexpectedly excluded: {plan['excluded']}")

    def test_bloat_excluded(self):
        bloat = [
            "logs/spcia/checkpoint.ckpt",
            "runtime/python.exe",
            "tools/asr/model.bin",
            "tools/uvr5/weights.pth",
            "tools/AP_BWE_main/x.pth",
            "GPT_SoVITS/pretrained_models/gsv-v4-pretrained/s2Gv4.pth",
            "GPT_SoVITS/pretrained_models/models--nvidia--bigvgan_v2_24khz/x.bin",
            "GPT_SoVITS/pretrained_models/gsv-v2final-pretrained/s2G2333k.pth",
            "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth",   # base, not loaded
            "GPT_SoVITS/pretrained_models/s1v3.ckpt",
            "GPT_SoVITS/pretrained_models/s2D488k.pth",
            "GPT_SoVITS/text/G2PWModel_1.1.zip",
            "GPT_SoVITS/text/cmudict.rep",
            "webui.py",
            "api_v2.py",
            "Colab-Inference.ipynb",
            "GPT_SoVITS/module/__pycache__/models.cpython-311.pyc",
            "SoVITS_weights_v2ProPlus/spcia_e12_s1932.pth",  # character weight -> NOT in base
            "GPT_weights_v2ProPlus/spcia-e25.ckpt",
        ]
        plan = plan_includes(bloat, self.m)
        self.assertEqual(plan["included"], [], f"bloat leaked into base: {plan['included']}")

    def test_exclude_wins_over_keep(self):
        # a path matching BOTH keep and exclude must be EXCLUDED.
        self.assertFalse(should_include("a/secret/x", keep=["a/**"], exclude=["a/secret/**"]))
        self.assertTrue(should_include("a/ok/x", keep=["a/**"], exclude=["a/secret/**"]))

    def test_glob_segment_semantics(self):
        # * stays within a segment; ** spans segments.
        self.assertTrue(should_include("a/b.py", keep=["a/*.py"], exclude=[]))
        self.assertFalse(should_include("a/b/c.py", keep=["a/*.py"], exclude=[]))  # * not across /
        self.assertTrue(should_include("a/b/c.py", keep=["a/**/*.py"], exclude=[]))


class PathSafetyTest(unittest.TestCase):
    def test_safe_rel_rejects_escape(self):
        self.assertTrue(is_safe_rel("GPT_SoVITS/x.py"))
        self.assertFalse(is_safe_rel("../etc/passwd"))
        self.assertFalse(is_safe_rel("/abs/path"))
        self.assertFalse(is_safe_rel("a/../../b"))
        self.assertFalse(is_safe_rel("a\x00b"))  # embedded NUL

    def test_safe_rel_rejects_windows_and_unc(self):
        for bad in (
            r"C:\windows\system32",   # drive-absolute, backslash
            "C:/windows",             # drive-absolute, forward slash
            "c:relative",             # drive-relative
            "D:\\x",                  # another drive
            r"\\server\share\x",      # UNC, backslash
            "//server/share/x",       # UNC, forward slash
        ):
            self.assertFalse(is_safe_rel(bad), bad)
        # a colon NOT at the drive position is a legal POSIX filename -> allowed.
        self.assertTrue(is_safe_rel("dir/file:name.txt"))
        self.assertTrue(is_safe_rel("GPT_SoVITS/text/ja_userdic/userdict.csv"))

    def test_within_containment(self):
        self.assertTrue(is_within("/out/base/x", "/out"))
        self.assertFalse(is_within("/out/../etc/passwd", "/out"))  # normpath escapes
        self.assertFalse(is_within("/other/x", "/out"))

    def test_size_cap_guard(self):
        self.assertTrue(within_size_cap(2 * 1024 ** 3, 3.0))
        self.assertFalse(within_size_cap(4 * 1024 ** 3, 3.0))

    def test_symlinks_not_followed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "src"
            (root / "sub").mkdir(parents=True)
            (root / "real.txt").write_text("x")
            (root / "sub" / "a.py").write_text("p")
            outside_file = Path(d) / "outside.txt"
            outside_file.write_text("secret")
            (root / "link.txt").symlink_to(outside_file)        # symlinked FILE
            outside_dir = Path(d) / "outdir"
            outside_dir.mkdir()
            (outside_dir / "b.txt").write_text("q")
            (root / "linkdir").symlink_to(outside_dir)          # symlinked DIR

            files = collect_files(str(root), follow_symlinks=False)
            self.assertIn("real.txt", files)
            self.assertIn("sub/a.py", files)
            self.assertNotIn("link.txt", files)                 # symlink file skipped
            self.assertFalse(any(f.startswith("linkdir/") for f in files))  # symlink dir not descended

    def test_output_gitignored_check_is_mockable(self):
        self.assertTrue(output_is_gitignored("artifacts/tts_slim", lambda p: True))
        self.assertFalse(output_is_gitignored("spica/x", lambda p: False))

    def test_sha256_of_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.bin"
            p.write_bytes(b"hello slim")
            self.assertEqual(sha256_of(str(p)), hashlib.sha256(b"hello slim").hexdigest())


FAKE_TTS = {
    "ref_language": "日文",
    "target_language": "日文",
    "emotions": {
        "happy": {
            "ref_audio_path": "../../spica_data/voice/happy/あそこ.wav",
            "prompt_text_path": "../../spica_data/voice/happy/prompt.txt",
            "ref_language": "日文",
        },
        "angry": {
            "ref_audio_path": "/home/san/ai_code/Spica-Chatbot/spica_data/voice/angry/x.wav",
            "prompt_text": "怒ってる",
        },
    },
}
FAKE_PACK = {
    "version": "v2ProPlus",
    "gpt_weight": "GPT_weights_v2ProPlus/spcia-e25.ckpt",
    "sovits_weight": "SoVITS_weights_v2ProPlus/spcia_e12_s1932.pth",
    "config_source": "data/config/tts.yaml",
}


class CharacterPackTest(unittest.TestCase):
    def test_character_config_paths_are_pack_relative(self):
        cfg = build_character_config(FAKE_PACK, FAKE_TTS)
        self.assertEqual(cfg["gpt_model_path"], "GPT_weights/spcia-e25.ckpt")
        self.assertEqual(cfg["sovits_model_path"], "SoVITS_weights/spcia_e12_s1932.pth")
        self.assertEqual(cfg["emotions"]["happy"]["ref_audio_path"], "reference/happy/あそこ.wav")
        self.assertEqual(cfg["emotions"]["happy"]["prompt_text_path"], "reference/happy/prompt.txt")
        self.assertEqual(cfg["emotions"]["angry"]["ref_audio_path"], "reference/angry/x.wav")
        self.assertEqual(cfg["emotions"]["angry"]["prompt_text"], "怒ってる")

    def test_character_config_has_no_dev_machine_paths(self):
        import json

        blob = json.dumps(build_character_config(FAKE_PACK, FAKE_TTS), ensure_ascii=False)
        self.assertNotIn("/home", blob)
        self.assertNotIn("../", blob)
        self.assertNotIn("spica_data", blob)

    def test_character_reference_files_to_copy(self):
        refs = dict(character_reference_files(FAKE_TTS))
        self.assertEqual(refs["../../spica_data/voice/happy/あそこ.wav"], "reference/happy/あそこ.wav")
        self.assertEqual(refs["../../spica_data/voice/happy/prompt.txt"], "reference/happy/prompt.txt")
        self.assertIn("/home/san/ai_code/Spica-Chatbot/spica_data/voice/angry/x.wav", refs)


class BuildReportTest(unittest.TestCase):
    def test_license_status(self):
        status = license_status(
            ["a/bert", "a/sv"], ["a/bert/LICENSE", "a/bert/README.md"]
        )
        self.assertEqual(status["copied"], ["a/bert"])
        self.assertEqual(status["missing"], ["a/sv"])

    def test_build_report_schema(self):
        manifest = load_manifest(REAL_MANIFEST)
        files = [
            {
                "category": "character_gpt",
                "source": "/v/GPT_weights_v2ProPlus/spcia-e25.ckpt",
                "target": "characters/spcia/GPT_weights/spcia-e25.ckpt",
                "size_bytes": 162703200,
                "sha256": "abc123",
            }
        ]
        report = assemble_build_report(
            manifest=manifest,
            character="spcia",
            files=files,
            licenses={"copied": ["a/bert"], "missing": ["a/sv"]},
            totals={"total_bytes": 162703200},
        )
        self.assertEqual(report["parity"], "PENDING")
        self.assertEqual(report["language_profile"], "ja_only")
        self.assertEqual(report["files"][0]["sha256"], "abc123")
        self.assertIn("category", report["files"][0])
        self.assertEqual(report["licenses"]["missing"], ["a/sv"])
        self.assertTrue(any("weight.json" in w and "(P0)" in w for w in report["writable_paths"]))


if __name__ == "__main__":
    unittest.main()
