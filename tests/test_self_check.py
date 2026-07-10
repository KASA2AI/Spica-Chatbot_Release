"""scripts/self_check.py 自身的可信度测试(2026-07 review 要求).

默认真模型一概不加载(唯一例外: SPICA_SELF_CHECK_HEAVY_TESTS=1 显式开启的 UVR 真加载集成测试); 这里测的是编排器本身 -- worker 结果解析、超时进程树清理、
exit code 语义、JSON 输出、secrets 不泄漏、fake-worker 注入。
"""

import importlib.util
import json
import os
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
    # 与真 worker 同款出口: 读父进程注入的 nonce(防后置 marker 欺骗)
    code = (
        "import json, os\n"
        "nonce = os.environ.get('SPICA_SELF_CHECK_MARKER_NONCE', '')\n"
        f"marker = {self_check.RESULT_MARKER!r} + ((nonce + ':') if nonce else '')\n"
        f"print('\\n' + marker + json.dumps({payload!r}))\n"
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

    def test_finite_scale_with_infinite_product_exits_3_without_spawning(self):
        # 第七轮 review P2: 1e308 本身有限, 乘默认 timeout 后为 inf ->
        # communicate OverflowError 原生 exit 1。必须在任何 spawn 前验证乘积。
        from unittest.mock import patch

        with patch.object(self_check.subprocess, "Popen",
                          side_effect=AssertionError("must not spawn")):
            # --force: 不依赖「应用是否在跑」的守卫状态, 直指乘积验证本身
            rc = self_check.main(["--full", "--force", "--timeout-scale", "1e308"])
        self.assertEqual(rc, 3)

    def test_spawn_oserror_maps_to_exit_3_not_an_escaped_exception(self):
        # 第六轮 review P2: OS 拒绝创建进程时 OSError 逃出 main, CLI 原生 exit 1
        # 与「1=DEGRADED」冲突 -- 自检自身错误应为 3。
        from unittest.mock import patch

        with patch.object(self_check.subprocess, "Popen",
                          side_effect=OSError("cannot fork")):
            rc = self_check.main(["--full", "--only", "tts"])
        self.assertEqual(rc, 3)

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

    def test_marker_inside_payload_string_does_not_spoof_the_parser(self):
        # 第七轮 review P2: reason 里合法出现 marker 文本时, rfind 从行内最后一个
        # marker 起切会把 JSON 拦腰截断 -> 误报 without a result。
        payload = {"status": "PASS",
                   "reason": f"log said {self_check.RESULT_MARKER} appeared upstream",
                   "detail": {}}
        stdout = self_check.RESULT_MARKER + json.dumps(payload)
        parsed = self_check.parse_worker_stdout(stdout)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["status"], "PASS")

    def test_malformed_marker_does_not_clobber_a_prior_valid_result(self):
        stdout = "\n".join([
            self_check.RESULT_MARKER + json.dumps({"status": "PASS", "detail": {}}),
            self_check.RESULT_MARKER + "{this is not json",
        ])
        parsed = self_check.parse_worker_stdout(stdout)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["status"], "PASS")

    def test_last_of_multiple_valid_markers_wins(self):
        stdout = "\n".join([
            self_check.RESULT_MARKER + json.dumps({"status": "FAIL"}),
            self_check.RESULT_MARKER + json.dumps({"status": "PASS"}),
        ])
        self.assertEqual(self_check.parse_worker_stdout(stdout)["status"], "PASS")

    def test_marker_in_reason_parses_through_a_real_subprocess(self):
        # 真实子进程输出路径(非直调 parser)。
        payload = {"status": "PASS",
                   "reason": f"upstream printed {self_check.RESULT_MARKER} once",
                   "detail": {"x": 1}}
        result = self_check.run_subprocess_check(
            _fake_worker_cmd(payload), timeout_s=30, sample_vram=False)
        self.assertEqual(result["status"], "PASS")
        self.assertIn("upstream printed", result["reason"])

    def test_marker_glued_after_library_output_without_newline_still_parses(self):
        # 第六轮 review P2: 子库最后一次 stdout 写入没有换行 -> worker 的 marker
        # 被拼成 "noiseSELF_CHECK_RESULT_JSON:{...}"; 只认行首会误报 without a result。
        stdout = "loading model...noise" + self_check.RESULT_MARKER + json.dumps(
            {"status": "PASS", "detail": {"x": 1}})
        payload = self_check.parse_worker_stdout(stdout)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["status"], "PASS")


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
            "import json, os, sys\n"
            "nonce = os.environ.get('SPICA_SELF_CHECK_MARKER_NONCE', '')\n"
            f"marker = {self_check.RESULT_MARKER!r} + ((nonce + ':') if nonce else '')\n"
            "print('\\n' + marker + json.dumps({'status': 'PASS'}))\n"
            "sys.exit(9)\n"
        )
        result = self_check.run_subprocess_check(
            [sys.executable, "-c", code], timeout_s=30, sample_vram=False,
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("rc=9", result["reason"])
        self.assertIn("AFTER reporting", result["reason"])  # 真走 crash-after-report 分支

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
            "import os\n"
            "nonce = os.environ.get('SPICA_SELF_CHECK_MARKER_NONCE', '')\n"
            f"marker = {self_check.RESULT_MARKER!r} + ((nonce + ':') if nonce else '')\n"
            "print('\\n' + marker + '\"just a string\"')\n"
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

    @unittest.skipIf(sys.platform == "win32", "POSIX supervisor path")
    def test_normal_same_group_helper_does_not_kill_the_supervisor(self):
        # 第八轮 Standards P1: 普通(未 setsid)helper 与 supervisor 同 PGID --
        # 清扫时 killpg 会把 supervisor 一起杀掉, 合法 PASS 被误报 rc=-9 FAIL。
        import os
        import signal as signal_mod
        from tempfile import TemporaryDirectory

        code = (
            "import json, os, subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            "                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "open(sys.argv[1], 'w').write(str(child.pid))\n"
            "nonce = os.environ.get('SPICA_SELF_CHECK_MARKER_NONCE', '')\n"
            f"marker = {self_check.RESULT_MARKER!r} + ((nonce + ':') if nonce else '')\n"
            "print('\\n' + marker + json.dumps({'status': 'PASS', 'detail': {}}))\n"
            "sys.exit(0)\n"
        )
        child_pid = None
        try:
            with TemporaryDirectory() as tmp:
                pidfile = Path(tmp) / "child.pid"
                result = self_check.run_subprocess_check(
                    [sys.executable, "-c", code, str(pidfile)],
                    timeout_s=30, sample_vram=False,
                )
                child_pid = int(pidfile.read_text())
                self.assertEqual(result["status"], "PASS", result.get("reason"))
                deadline = time.time() + 10
                alive = True
                while time.time() < deadline:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        alive = False
                        break
                    time.sleep(0.1)
                self.assertFalse(alive, f"helper {child_pid} 未被清扫")
        finally:
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal_mod.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    @unittest.skipIf(sys.platform != "linux", "/proc enumeration path")
    def test_detached_descendants_swept_even_without_ps(self):
        # 第八轮 Spec P1: PATH 里没有 ps 时 _children() 曾静默返回空 -- 清扫假绿。
        import os
        import signal as signal_mod
        from tempfile import TemporaryDirectory

        code = (
            "import subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            "                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
            "                         start_new_session=True)\n"
            "open(sys.argv[1], 'w').write(str(child.pid))\n"
            "sys.exit(7)\n"
        )
        child_pid = None
        env = {**os.environ, "PATH": ""}
        try:
            with TemporaryDirectory() as tmp:
                pidfile = Path(tmp) / "child.pid"
                result = self_check.run_subprocess_check(
                    [sys.executable, "-c", code, str(pidfile)],
                    timeout_s=30, env=env, sample_vram=False,
                )
                self.assertEqual(result["status"], "FAIL")
                child_pid = int(pidfile.read_text())
                deadline = time.time() + 10
                alive = True
                while time.time() < deadline:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        alive = False
                        break
                    time.sleep(0.1)
                self.assertFalse(alive, f"no-ps sweep missed descendant {child_pid}")
        finally:
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal_mod.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    @unittest.skipIf(sys.platform == "win32", "POSIX path")
    def test_healthy_worker_has_no_fixed_sweep_delay(self):
        # 第八轮 Spec P2: ps 快照把自己算进 kids -> 每个 worker 固定空转 ~10s。
        started = time.time()
        result = self_check.run_subprocess_check(
            _fake_worker_cmd({"status": "PASS", "detail": {}}),
            timeout_s=30, sample_vram=False,
        )
        elapsed = time.time() - started
        self.assertEqual(result["status"], "PASS")
        self.assertLess(elapsed, 6, f"healthy worker took {elapsed:.1f}s (sweep spin?)")

    def test_reader_thread_start_failure_cleans_up_and_maps_to_internal_error(self):
        # 第八轮 Spec P1: Thread.start 资源错误曾逃逸为原生 exit 1 且不清理 worker。
        from unittest.mock import patch

        spawned: list = []
        real_popen = self_check.subprocess.Popen

        def capture(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            spawned.append(proc)
            return proc

        class _NoStartThread(self_check.threading.Thread):
            def start(self):  # 构造成功、start 才抛(第九轮: 钉真实场景)
                raise RuntimeError("thread quota")

        with patch.object(self_check.subprocess, "Popen", side_effect=capture), \
                patch.object(self_check.threading, "Thread", _NoStartThread):
            with self.assertRaises(self_check.SelfCheckInternalError):
                self_check.run_subprocess_check(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    timeout_s=30, sample_vram=False,
                )
        self.assertEqual(len(spawned), 1)
        deadline = time.time() + 10
        while time.time() < deadline and spawned[0].poll() is None:
            time.sleep(0.1)
        self.assertIsNotNone(spawned[0].poll(), "worker leaked after thread-start failure")

    def test_invalid_utf8_before_marker_does_not_swallow_the_result(self):
        # 第八轮 Spec P2: 非法 UTF-8 使 reader 静默退出 -> 合法 PASS 丢失。
        code = (
            "import json, os, sys\n"
            "sys.stdout.buffer.write(b'\\xff\\xfe garbage \\xff\\n')\n"
            "sys.stdout.flush()\n"
            "nonce = os.environ.get('SPICA_SELF_CHECK_MARKER_NONCE', '')\n"
            f"marker = {self_check.RESULT_MARKER!r} + ((nonce + ':') if nonce else '')\n"
            "print('\\n' + marker + json.dumps({'status': 'PASS', 'detail': {}}))\n"
        )
        result = self_check.run_subprocess_check(
            [sys.executable, "-c", code], timeout_s=30, sample_vram=False)
        self.assertEqual(result["status"], "PASS", result.get("reason"))

    def test_late_plain_marker_cannot_spoof_the_nonced_result(self):
        # 第八轮 Spec P2: 插件 atexit 后置输出公开 marker 的 PASS 不得覆盖真实
        # FAIL -- 父进程只认带本次 nonce 的 marker。
        code = (
            "import json, os, sys\n"
            "nonce = os.environ.get('SPICA_SELF_CHECK_MARKER_NONCE', '')\n"
            f"marker = {self_check.RESULT_MARKER!r} + ((nonce + ':') if nonce else '')\n"
            "print('\\n' + marker + json.dumps({'status': 'FAIL', 'reason': 'real', 'detail': {}}))\n"
            f"print('\\n' + {self_check.RESULT_MARKER!r} + json.dumps({{'status': 'PASS', 'detail': {{}}}}))\n"
        )
        result = self_check.run_subprocess_check(
            [sys.executable, "-c", code], timeout_s=30, sample_vram=False)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["reason"], "real")

    @unittest.skipIf(sys.platform == "win32", "POSIX path")
    def test_cleanup_failure_is_reported_not_silently_claimed(self):
        # 第七轮 review P2: kill helper 失败时不得照旧声称 process tree killed。
        from unittest.mock import patch

        with patch.object(self_check, "_cleanup_tree",
                          return_value=(False, "SIGTERM denied by sandbox")):
            result = self_check.run_subprocess_check(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                timeout_s=1.0, sample_vram=False,
            )
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("清理未确认", result["reason"])
        self.assertIn("SIGTERM denied", result["reason"])
        self.assertNotIn("process tree killed", result["reason"])

    @unittest.skipIf(sys.platform == "win32", "POSIX supervisor path")
    def test_crashed_worker_leaves_no_detached_session_descendants(self):
        # 第七轮 review P1: 孙进程 start_new_session=True 脱组后, killpg(worker)
        # 杀不到它 -- 进程组 != 进程树。worker exit 7 正常返回后孙进程必须消失。
        import os
        import signal as signal_mod
        from tempfile import TemporaryDirectory

        code = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            "                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
            "                         start_new_session=True)\n"  # 脱组: 自己的 PGID
            "open(sys.argv[1], 'w').write(str(child.pid))\n"
            "sys.exit(7)\n"
        )
        child_pid = None
        try:
            with TemporaryDirectory() as tmp:
                pidfile = Path(tmp) / "child.pid"
                result = self_check.run_subprocess_check(
                    [sys.executable, "-c", code, str(pidfile)],
                    timeout_s=30, sample_vram=False,
                )
                self.assertEqual(result["status"], "FAIL")
                child_pid = int(pidfile.read_text())
                deadline = time.time() + 10
                alive = True
                while time.time() < deadline:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        alive = False
                        break
                    time.sleep(0.1)
                self.assertFalse(
                    alive, f"detached descendant {child_pid} survived the cleanup")
        finally:
            if child_pid is not None:  # 测试失败也不许留 canary 进程
                try:
                    os.kill(child_pid, signal_mod.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    @unittest.skipIf(sys.platform == "win32", "POSIX killpg path")
    def test_crashed_worker_leaves_no_descendants_behind(self):
        # 第六轮 review P1: worker 拉起孙进程后自行 exit 7 -- communicate() 正常
        # 返回, 此前只生成 FAIL 结果不清树, 孙进程(可能占 GPU)继续存活。
        import os
        from tempfile import TemporaryDirectory

        code = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            "                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "open(sys.argv[1], 'w').write(str(child.pid))\n"
            "sys.exit(7)\n"
        )
        with TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "child.pid"
            started = time.time()
            result = self_check.run_subprocess_check(
                [sys.executable, "-c", code, str(pidfile)],
                timeout_s=30, sample_vram=False,
            )
            # 孙进程不占管道 -> communicate 立即返回(不是靠 30s 超时兜底清树)
            self.assertLess(time.time() - started, 10)
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
            self.assertFalse(alive, f"descendant {child_pid} survived the crash cleanup")

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
                _interrupted = False

                def wait(self, *args, **kwargs):  # noqa: ARG002
                    # 首次 wait 模拟 Ctrl-C(等孙进程真拉起来后); 清理路径的后续
                    # wait 走真实实现, 否则 supervisor 收尾无法被确认。
                    if not type(self)._interrupted:
                        deadline = time.time() + 10
                        while not pidfile.exists() and time.time() < deadline:
                            time.sleep(0.05)
                        type(self)._interrupted = True
                        raise KeyboardInterrupt
                    return super().wait(*args, **kwargs)

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


class TtsCheckRegistryTest(unittest.TestCase):
    """第六/七轮 review P2: TTS worker 的 registry 必须与生产的注册前置能力等价
    (插件 register() 可以依赖已有 LLM builtin), 且插件错误要如实带出。"""

    _PLUGIN_SOURCE = (
        "def register(registry):\n"
        "    # 生产可行的合法插件: 先依赖已有 LLM builtin, 再注册自定义 TTS\n"
        "    assert 'openai_compatible' in registry.list_adapters('llm'), 'LLM builtin missing'\n"
        "    from agent_tools.tts.adapters import TextOnlyTTSAdapter\n"
        "    registry.register_tts('plugin_voice',\n"
        "                          lambda config=None, service=None: TextOnlyTTSAdapter())\n"
    )

    def _plugin_env(self, tmp: str):
        root = Path(tmp) / "plugins"
        (root / "fake_tts").mkdir(parents=True)
        (root / "fake_tts" / "__init__.py").write_text(self._PLUGIN_SOURCE, encoding="utf-8")
        manifest = Path(tmp) / "plugins.yaml"
        manifest.write_text("plugins:\n  - fake_tts\n", encoding="utf-8")
        return root, manifest

    def test_registry_resolves_builtin_text_only(self):
        registry, errors = self_check._tts_check_registry()
        self.assertIsInstance(errors, dict)
        adapter = registry.resolve_tts(
            "text_only", config={"provider": "text_only"}, service=None)
        from agent_tools.tts.adapters import TextOnlyTTSAdapter

        self.assertIsInstance(adapter, TextOnlyTTSAdapter)

    def test_plugin_depending_on_llm_builtin_works_like_production(self):
        # 第七轮 review P2 场景: 该插件在生产 registry 下注册成功 -- 自检 registry
        # 必须同样成功并能 resolve 同一 provider, 否则对合法配置假 FAIL。
        from tempfile import TemporaryDirectory

        from spica.host.builtins import register_builtin_adapters
        from spica.plugins.host import PluginHost
        from spica.plugins.registry import CapabilityRegistry

        with TemporaryDirectory() as tmp:
            root, manifest = self._plugin_env(tmp)
            production = CapabilityRegistry()
            register_builtin_adapters(production)
            plugin_host = PluginHost(production, plugins_root=root, manifest_path=manifest)
            plugin_host.load()
            self.assertEqual(plugin_host.errors(), {})  # 生产侧确实可注册
            self.assertIsNotNone(production.resolve_tts(
                "plugin_voice", config={}, service=None))

            registry, errors = self_check._tts_check_registry(
                plugins_root=root, manifest_path=manifest)
            self.assertEqual(errors, {})
            self.assertIsNotNone(registry.resolve_tts(
                "plugin_voice", config={}, service=None))

    def test_plugin_depending_on_inspect_screen_tool_works_like_production(self):
        # 第八轮 review P2: 生产插件加载前已有 inspect_screen 等内建工具 --
        # 自检 registry 必须提供同等注册前置能力。
        from tempfile import TemporaryDirectory

        plugin = (
            "def register(registry):\n"
            "    assert 'inspect_screen' in registry.list_adapters('tool'), 'tool missing'\n"
            "    from agent_tools.tts.adapters import TextOnlyTTSAdapter\n"
            "    registry.register_tts('screen_dep_voice',\n"
            "                          lambda config=None, service=None: TextOnlyTTSAdapter())\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "plugins"
            (root / "screen_dep").mkdir(parents=True)
            (root / "screen_dep" / "__init__.py").write_text(plugin, encoding="utf-8")
            manifest = Path(tmp) / "plugins.yaml"
            manifest.write_text("plugins:\n  - screen_dep\n", encoding="utf-8")
            registry, errors = self_check._tts_check_registry(
                plugins_root=root, manifest_path=manifest)
        self.assertEqual(errors, {})
        self.assertIsNotNone(registry.resolve_tts("screen_dep_voice", config={}, service=None))

    def test_plugin_errors_are_surfaced_not_swallowed(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "plugins"
            (root / "broken").mkdir(parents=True)
            (root / "broken" / "__init__.py").write_text(
                "def register(registry):\n    raise RuntimeError('plugin boom')\n",
                encoding="utf-8")
            manifest = Path(tmp) / "plugins.yaml"
            manifest.write_text("plugins:\n  - broken\n", encoding="utf-8")
            registry, errors = self_check._tts_check_registry(
                plugins_root=root, manifest_path=manifest)
        self.assertIn("broken", errors)
        self.assertIn("plugin boom", errors["broken"])
        # builtins 仍可用(单个坏插件不摧毁检查)
        from agent_tools.tts.adapters import TextOnlyTTSAdapter

        adapter = registry.resolve_tts(
            "text_only", config={"provider": "text_only"}, service=None)
        self.assertIsInstance(adapter, TextOnlyTTSAdapter)


class SttLightModelKindTest(unittest.TestCase):
    """第六轮 review P2: 合法 Hub ID(org/name, 如 Systran/faster-whisper-large-v3)
    因含 / 被当成缺失的本地路径 -> 轻量档误报 DEGRADED/exit 1。faster-whisper
    明确支持这种 ID。"""

    def _app(self, model: str):
        from types import SimpleNamespace

        return SimpleNamespace(stt=SimpleNamespace(
            backend="faster_whisper", model=model, device="cuda",
            compute_type="float16", warmup_on_startup=True))

    def test_org_slash_name_hub_id_is_unverified_not_degraded(self):
        result = self_check.check_stt_light(self._app("Systran/faster-whisper-large-v3"))
        self.assertEqual(result["status"], "UNVERIFIED")

    def test_bare_size_name_is_unverified(self):
        result = self_check.check_stt_light(self._app("large-v3-turbo"))
        self.assertEqual(result["status"], "UNVERIFIED")

    def test_existing_local_dir_with_model_bin_is_unverified(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            (Path(tmp) / "model.bin").write_bytes(b"x")  # CTranslate2 布局
            result = self_check.check_stt_light(self._app(tmp))
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertIs(result["detail"]["local_model_dir"], True)

    def test_missing_multi_segment_path_is_degraded(self):
        # 两段以上斜杠不可能是 Hub ID -- 只能是配置错误的本地路径
        result = self_check.check_stt_light(self._app("spica_data/models/absent-dir"))
        self.assertEqual(result["status"], "DEGRADED")

    def test_invalid_bare_size_name_is_degraded(self):
        # 第八轮 review P2: faster-whisper 对未知裸 size 名离线即抛 ValueError --
        # 不能放行为 UNVERIFIED/exit 0。合法 size 表来自 faster_whisper.utils。
        result = self_check.check_stt_light(self._app("definitely-not-a-faster-whisper-size"))
        self.assertEqual(result["status"], "DEGRADED")

    def test_invalid_hub_id_segments_are_degraded(self):
        # HF 段不得以 . 或 - 开头: -bad/model 与 org/.bad 都是必然非法的 ID。
        for model in ("-bad/model", "org/.bad"):
            result = self_check.check_stt_light(self._app(model))
            self.assertEqual(result["status"], "DEGRADED", model)

    def test_explicit_relative_and_absolute_missing_paths_are_degraded(self):
        # 第七轮 review P2: ./ ../ 和绝对路径是显式本地路径, 不能仅按斜杠数量
        # 误判成 Hub ID(". "".." 能通过字符类正则)。
        for model in ("./missing-dir", "../missing-dir", "/opt/models/absent"):
            result = self_check.check_stt_light(self._app(model))
            self.assertEqual(result["status"], "DEGRADED", model)


class UvrEffectiveModelDirTest(unittest.TestCase):
    """第六轮 review P1: 禁下载预检探测的是 Separator 默认目录, 但生产
    separate_vocals 把 extra_kwargs(可含 model_file_dir)透传给 Separator --
    自定义目录缺模型时预检放行, audio-separator 照样下载。预检必须用与
    _build_separator 相同的生效目录。"""

    def test_extra_kwargs_model_file_dir_wins_without_probing(self):
        from unittest.mock import patch

        # 显式 override 时绝不构造 Separator 探测(探测本身可能触发副作用)
        with patch.dict(sys.modules, {"audio_separator.separator": None}):
            result = self_check.uvr_effective_model_dir(
                {"extra_kwargs": {"model_file_dir": "/custom/uvr/models"}}, environ={}
            )
        self.assertEqual(result, "/custom/uvr/models")

    def test_no_override_falls_back_to_separator_probe(self):
        from types import ModuleType, SimpleNamespace
        from unittest.mock import patch

        fake_module = ModuleType("audio_separator.separator")
        fake_module.Separator = lambda **kwargs: SimpleNamespace(
            model_file_dir="/tmp/audio-separator-models"
        )
        with patch.dict(sys.modules, {"audio_separator.separator": fake_module}):
            result = self_check.uvr_effective_model_dir({"extra_kwargs": {}}, environ={})
        self.assertEqual(result, "/tmp/audio-separator-models")

    def test_env_var_beats_extra_kwargs_override(self):
        # 第七轮 review P1: audio-separator 0.44.2 里 AUDIO_SEPARATOR_MODEL_DIR
        # env 优先于 model_file_dir kwarg(separator.py:167) -- 预检必须同序。
        result = self_check.uvr_effective_model_dir(
            {"extra_kwargs": {"model_file_dir": "/custom/B"}},
            environ={"AUDIO_SEPARATOR_MODEL_DIR": "/env/A"},
        )
        self.assertEqual(result, "/env/A")

    def test_env_priority_matches_the_real_separator(self):
        # 用真实 Separator 构造语义钉上游行为(纯本地构造, 零网络)。
        import os
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        from audio_separator.separator import Separator

        with TemporaryDirectory() as env_dir, TemporaryDirectory() as kwarg_dir:
            with patch.dict(os.environ, {"AUDIO_SEPARATOR_MODEL_DIR": env_dir}):
                sep = Separator(output_dir=kwarg_dir, model_file_dir=kwarg_dir)
            self.assertEqual(str(sep.model_file_dir), env_dir)
            # 我们的 helper 必须给出同一答案
            self.assertEqual(
                self_check.uvr_effective_model_dir(
                    {"extra_kwargs": {"model_file_dir": kwarg_dir}},
                    environ={"AUDIO_SEPARATOR_MODEL_DIR": env_dir},
                ),
                env_dir,
            )


class UvrPrerequisitesTest(unittest.TestCase):
    """第七轮 review P1: 主 onnx 存在但 load_model 的前置 metadata 缺失时,
    audio-separator 仍会从 GitHub 下载 -- 0.44.2 源码核对的完整前置清单:
    模型文件 + download_checks.json(list_supported_model_files) + 非 yaml 模型的
    vr_model_data.json/mdx_model_data.json(load_model_data_using_hash)。"""

    def test_model_present_but_metadata_missing_is_reported(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            (Path(tmp) / "UVR-MDX-NET-Inst_HQ_3.onnx").write_bytes(b"x")
            missing = self_check.uvr_missing_prerequisites(tmp, "UVR-MDX-NET-Inst_HQ_3.onnx")
        names = {Path(p).name for p in missing}
        self.assertEqual(names, {"download_checks.json", "vr_model_data.json",
                                 "mdx_model_data.json"})

    def test_all_prerequisites_present_is_clean(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            for name in ("UVR-MDX-NET-Inst_HQ_3.onnx", "download_checks.json",
                         "vr_model_data.json", "mdx_model_data.json"):
                (Path(tmp) / name).write_bytes(b"x")
            self.assertEqual(
                self_check.uvr_missing_prerequisites(tmp, "UVR-MDX-NET-Inst_HQ_3.onnx"), [])

    _REAL_CHECKS = Path("/tmp/audio-separator-models/download_checks.json")
    _FIXTURE = Path(__file__).parent / "fixtures" / "uvr_download_checks_min.json"
    # 固定期望(独立真相源: 从 0.44.2 真实 download_checks.json 人工抄录, 不由
    # 实现同一解析路径生成 -- 第十轮 Standards):
    _HTDEMUCS_EXPECTED = {"f7e0c4bc-ba3fe64a.th", "d12395a8-e57c48e6.th",
                          "92cfc3b6-ef3bcb9c.th", "04573f0d-f3cf25b2.th"}

    def test_yaml_model_requires_its_download_files_from_committed_fixture(self):
        # 第八轮 review P1: htdemucs_ft.yaml 在场但配套 .th 权重缺失时, 真实
        # 0.44.2 仍会下载 -- 前置清单必须解析该模型的 download_files, 而不是
        # 固化「yaml 无需其他文件」的错误假设。
        import shutil
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        with TemporaryDirectory() as tmp:
            shutil.copy(self._FIXTURE, Path(tmp) / "download_checks.json")
            (Path(tmp) / "htdemucs_ft.yaml").write_bytes(b"x")
            with patch("requests.get", side_effect=AssertionError("network!")):
                missing = self_check.uvr_missing_prerequisites(tmp, "htdemucs_ft.yaml")
        missing_names = {Path(m).name for m in missing}
        # 精确等值断言(不是子集): 多报文件同样是缺陷
        self.assertEqual(missing_names, self._HTDEMUCS_EXPECTED)

    @unittest.skipUnless(
        os.environ.get("SPICA_SELF_CHECK_HEAVY_TESTS") == "1"
        and _REAL_CHECKS.exists()
        and (_REAL_CHECKS.parent / "UVR-MDX-NET-Inst_HQ_3.onnx").exists()
        and (_REAL_CHECKS.parent / "vr_model_data.json").exists()
        and (_REAL_CHECKS.parent / "mdx_model_data.json").exists(),
        "需要本地完整 UVR 前置文件")
    def test_real_load_model_with_full_prerequisites_does_zero_network(self):
        # 真实 Separator.load_model 全程零网络(review 要求覆盖真实行为而非 fake)。
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        from audio_separator.separator import Separator

        model_dir = str(self._REAL_CHECKS.parent)
        self.assertEqual(
            self_check.uvr_missing_prerequisites(model_dir, "UVR-MDX-NET-Inst_HQ_3.onnx"),
            [])
        with TemporaryDirectory() as out:
            with patch("requests.get", side_effect=AssertionError("network!")), \
                    patch("requests.Session", side_effect=AssertionError("network!")):
                sep = Separator(output_dir=out, model_file_dir=model_dir)
                sep.load_model("UVR-MDX-NET-Inst_HQ_3.onnx")  # 缺前置才会联网

    def test_ckpt_with_companion_yaml_needs_no_hash_metadata(self):
        # MDXC ckpt+配套 yaml 不走 hash 路径 -- fixture 固定条目 MDX23C_D1581。
        import shutil
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        with TemporaryDirectory() as tmp:
            shutil.copy(self._FIXTURE, Path(tmp) / "download_checks.json")
            (Path(tmp) / "MDX23C_D1581.ckpt").write_bytes(b"x")
            (Path(tmp) / "model_2_stem_061321.yaml").write_bytes(b"x")
            with patch("requests.get", side_effect=AssertionError("network!")):
                missing = self_check.uvr_missing_prerequisites(tmp, "MDX23C_D1581.ckpt")
        self.assertEqual(missing, [], f"companion-yaml 模型被强加了多余前置: {missing}")

    def test_precheck_helpers_do_zero_network(self):
        from unittest.mock import patch

        with patch("requests.get", side_effect=AssertionError("network!")), \
                patch("requests.Session", side_effect=AssertionError("network!")):
            self_check.uvr_effective_model_dir({"extra_kwargs": {}}, environ={})
            self_check.uvr_missing_prerequisites("/nonexistent", "x.onnx")


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


class ReportRedactionTest(unittest.TestCase):
    """第六轮 review P1: worker stderr tail / str(exc) 可能携带 secret 值原样进
    报告。render_report 是唯一输出口, 必须按 SECRETS_ENV_MAP 的 env 值兜底脱敏。"""

    CANARY = "sk-canary-THIS-MUST-NEVER-APPEAR-9f8e7d"

    def test_secret_value_in_reason_is_redacted_from_both_formats(self):
        import os
        from unittest.mock import patch

        results = [{"name": "llm", "status": "FAIL",
                    "reason": f"AuthenticationError: bad key {self.CANARY} rejected",
                    "detail": {"stderr_tail": f"key={self.CANARY}"}}]
        with patch.dict(os.environ, {"OPENAI_API_KEY": self.CANARY}):
            as_json = self_check.render_report("full", results, as_json=True)
            as_text = self_check.render_report("full", results, as_json=False)
        for rendered in (as_json, as_text):
            self.assertNotIn(self.CANARY, rendered)
            self.assertIn("REDACTED:OPENAI_API_KEY", rendered)

    def test_secret_with_quotes_and_backslashes_survives_json_escaping(self):
        # 第七轮 review P1: 序列化后字符串 replace 对含 " 和 \ 的 secret 失效
        # (dumps 转义后搜不到原值)。必须在序列化前做结构化清洗。
        import os
        from unittest.mock import patch

        canary = 'pa"ss\\wo"rd\\CANARY!!'
        results = [{"name": "llm", "status": "FAIL",
                    "reason": f"bad key {canary} rejected",
                    "detail": {"nested": {"deep": f"echo {canary}"}}}]
        with patch.dict(os.environ, {"JUDGE_API_KEY": canary}):
            rendered = self_check.render_report("full", results, as_json=True)
        doc = json.loads(rendered)  # 必须仍是合法 JSON
        self.assertNotIn(canary, json.dumps(doc, ensure_ascii=False))
        self.assertNotIn(canary, doc["results"][0]["reason"])
        self.assertNotIn(canary, doc["results"][0]["detail"]["nested"]["deep"])
        self.assertIn("REDACTED:JUDGE_API_KEY", doc["results"][0]["reason"])

    def test_five_char_password_is_redacted_too(self):
        # 不得以「太短」为理由泄漏短密码(QBITTORRENT_PASSWORD 常见 5-6 位)。
        import os
        from unittest.mock import patch

        results = [{"name": "x", "status": "FAIL", "reason": "auth with adm1n failed",
                    "detail": {}}]
        with patch.dict(os.environ, {"QBITTORRENT_PASSWORD": "adm1n"}):
            rendered = self_check.render_report("full", results, as_json=False)
        self.assertNotIn("adm1n", rendered)
        self.assertIn("REDACTED:QBITTORRENT_PASSWORD", rendered)

    def test_worker_stderr_crash_leak_is_redacted(self):
        # stderr tail 进 reason 的路径: 真实子进程把 secret 吐到 stderr 后崩溃。
        import os
        from unittest.mock import patch

        canary = 'stderr"CANARY\\leak-77'
        code = ("import sys\n"
                f"print('auth failed: ' + {canary!r}, file=sys.stderr)\n"
                "sys.exit(5)\n")
        with patch.dict(os.environ, {"OPENAI_API_KEY": canary}):
            result = self_check.run_subprocess_check(
                [sys.executable, "-c", code], timeout_s=30, sample_vram=False)
            rendered = self_check.render_report(
                "full", [{"name": "x", **result}], as_json=True)
        self.assertEqual(result["status"], "FAIL")
        # 在解析后的结构里查(序列化转义会让裸字符串搜索假绿)
        doc = json.loads(rendered)
        self.assertNotIn(canary, doc["results"][0]["reason"])
        self.assertIn("REDACTED:OPENAI_API_KEY", doc["results"][0]["reason"])

    def test_secret_as_dict_key_is_redacted(self):
        # 第八轮 review P1: redact_obj 只清 value 不清 key。
        import os
        from unittest.mock import patch

        canary = "key-CANARY-as-dict-key-31337"
        results = [{"name": "x", "status": "FAIL", "reason": "r",
                    "detail": {"nested": {canary: "value"}}}]
        with patch.dict(os.environ, {"OPENAI_API_KEY": canary}):
            rendered = self_check.render_report("full", results, as_json=True)
        doc = json.loads(rendered)
        self.assertNotIn(canary, json.dumps(doc, ensure_ascii=False))

    def test_common_word_secret_does_not_break_the_status_protocol(self):
        # 第八轮 review P2: QBITTORRENT_PASSWORD=PASS 时, 合法 status="PASS" 被
        # 替换成占位符 -> invalid status/exit 2。协议字段必须保持合法枚举。
        import os
        from unittest.mock import patch

        results = [{"name": "x", "status": "PASS",
                    "reason": "login with PASS ok", "detail": {}}]
        with patch.dict(os.environ, {"QBITTORRENT_PASSWORD": "PASS"}):
            rendered = self_check.render_report("full", results, as_json=True)
        doc = json.loads(rendered)
        self.assertEqual(doc["results"][0]["status"], "PASS")  # 协议字段不被脱敏
        self.assertEqual(doc["exit_code"], 0)
        self.assertIn("REDACTED:QBITTORRENT_PASSWORD", doc["results"][0]["reason"])

    def test_hidden_worker_marker_output_is_redacted(self):
        # 直接 --worker 输出不经 render_report -- worker 侧也必须结构化脱敏。
        import contextlib
        import io
        import os
        from unittest.mock import patch

        canary = 'exc"CANARY\\worker-88'

        def _boom():
            raise RuntimeError(f"key {canary} invalid")

        buf = io.StringIO()
        with patch.dict(self_check.WORKERS, {"boom": _boom}), \
                patch.dict(os.environ, {"OPENAI_API_KEY": canary}), \
                contextlib.redirect_stdout(buf):
            self_check.run_worker_and_print("boom")
        output = buf.getvalue()
        self.assertNotIn(canary, output)
        payload = self_check.parse_worker_stdout(output)
        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("REDACTED:OPENAI_API_KEY", payload["reason"])


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




class NinthRoundFixesTest(unittest.TestCase):
    """第九轮 review 修复的回归钉。"""

    def test_truncation_happens_after_redaction_in_worker_reason(self):
        # P1: [:500] 截断落进 secret 中间会留下匹配不到的片段。
        import contextlib
        import io
        from unittest.mock import patch

        canary = "sk-CANARY-" + "x" * 26  # 36 字符
        long_message = "e" * 490 + canary  # 截断点(500)落在 canary 内部

        def _boom():
            raise RuntimeError(long_message)

        buf = io.StringIO()
        with patch.dict(self_check.WORKERS, {"boom9": _boom}), \
                patch.dict(os.environ, {"OPENAI_API_KEY": canary}), \
                contextlib.redirect_stdout(buf):
            self_check.run_worker_and_print("boom9")
        payload = self_check.parse_worker_stdout(buf.getvalue())
        # 核心不变量: secret 的任何片段都不许出现(占位符本身可被截断, 无妨)
        self.assertNotIn(canary[:12], payload["reason"])
        self.assertNotIn(canary, payload["reason"])

    def test_directory_masquerading_as_model_is_still_missing(self):
        # P1: exists() 会被同名目录骗过, 真实 0.44.2 用 isfile 判断后仍下载。
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            (Path(tmp) / "UVR-MDX-NET-Inst_HQ_3.onnx").mkdir()  # 目录冒充
            for name in ("download_checks.json", "vr_model_data.json",
                         "mdx_model_data.json"):
                (Path(tmp) / name).write_bytes(b"{}")
            missing = self_check.uvr_missing_prerequisites(tmp, "UVR-MDX-NET-Inst_HQ_3.onnx")
        self.assertTrue(any(m.endswith(".onnx") for m in missing), missing)

    def test_flood_of_plain_markers_cannot_evict_the_nonced_result(self):
        # P2: 真 nonce PASS 后 4 条明文 marker 不得把真结果挤出候选窗。
        code = (
            "import json, os\n"
            "nonce = os.environ.get('SPICA_SELF_CHECK_MARKER_NONCE', '')\n"
            f"marker = {self_check.RESULT_MARKER!r} + ((nonce + ':') if nonce else '')\n"
            "print('\\n' + marker + json.dumps({'status': 'PASS', 'detail': {}}))\n"
            f"print(('\\n' + {self_check.RESULT_MARKER!r} + 'noise') * 4)\n"
        )
        result = self_check.run_subprocess_check(
            [sys.executable, "-c", code], timeout_s=30, sample_vram=False)
        self.assertEqual(result["status"], "PASS", result.get("reason"))

    def test_secret_equal_to_protocol_key_keeps_schema_intact(self):
        # P2: QBITTORRENT_PASSWORD=status 不得改写 JSON 协议键。
        from unittest.mock import patch

        results = [{"name": "x", "status": "PASS", "reason": "status ok", "detail": {}}]
        with patch.dict(os.environ, {"QBITTORRENT_PASSWORD": "status"}):
            doc = json.loads(self_check.render_report("full", results, as_json=True))
        self.assertIn("status", doc["results"][0])
        self.assertEqual(doc["results"][0]["status"], "PASS")
        self.assertEqual(doc["exit_code"], 0)

    @unittest.skipIf(sys.platform == "win32", "POSIX supervisor path")
    def test_missing_inner_interpreter_is_internal_error_not_model_fail(self):
        # P2: 内层 worker spawn 失败是自检基础设施错误(exit 3), 不是模型 FAIL。
        with self.assertRaises(self_check.SelfCheckInternalError):
            self_check.run_subprocess_check(
                ["/definitely/missing-interpreter-xyz"], timeout_s=30, sample_vram=False)

    def test_builtins_error_survives_plugin_errors_merge(self):
        # P3: <builtins> 错误不得被 plugin_host.errors() 覆盖。
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "plugins"
            (root / "broken").mkdir(parents=True)
            (root / "broken" / "__init__.py").write_text(
                "def register(registry):\n    raise RuntimeError('plugin boom')\n")
            manifest = Path(tmp) / "plugins.yaml"
            manifest.write_text("plugins:\n  - broken\n")
            with patch("spica.host.builtins.register_builtin_adapters",
                       side_effect=RuntimeError("builtin boom")):
                _registry, errors = self_check._tts_check_registry(
                    plugins_root=root, manifest_path=manifest)
        self.assertIn("<builtins>", errors)
        self.assertIn("broken", errors)

    def test_plugin_depending_on_sing_song_tool_works_like_production(self):
        # P2: 生产在插件前注册 host 闭包工具 -- 自检提供同 schema stub。
        from tempfile import TemporaryDirectory

        plugin = (
            "def register(registry):\n"
            "    assert 'sing_song' in registry.list_adapters('tool'), 'sing_song missing'\n"
            "    from agent_tools.tts.adapters import TextOnlyTTSAdapter\n"
            "    registry.register_tts('song_dep_voice',\n"
            "                          lambda config=None, service=None: TextOnlyTTSAdapter())\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "plugins"
            (root / "song_dep").mkdir(parents=True)
            (root / "song_dep" / "__init__.py").write_text(plugin)
            manifest = Path(tmp) / "plugins.yaml"
            manifest.write_text("plugins:\n  - song_dep\n")
            registry, errors = self_check._tts_check_registry(
                plugins_root=root, manifest_path=manifest)
        self.assertEqual(errors, {})
        self.assertIsNotNone(registry.resolve_tts("song_dep_voice", config={}, service=None))

    def test_sigterm_handler_installed_by_main(self):
        # P1: CI cancel/systemd stop 的 SIGTERM 必须走清理路径而非默认终止。
        import signal as signal_mod

        original = signal_mod.getsignal(signal_mod.SIGTERM)
        try:
            self_check.main([])  # 轻量档
            # 第十轮 P2: main 返回后必须恢复原 handler, 不污染调用进程
            self.assertIs(signal_mod.getsignal(signal_mod.SIGTERM), original)
            with self.assertRaises(KeyboardInterrupt):
                self_check._raise_keyboard_interrupt(signal_mod.SIGTERM, None)
        finally:
            signal_mod.signal(signal_mod.SIGTERM, original)


class SttOfficialValidatorTest(unittest.TestCase):
    def _app(self, model):
        from types import SimpleNamespace

        return SimpleNamespace(stt=SimpleNamespace(
            backend="faster_whisper", model=model, device="cuda",
            compute_type="float16", warmup_on_startup=True))

    def test_officially_invalid_ids_are_degraded(self):
        for model in ("org/name.", "org/na--me", "org/name.git", "org/.bad"):
            result = self_check.check_stt_light(self._app(model))
            self.assertEqual(result["status"], "DEGRADED", model)

    def test_officially_valid_underscore_ids_are_not_rejected(self):
        for model in ("_org/name", "org/_name"):
            result = self_check.check_stt_light(self._app(model))
            self.assertEqual(result["status"], "UNVERIFIED", model)

    def test_empty_local_dir_without_model_bin_is_degraded(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            result = self_check.check_stt_light(self._app(tmp))
        self.assertEqual(result["status"], "DEGRADED")




class TenthRoundFixesTest(unittest.TestCase):
    """第十轮修复回归钉。"""

    def test_stream_layer_no_longer_breaks_the_protocol_with_common_secrets(self):
        # P2-1 回归钉: password=PASS 时 marker JSON 不得被流层脱敏打碎。
        from unittest.mock import patch

        with patch.dict(os.environ, {"QBITTORRENT_PASSWORD": "PASS"}):
            result = self_check.run_subprocess_check(
                _fake_worker_cmd({"status": "PASS", "detail": {}}),
                timeout_s=30, sample_vram=False)
        self.assertEqual(result["status"], "PASS", result.get("reason"))

    def test_multiline_secret_in_stderr_is_fully_redacted(self):
        # P1: dotenv 1.2.x 支持引号内换行 -- 跨行 secret 两段都不得进 reason。
        from unittest.mock import patch

        canary = "LEFT-CANARY-9x7\nRIGHT-CANARY-3z1"
        code = ("import sys\n"
                f"print({canary!r}, file=sys.stderr)\n"
                "sys.exit(5)\n")
        with patch.dict(os.environ, {"OPENAI_API_KEY": canary}):
            result = self_check.run_subprocess_check(
                [sys.executable, "-c", code], timeout_s=30, sample_vram=False)
        self.assertEqual(result["status"], "FAIL")
        self.assertNotIn("LEFT-CANARY-9x7", result["reason"])
        self.assertNotIn("RIGHT-CANARY-3z1", result["reason"])

    def test_escaped_variants_of_secrets_are_redacted(self):
        # P1: repr/unicode_escape 与 JSON 转义形式同样能还原 secret。
        canary = 'esc"CANARY\\tail'
        escaped = canary.encode("unicode_escape").decode("ascii")
        text = f"raw={canary} repr={escaped}"
        out = self_check.redact_secrets(text, environ={"JUDGE_API_KEY": canary})
        self.assertNotIn(canary, out)
        self.assertNotIn(escaped, out)

    @unittest.skipIf(sys.platform == "win32", "Linux 上测 Windows 控制流")
    def test_windows_cleanup_control_flow_never_raises(self):
        # P2-9: taskkill 超时/Job API 异常都不得越过 cleanup 契约(fake 控制流;
        # 真实 Job 内核语义仍属 Windows waiver)。
        from types import SimpleNamespace
        from unittest.mock import patch

        proc = SimpleNamespace(pid=12345, wait=lambda timeout=None: 0)
        with patch.object(self_check.os, "name", "nt"):
            with patch.object(self_check.subprocess, "run",
                              side_effect=self_check.subprocess.TimeoutExpired("taskkill", 15)):
                ok, note = self_check._cleanup_tree(proc, None)
            self.assertFalse(ok)
            self.assertIn("taskkill 兜底失败", note)
            ok2, note2 = self_check._cleanup_tree(proc, object())  # windll 缺失
            self.assertFalse(ok2)
            self.assertIn("失败", note2)


if __name__ == "__main__":
    unittest.main()
