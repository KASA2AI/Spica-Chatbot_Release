"""scripts/self_check.py 自身的可信度测试(2026-07 review 要求).

真模型一概不加载: 这里测的是编排器本身 -- worker 结果解析、超时进程树清理、
exit code 语义、JSON 输出、secrets 不泄漏、fake-worker 注入。
"""

import importlib.util
import json
import sys
import time
import unittest
from argparse import Namespace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "spica_self_check", REPO_ROOT / "scripts" / "self_check.py"
)
self_check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(self_check)


def _fake_worker_cmd(payload: dict) -> list[str]:
    code = (
        "import json\n"
        f"print({self_check.RESULT_MARKER!r} + json.dumps({payload!r}))\n"
    )
    return [sys.executable, "-c", code]


class ExitCodeTest(unittest.TestCase):
    def test_all_pass_is_zero(self):
        results = [{"status": "PASS"}, {"status": "SKIPPED_DISABLED"}, {"status": "UNVERIFIED"}]
        self.assertEqual(self_check.exit_code_for(results), 0)

    def test_degraded_is_one(self):
        self.assertEqual(self_check.exit_code_for([{"status": "PASS"}, {"status": "DEGRADED"}]), 1)

    def test_fail_beats_degraded(self):
        results = [{"status": "DEGRADED"}, {"status": "FAIL"}]
        self.assertEqual(self_check.exit_code_for(results), 2)

    def test_unknown_status_is_never_silent_zero(self):
        # review P1: status="TYPO" previously fell through to exit 0.
        self.assertEqual(self_check.exit_code_for([{"status": "TYPO"}]), 2)


class GuardsAndEnvTest(unittest.TestCase):
    def test_running_guard_covers_the_real_entry_point(self):
        # 正式入口是 webui_qt.py(main -> ui.qt_overlay) -- 只匹配 qt_overlay 会漏掉
        # 正常启动的应用(review P1)。
        self.assertIn("webui_qt", self_check.APP_PROCESS_PATTERNS)
        self.assertIn("qt_overlay", self_check.APP_PROCESS_PATTERNS)

    def test_worker_env_default_blocks_downloads(self):
        env = self_check._worker_env(allow_downloads=False)
        self.assertEqual(env["HF_HUB_OFFLINE"], "1")
        self.assertEqual(env["TRANSFORMERS_OFFLINE"], "1")
        self.assertEqual(env["SPICA_SELF_CHECK_NO_DOWNLOAD"], "1")

    def test_allow_downloads_pops_preexisting_offline_vars(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}):
            env = self_check._worker_env(allow_downloads=True)
        self.assertNotIn("HF_HUB_OFFLINE", env)
        self.assertNotIn("TRANSFORMERS_OFFLINE", env)
        self.assertNotIn("SPICA_SELF_CHECK_NO_DOWNLOAD", env)

    def test_bad_timeout_scale_exits_3_before_spawning_anything(self):
        # nan/inf/<=0 曾在 spawn 之后才爆(泄漏 worker 且非约定 exit code)。
        for bad in ("nan", "inf", "0", "-1"):
            self.assertEqual(self_check.main(["--full", "--timeout-scale", bad]), 3, bad)

    def test_argparse_error_maps_to_exit_3_not_2(self):
        # 提交前复核: --timeout-scale nope 被 argparse 以 exit 2 拒绝, 与
        # 「2=有模型 FAIL」的约定冲突 -- 参数错误属自检自身错误(3)。
        self.assertEqual(self_check.main(["--timeout-scale", "nope"]), 3)

    def test_help_still_exits_0(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(self_check.main(["--help"]), 0)
        self.assertIn("--full", buf.getvalue())

    def test_main_first_statement_is_load_secrets(self):
        # 铁律 #10 (test_env_centralization 同款 AST 钉): 进程入口在构造任何对象
        # 之前先灌注环境。
        import ast

        tree = ast.parse((REPO_ROOT / "scripts" / "self_check.py").read_text(encoding="utf-8"))
        main_def = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        first = main_def.body[0]
        self.assertIsInstance(first, ast.Expr)
        self.assertIsInstance(first.value, ast.Call)
        self.assertEqual(getattr(first.value.func, "id", None), "load_secrets")


class GpuEvidenceRuleTest(unittest.TestCase):
    def _pass_result(self, name, detail):
        return {"name": name, "status": "PASS", "detail": detail}

    def test_zero_vram_peak_degrades_a_song_pass(self):
        result = self_check._apply_gpu_evidence_rule(
            "song_rvc", self._pass_result("song_rvc",
                                          {"configured_device": "cuda",
                                           "approx_vram_peak_mb": 0}))
        self.assertEqual(result["status"], "DEGRADED")

    def test_missing_sampling_degrades_a_song_pass(self):
        result = self_check._apply_gpu_evidence_rule(
            "song_uvr", self._pass_result("song_uvr", {}))
        self.assertEqual(result["status"], "DEGRADED")

    def test_real_vram_peak_keeps_pass(self):
        result = self_check._apply_gpu_evidence_rule(
            "song_uvr", self._pass_result("song_uvr", {"approx_vram_peak_mb": 2700}))
        self.assertEqual(result["status"], "PASS")

    def test_cpu_configured_voice_is_untouched(self):
        result = self_check._apply_gpu_evidence_rule(
            "song_rvc", self._pass_result("song_rvc",
                                          {"configured_device": "cpu",
                                           "approx_vram_peak_mb": 0}))
        self.assertEqual(result["status"], "PASS")

    def test_non_song_checks_are_untouched(self):
        result = self_check._apply_gpu_evidence_rule(
            "stt", self._pass_result("stt", {"approx_vram_peak_mb": 0}))
        self.assertEqual(result["status"], "PASS")


class WorkerStdoutParseTest(unittest.TestCase):
    def test_last_marker_line_wins_over_library_noise(self):
        stdout = "\n".join([
            "some library banner",
            self_check.RESULT_MARKER + json.dumps({"status": "FAIL"}),
            "more noise",
            self_check.RESULT_MARKER + json.dumps({"status": "PASS", "detail": {"x": 1}}),
        ])
        payload = self_check.parse_worker_stdout(stdout)
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["detail"], {"x": 1})

    def test_no_marker_returns_none(self):
        self.assertIsNone(self_check.parse_worker_stdout("no marker here"))


class SubprocessRunnerTest(unittest.TestCase):
    def test_success_payload_parsed(self):
        result = self_check.run_subprocess_check(
            _fake_worker_cmd({"status": "PASS", "detail": {"device": "cuda"}}),
            timeout_s=30, sample_vram=False,
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["detail"]["device"], "cuda")

    def test_crash_after_pass_report_is_fail(self):
        # review P1: a worker that prints PASS and then dies (rc=9) must FAIL --
        # the return code is part of the contract, not just the marker line.
        code = (
            "import json, sys\n"
            f"print({self_check.RESULT_MARKER!r} + json.dumps({{'status': 'PASS'}}))\n"
            "sys.exit(9)\n"
        )
        result = self_check.run_subprocess_check(
            [sys.executable, "-c", code], timeout_s=30, sample_vram=False,
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("rc=9", result["reason"])

    def test_invalid_status_string_is_fail(self):
        result = self_check.run_subprocess_check(
            _fake_worker_cmd({"status": "TYPO"}), timeout_s=30, sample_vram=False,
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("invalid status", result["reason"])

    def test_non_object_json_payload_is_fail_not_a_crash(self):
        # review P1: 带 marker 的合法 JSON 数组/字符串曾直接 AttributeError 崩掉
        # CLI(未约定的 exit 1)。必须变成 FAIL。
        code = (
            "print(" + repr(self_check.RESULT_MARKER) + " + '\"just a string\"')"
        )
        result = self_check.run_subprocess_check(
            [sys.executable, "-c", code], timeout_s=30, sample_vram=False,
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("not a JSON object", result["reason"])

    def test_non_object_nested_detail_is_fail_not_a_crash(self):
        # 第四轮 review: {"status":"PASS","detail":"oops"} 曾在 dict("oops") 处
        # ValueError 崩掉 CLI。嵌套结构违约同样必须是 FAIL。
        result = self_check.run_subprocess_check(
            _fake_worker_cmd({"status": "PASS", "detail": "oops"}),
            timeout_s=30, sample_vram=False,
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("detail is not a JSON object", result["reason"])

    @unittest.skipIf(sys.platform == "win32", "POSIX killpg path")
    def test_timeout_kills_descendant_processes_too(self):
        # The worker spawns a grandchild and parks; the timeout kill must take
        # down the WHOLE tree (models often live in child processes, e.g. RVC).
        import os
        from tempfile import TemporaryDirectory

        code = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "open(sys.argv[1], 'w').write(str(child.pid))\n"
            "time.sleep(60)\n"
        )
        with TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "child.pid"
            result = self_check.run_subprocess_check(
                [sys.executable, "-c", code, str(pidfile)],
                timeout_s=2.0, sample_vram=False,
            )
            self.assertEqual(result["status"], "FAIL")
            child_pid = int(pidfile.read_text())
            deadline = time.time() + 5
            alive = True
            while time.time() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.1)
            self.assertFalse(alive, f"descendant {child_pid} survived the tree kill")

    @unittest.skipIf(sys.platform == "win32", "POSIX killpg path")
    def test_parent_interrupt_kills_worker_tree_before_propagating(self):
        # review P1: worker 在独立 session 里收不到终端 SIGINT -- 父进程被 Ctrl-C
        # 打断时必须先杀掉 worker 进程树再传播, 否则 GPU worker 遗留占显存。
        import os
        import subprocess as sp
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        code = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "open(sys.argv[1], 'w').write(str(child.pid))\n"
            "time.sleep(60)\n"
        )
        with TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "child.pid"

            class InterruptingPopen(sp.Popen):
                def communicate(self, *args, **kwargs):  # noqa: ARG002
                    deadline = time.time() + 10
                    while not pidfile.exists() and time.time() < deadline:
                        time.sleep(0.05)  # 等 worker 真把孙进程拉起来
                    raise KeyboardInterrupt

            with patch.object(self_check.subprocess, "Popen", InterruptingPopen):
                with self.assertRaises(KeyboardInterrupt):
                    self_check.run_subprocess_check(
                        [sys.executable, "-c", code, str(pidfile)],
                        timeout_s=30, sample_vram=False,
                    )
            child_pid = int(pidfile.read_text())
            deadline = time.time() + 5
            alive = True
            while time.time() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.1)
            self.assertFalse(alive, f"descendant {child_pid} survived the interrupt kill")

    def test_timeout_kills_process_tree_and_fails(self):
        started = time.time()
        result = self_check.run_subprocess_check(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            timeout_s=1.0, sample_vram=False,
        )
        self.assertLess(time.time() - started, 15)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("timeout", result["reason"])

    def test_worker_crash_without_marker_fails_with_stderr_tail(self):
        result = self_check.run_subprocess_check(
            [sys.executable, "-c", "import sys; print('boom', file=sys.stderr); sys.exit(7)"],
            timeout_s=30, sample_vram=False,
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("rc=7", result["reason"])
        self.assertIn("boom", result["reason"])


class FakeWorkerInjectionTest(unittest.TestCase):
    def test_run_full_checks_uses_injected_runner_and_skips_disabled(self):
        calls: list = []

        def fake_runner(cmd, timeout, env=None):
            calls.append(cmd)
            return {"status": "PASS", "detail": {}, "reason": ""}

        args = Namespace(llm=False, all=False, allow_model_downloads=False, timeout_scale=1.0)
        results = self_check.run_full_checks(
            ["tts", "stt", "llm"], {"tts": True, "stt": False}, args, runner=fake_runner,
        )
        by_name = {r["name"]: r for r in results}
        self.assertEqual(by_name["tts"]["status"], "PASS")
        self.assertEqual(by_name["stt"]["status"], "SKIPPED_DISABLED")  # disabled -> no worker
        self.assertEqual(by_name["llm"]["status"], "UNVERIFIED")  # no --llm -> never online
        self.assertEqual(len(calls), 1)  # ONLY tts actually spawned a worker

    def test_all_flag_reaches_the_worker_via_env(self):
        # 第四轮 review: --all 只在父进程停止 skip 不够 -- tts 的开关生效在 worker
        # 内的生产装配缝里, 必须经 env 旗标穿透, 否则关掉的引擎永远查不到。
        seen_envs: list = []

        def fake_runner(cmd, timeout, env=None):
            seen_envs.append(env or {})
            return {"status": "PASS", "detail": {}, "reason": ""}

        args = Namespace(llm=False, all=True, allow_model_downloads=False, timeout_scale=1.0)
        self_check.run_full_checks(["tts"], {"tts": False}, args, runner=fake_runner)
        self.assertEqual(len(seen_envs), 1)  # --all: disabled 也真的起了 worker
        self.assertEqual(seen_envs[0].get("SPICA_SELF_CHECK_FORCE_DISABLED"), "1")

    def test_without_all_the_force_flag_is_scrubbed_from_env(self):
        env = self_check._worker_env(allow_downloads=False, force_disabled=False)
        self.assertNotIn("SPICA_SELF_CHECK_FORCE_DISABLED", env)


class RvcEmbedderPrecheckTest(unittest.TestCase):
    """rvc_embedder_missing_files: 逐名镜像 vendored 映射, bin+config.json 双查
    (第四轮 review P1: 只查 bin / dash-underscore 启发式都会漏下载路径)。"""

    def _root(self, tmp, dir_name, files):
        base = Path(tmp) / "rvc" / "models" / "embedders" / dir_name
        base.mkdir(parents=True)
        for name in files:
            (base / name).write_bytes(b"x")
        return Path(tmp)

    def test_bin_only_still_reports_missing_config_json(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            root = self._root(tmp, "contentvec", ["pytorch_model.bin"])
            missing = self_check.rvc_embedder_missing_files(root, "contentvec")
            self.assertEqual(len(missing), 1)
            self.assertIn("config.json", missing[0])

    def test_wrong_directory_variant_is_not_accepted(self):
        # spin-v2 的 vendored 目录带连字符; 权重放在 spin_v2(下划线)不算数。
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            root = self._root(tmp, "spin_v2", ["pytorch_model.bin", "config.json"])
            missing = self_check.rvc_embedder_missing_files(root, "spin-v2")
            self.assertEqual(len(missing), 2)

    def test_hubert_names_map_to_underscore_dirs(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            root = self._root(tmp, "chinese_hubert_base",
                              ["pytorch_model.bin", "config.json"])
            self.assertEqual(
                self_check.rvc_embedder_missing_files(root, "chinese-hubert-base"), [])

    def test_unknown_embedder_returns_none_and_custom_is_downloadless(self):
        self.assertIsNone(self_check.rvc_embedder_missing_files(Path("/nope"), "typo"))
        self.assertEqual(self_check.rvc_embedder_missing_files(Path("/nope"), "custom"), [])


class SongLightIndexConsistencyTest(unittest.TestCase):
    """提交前复核 P2: 显式配置的 index 文件缺失时, reason 报「文件缺失」而 status
    却 UNVERIFIED/exit 0 -- 自相矛盾。缺失必须体现为 DEGRADED; 未配置 index 不算缺。"""

    def _cfg(self, model_path: str, index_path: str) -> dict:
        return {
            "enabled": True,
            "separator": {"model_filename": "UVR.onnx"},
            "rvc": {"voice_model": "spica", "execution_mode": "subprocess",
                    "voices": {"spica": {"model_path": model_path,
                                         "index_path": index_path,
                                         "device": "cuda"}}},
        }

    def _rvc_result(self, cfg) -> dict:
        return next(r for r in self_check.check_song_light(cfg) if r["name"] == "song_rvc")

    def test_missing_configured_index_degrades(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            model = Path(tmp) / "m.pth"
            model.write_bytes(b"x")
            result = self._rvc_result(self._cfg(str(model), str(Path(tmp) / "no.index")))
        self.assertEqual(result["status"], "DEGRADED")
        self.assertIn("index_path", result["reason"])

    def test_unconfigured_index_is_not_missing(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            model = Path(tmp) / "m.pth"
            model.write_bytes(b"x")
            result = self._rvc_result(self._cfg(str(model), ""))
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertEqual(result["detail"]["index_path_exists"], "not_configured")

    def test_all_present_stays_unverified(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            model = Path(tmp) / "m.pth"
            index = Path(tmp) / "v.index"
            model.write_bytes(b"x")
            index.write_bytes(b"x")
            result = self._rvc_result(self._cfg(str(model), str(index)))
        self.assertEqual(result["status"], "UNVERIFIED")


class LlmReplyMissingTest(unittest.TestCase):
    def test_ok_with_empty_reply_is_suspicious(self):
        self.assertTrue(self_check.llm_reply_missing({"ok": True, "reply_chars": 0}))
        self.assertTrue(self_check.llm_reply_missing({"ok": True}))

    def test_failure_or_real_reply_is_not(self):
        self.assertFalse(self_check.llm_reply_missing({"ok": False}))
        self.assertFalse(self_check.llm_reply_missing({"ok": True, "reply_chars": 1}))


class OcrFixtureFontFallbackTest(unittest.TestCase):
    def test_tiny_bitmap_fallback_upscales_the_image(self):
        # 无 truetype 候选 + 老 Pillow(load_default 不收 size)时: 11px 位图字体
        # 必须触发 3x 放大, 否则 det 因字太小而假 DEGRADED(review P3)。
        from unittest.mock import patch

        from PIL import ImageFont

        real_default = ImageFont.load_default()

        def fake_load_default(*args, **kwargs):
            if args or kwargs:
                raise TypeError("size not supported")
            return real_default

        with patch("PIL.ImageFont.truetype", side_effect=OSError), \
                patch("PIL.ImageFont.load_default", new=fake_load_default):
            image = self_check._synth_ocr_image()
        self.assertEqual(image.width, 1280 * 3)


class SecretsRedactionTest(unittest.TestCase):
    def test_report_contains_presence_booleans_never_values(self):
        secret_value = "sk-THIS-MUST-NEVER-LEAK-1234"
        presence = self_check.secrets_presence({
            "OPENAI_API_KEY": secret_value,
            "JUDGE_API_KEY": "",
        })
        self.assertIs(presence["OPENAI_API_KEY"], True)
        self.assertIs(presence["JUDGE_API_KEY"], False)
        report = self_check.render_report(
            "light",
            [{"name": "secrets", "status": "PASS", "detail": presence}],
            as_json=True,
        )
        self.assertNotIn(secret_value, report)
        self.assertIn('"OPENAI_API_KEY": true', report)


class RenderReportTest(unittest.TestCase):
    def test_json_roundtrip_carries_exit_code(self):
        results = [{"name": "gpu", "status": "PASS", "detail": {"gpu": "RTX"}},
                   {"name": "stt", "status": "DEGRADED", "detail": {}, "reason": "cpu"}]
        doc = json.loads(self_check.render_report("full", results, as_json=True))
        self.assertEqual(doc["mode"], "full")
        self.assertEqual(doc["exit_code"], 1)
        self.assertEqual(len(doc["results"]), 2)

    def test_human_report_lists_every_check(self):
        results = [{"name": "gpu", "status": "PASS", "detail": {"gpu": "RTX"}},
                   {"name": "tts", "status": "SKIPPED_DISABLED", "detail": {},
                    "reason": "tts.enabled=false"}]
        text = self_check.render_report("light", results, as_json=False)
        self.assertIn("gpu", text)
        self.assertIn("SKIPPED_DISABLED", text)
        self.assertIn("exit_code=0", text)


class SineFixtureTest(unittest.TestCase):
    def test_fixture_is_a_decodable_wav(self):
        import wave as wave_mod
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            path = self_check.write_sine_wav(Path(tmp) / "f.wav", seconds=0.1)
            with wave_mod.open(str(path), "rb") as f:
                self.assertGreater(f.getnframes(), 0)
                self.assertEqual(f.getnchannels(), 1)


if __name__ == "__main__":
    unittest.main()
