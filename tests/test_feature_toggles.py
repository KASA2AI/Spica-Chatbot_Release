"""Feature toggles (VRAM switches) + the 2026-07 review-P1 fixes.

What is pinned here:
- ``TtsConfig`` (new typed section): default enabled; the text_only assembly
  pieces (adapter behaviour + factory branch + registry chain). text_only must
  expose NO warmup surface -- run_warmup's "无需预热" branch -- while the
  adapter object itself stays non-None (a None tts_adapter makes qt_overlay
  skip the whole startup warmup worker AND the dangling-session recovery
  chained on it).
- ``SttConfig.backend`` Literal: a typo fails LOUD at config load instead of
  silently assembling no local adapter and falling back to online Google.
- ``song_enabled``: STRICT boolean read of the untyped song master switch
  (the string "false" must never read as enabled).
- Supply gates are NOT execution gates (tools.run never re-checks
  ``available``): sing_song dies in the host closure with zero search / zero
  events; inspect_screen / watch_game_screen die BEFORE any capture.
- ``_warmup_stt`` honours stt.warmup_on_startup (the flag existed in SttConfig
  but was never wired); faster-whisper warmup drains the LAZY segments
  generator (a warmup that doesn't iterate never runs the decode path).
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from agent_tools.function_tools.screen.config import ScreenPipelineConfig
from agent_tools.function_tools.screen.schema import ScreenToolError
from agent_tools.function_tools.song.config import song_enabled
from agent_tools.tts.adapters import TextOnlyTTSAdapter
from agent_tools.tts.manager import build_tts_adapter
from agent_tools.tts.schemas import TTSRequest
from spica.config.manager import ConfigManager
from spica.config.schema import AppConfig, SttConfig, TtsConfig
from spica.host.app_host import AppHost
from spica.host.builtins import register_builtin_adapters
from spica.host.warmup import _warmup_stt
from spica.plugins.registry import CapabilityRegistry


def _schema_name(schema: dict) -> str:
    name = schema.get("name")
    if isinstance(name, str) and name:
        return name
    function = schema.get("function")
    if isinstance(function, dict):
        nested = function.get("name")
        if isinstance(nested, str) and nested:
            return nested
    return ""


def _screen_cfg(enabled: bool) -> ScreenPipelineConfig:
    return ScreenPipelineConfig(
        enabled=enabled, provider="moondream_local", model_id="m", revision="r",
        device="cuda", dtype="bfloat16", max_side=768, reasoning=False,
        preload=False, ocr_enabled=False, ocr_engine="rapidocr",
        capture_format="png", infer_timeout_sec=30.0, log_timing=False,
        debug_save_images=False,
    )


class TtsConfigTest(unittest.TestCase):
    def test_default_enabled(self):
        self.assertTrue(TtsConfig().enabled)
        self.assertTrue(AppConfig().tts.enabled)

    def test_yaml_can_disable(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.yaml"
            path.write_text("tts:\n  enabled: false\n", encoding="utf-8")
            self.assertFalse(ConfigManager(path).load().tts.enabled)


class SttBackendLiteralTest(unittest.TestCase):
    def test_default_faster_whisper(self):
        self.assertEqual(SttConfig().backend, "faster_whisper")

    def test_google_is_the_explicit_online_opt_out(self):
        self.assertEqual(SttConfig(backend="google").backend, "google")

    def test_typo_fails_loud(self):
        # Pre-fix a typo VALIDATED fine and silently assembled no local adapter
        # (= silent online-Google fallback). Now it dies at load, like mic_backend.
        with self.assertRaises(ValidationError):
            SttConfig(backend="fastwhisper")

    def test_typo_fails_loud_through_the_manager(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.yaml"
            path.write_text("stt:\n  backend: whisperx\n", encoding="utf-8")
            with self.assertRaises(ValidationError):
                ConfigManager(path).load()


class TextOnlyTTSAdapterTest(unittest.TestCase):
    def test_ok_result_with_no_audio(self):
        result = TextOnlyTTSAdapter().synthesize(TTSRequest(text="こんばんは", emotion="happy"))
        self.assertTrue(result.ok)
        self.assertIsNone(result.audio_path)
        self.assertIsNone(result.audio_url)
        self.assertIsNone(result.error)

    def test_factory_branch(self):
        self.assertIsInstance(build_tts_adapter({"provider": "text_only"}), TextOnlyTTSAdapter)

    def test_registry_chain(self):
        registry = CapabilityRegistry()
        register_builtin_adapters(registry)
        adapter = registry.resolve_tts(
            "text_only", config={"provider": "text_only"}, service=None
        )
        self.assertIsInstance(adapter, TextOnlyTTSAdapter)

    def test_tts_slice_registration_is_self_sufficient(self):
        # 提交前复核 P2: self_check 的 TTS worker 只注册 builtins 的 TTS 切片
        # (register_tts_providers), 无关内建件的构造异常不得殃及 TTS 检查——
        # 该切片必须独立可用且与 register_builtin_adapters 同源。
        from spica.host.builtins import register_tts_providers

        registry = CapabilityRegistry()
        register_tts_providers(registry)
        adapter = registry.resolve_tts(
            "text_only", config={"provider": "text_only"}, service=None
        )
        self.assertIsInstance(adapter, TextOnlyTTSAdapter)

    def test_no_warmup_surface_so_run_warmup_skips_gracefully(self):
        adapter = TextOnlyTTSAdapter()
        self.assertFalse(hasattr(adapter, "public_config"))
        self.assertFalse(hasattr(adapter, "warmup"))


class SongEnabledStrictTest(unittest.TestCase):
    def test_real_booleans(self):
        self.assertTrue(song_enabled({"enabled": True}))
        self.assertFalse(song_enabled({"enabled": False}))

    def test_missing_defaults_enabled(self):
        self.assertTrue(song_enabled({}))
        self.assertTrue(song_enabled(None))

    def test_true_false_strings(self):
        self.assertTrue(song_enabled({"enabled": "true"}))
        self.assertTrue(song_enabled({"enabled": "True"}))
        self.assertFalse(song_enabled({"enabled": "false"}))
        self.assertFalse(song_enabled({"enabled": " FALSE "}))

    def test_garbage_reads_disabled_never_enabled(self):
        # bool("false") is True -- the exact bug class this helper kills. Any
        # unrecognized value is a config mistake and must fail SAFE (disabled).
        for garbage in ("1", "yes", "on", 1, 0, [], {}, 3.14):
            self.assertFalse(song_enabled({"enabled": garbage}), repr(garbage))


class SingSongDisabledTest(unittest.TestCase):
    def setUp(self):
        self.host = AppHost()

    def _offered(self) -> set[str]:
        return {_schema_name(s) for s in self.host.registry.tool_schemas()}

    def test_offered_by_default(self):
        self.assertIn("sing_song", self._offered())

    def test_not_offered_when_disabled(self):
        self.host.song_config = {**self.host.song_config, "enabled": False}
        self.assertNotIn("sing_song", self._offered())

    def test_closure_hard_refuses_zero_search_zero_events(self):
        # ``available`` only filters schema supply; tools.run never re-checks it.
        # A forced/hallucinated call must die in the authority-holding closure
        # BEFORE the netease search and BEFORE any SongRequestEvent.
        self.host.song_config = {**self.host.song_config, "enabled": False}
        search_calls: list = []
        events: list = []
        self.host._song_search = lambda *a, **k: search_calls.append((a, k))
        self.host.companion_sink = lambda event: events.append(event)
        with self.assertRaises(ScreenToolError) as ctx:
            self.host._request_song("random song")
        self.assertEqual(ctx.exception.code, "SONG_DISABLED")
        self.assertEqual(search_calls, [])
        self.assertEqual(events, [])


class InspectScreenDisabledTest(unittest.TestCase):
    def test_not_offered_when_disabled(self):
        registry = CapabilityRegistry()
        register_builtin_adapters(registry, screen_config=_screen_cfg(False))
        offered = {_schema_name(s) for s in registry.tool_schemas()}
        self.assertNotIn("inspect_screen", offered)

    def test_offered_when_enabled(self):
        registry = CapabilityRegistry()
        register_builtin_adapters(registry, screen_config=_screen_cfg(True))
        offered = {_schema_name(s) for s in registry.tool_schemas()}
        self.assertIn("inspect_screen", offered)

    def test_hard_refuses_before_any_capture(self):
        import spica.adapters.tools.screen as screen_module
        from spica.adapters.tools import InspectScreenTool

        analysis = MagicMock()
        tool = InspectScreenTool(analysis, config=_screen_cfg(False))
        with patch.object(screen_module, "is_screen_intent_explicit", return_value=True), \
                patch.object(
                    screen_module, "capture_full_screen",
                    side_effect=AssertionError("disabled state must not capture"),
                ):
            with self.assertRaises(ScreenToolError) as ctx:
                tool.run(target="full_screen", question="看看我的屏幕")
        self.assertEqual(ctx.exception.code, "SCREEN_DISABLED")
        analysis.analyze_image.assert_not_called()


class WatchGameScreenDisabledTest(unittest.TestCase):
    def test_hard_refuses_before_any_window_capture(self):
        from spica.adapters.tools.watch_game_screen import WatchGameScreenTool
        from spica.galgame.session import GalgameState

        capture = MagicMock()
        context = SimpleNamespace(
            target=SimpleNamespace(owner_domain="galgame", game_id="g1", window_id="w1"),
            locator=MagicMock(),
            capture=capture,
            state=GalgameState.PLAYING,  # passes the privacy state gate: the
            # refusal under test must come from screen.enabled, nothing earlier
        )
        analysis = MagicMock()
        tool = WatchGameScreenTool(analysis, lambda: context, config=_screen_cfg(False))
        with self.assertRaises(ScreenToolError) as ctx:
            tool.run(question="她现在什么表情")
        self.assertEqual(ctx.exception.code, "SCREEN_DISABLED")
        capture.capture_rect.assert_not_called()
        analysis.analyze_image.assert_not_called()


class TtsAssemblyGateTest(unittest.TestCase):
    """AppHost 装配级 TTS 门（经 ``_resolve_tts_assembly`` 测试缝——initialize()
    内的真实装配路径就是这一个调用）。"""

    def _host(self, enabled: bool) -> AppHost:
        host = AppHost()
        host.config = AppConfig()
        host.config.tts.enabled = enabled
        return host

    def test_disabled_assembles_text_only_with_no_gptsovits_tool(self):
        with patch("spica.host.app_host.GPTSoVITSTool") as tool_ctor:
            provider, tool, adapter = self._host(False)._resolve_tts_assembly(
                {"provider": "gptsovits_current"}
            )
        self.assertEqual(provider, "text_only")
        self.assertIsNone(tool)
        tool_ctor.assert_not_called()  # GPT-SoVITS constructor NEVER invoked
        self.assertIsInstance(adapter, TextOnlyTTSAdapter)

    def test_disabled_ignores_a_plugin_override_of_text_only(self):
        # 第六轮 review P2: 插件可向 registry 注册同名 "text_only" -- 关闭 TTS 的
        # 无模型保证不允许被任何 registry 状态推翻, disabled 必须直构真的
        # TextOnlyTTSAdapter, 而不是从可覆盖的 registry resolve。
        class _HeavyFakeAdapter:
            name = "heavy_fake"

        host = self._host(False)
        host.registry = CapabilityRegistry()
        host.registry.register_tts(
            "text_only", lambda config=None, service=None: _HeavyFakeAdapter()
        )
        provider, tool, adapter = host._resolve_tts_assembly(
            {"provider": "gptsovits_current"}
        )
        self.assertEqual(provider, "text_only")
        self.assertIsNone(tool)
        self.assertIsInstance(adapter, TextOnlyTTSAdapter)

    def test_initialize_routes_through_both_assembly_seams(self):
        # facade-on-path pin: 抽出的两个测试缝必须真的在 initialize() 的路径上,
        # 否则「seam 存在但没人调」就是假绿(仿 PatchValidityTest 的用意, AST 版)。
        import ast

        from spica.host import app_host as app_host_module

        tree = ast.parse(Path(app_host_module.__file__).read_text(encoding="utf-8"))
        cls = next(n for n in tree.body
                   if isinstance(n, ast.ClassDef) and n.name == "AppHost")
        init = next(n for n in cls.body
                    if isinstance(n, ast.FunctionDef) and n.name == "initialize")
        calls = {node.func.attr for node in ast.walk(init)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertIn("_resolve_tts_assembly", calls)
        self.assertIn("_install_moondream_seam", calls)

    def test_enabled_assembles_the_configured_provider(self):
        provider, tool, adapter = self._host(True)._resolve_tts_assembly(
            {"provider": "gptsovits_current"}
        )
        self.assertEqual(provider, "gptsovits_current")
        self.assertIsNotNone(tool)
        self.assertNotIsInstance(adapter, TextOnlyTTSAdapter)


class MoondreamSeamGateTest(unittest.TestCase):
    """AppHost 装配级 Moondream 门（经 ``_install_moondream_seam`` 测试缝）。"""

    def tearDown(self):
        from agent_tools.function_tools.screen.backends.moondream_runtime import (
            set_active_moondream_provider,
        )

        set_active_moondream_provider(None)

    def test_enabled_installs_the_built_provider(self):
        from agent_tools.function_tools.screen.backends import moondream_runtime

        host = AppHost()
        host.screen_config = _screen_cfg(True)
        sentinel = object()
        with patch("spica.host.app_host.build_moondream_provider", return_value=sentinel):
            host._install_moondream_seam()
        self.assertIs(moondream_runtime.get_active_moondream_provider(), sentinel)

    def test_disabled_clears_seam_and_manager(self):
        from agent_tools.function_tools.screen import model_manager as mm
        from agent_tools.function_tools.screen.backends import moondream_runtime

        host = AppHost()
        host.screen_config = _screen_cfg(False)
        moondream_runtime.set_active_moondream_provider(object())  # 遗留 seam
        with patch.object(mm, "clear_moondream_manager") as clear:
            host._install_moondream_seam()
        self.assertIsNone(moondream_runtime.get_active_moondream_provider())
        clear.assert_called_once()

    def test_enabled_local_provider_overwrites_a_stale_hf_seam(self):
        # 第四轮 review: 同进程 hf->local 切换时, local factory 返回 None 也必须
        # 覆盖旧 seam, 否则 local 配置继续路由给旧 hf provider 并因不匹配失败。
        from agent_tools.function_tools.screen.backends import moondream_runtime

        host = AppHost()
        host.screen_config = _screen_cfg(True)
        moondream_runtime.set_active_moondream_provider(object())  # 旧 hf seam
        with patch("spica.host.app_host.build_moondream_provider", return_value=None):
            host._install_moondream_seam()
        self.assertIsNone(moondream_runtime.get_active_moondream_provider())


class MoondreamClearRaceTest(unittest.TestCase):
    """clear_moondream_manager vs 进行中 load 的竞态（2026-07 review P2）：
    clear 必须等 in-flight load 完成、丢弃其结果，并让旧引用上的再次 load 拒绝
    ——否则同进程 enabled=true→false 重装配后旧 manager 会重新持有模型/显存。"""

    def test_clear_waits_for_inflight_load_and_retires_the_manager(self):
        import threading

        from agent_tools.function_tools.screen import model_manager as mm

        entered = threading.Event()   # load 确实已进入(持有 _load_lock)
        release = threading.Event()

        def fake_load(config):
            entered.set()
            release.wait(timeout=10)
            return object()  # the "model backend" the race would leak

        with patch.object(mm, "load_moondream_backend", side_effect=fake_load), \
                patch.object(mm.MoondreamModelManager, "_validate_config", lambda self: None), \
                patch.object(mm.MoondreamModelManager, "_assert_cuda_available",
                             lambda self: None):
            try:
                manager = mm.get_moondream_manager(_screen_cfg(True))

                def _load_swallow():
                    try:
                        manager.load()
                    except Exception:
                        pass

                loader = threading.Thread(target=_load_swallow)
                loader.start()
                self.assertTrue(entered.wait(timeout=10))  # 事件同步, 不靠 sleep
                cleaner = threading.Thread(target=mm.clear_moondream_manager)
                cleaner.start()
                cleaner.join(timeout=0.3)
                # 修复语义: clear 在 load 完成前必须还在等(旧代码这里已经清完了)
                self.assertTrue(cleaner.is_alive())
                release.set()
                loader.join(10)
                cleaner.join(10)
                self.assertFalse(cleaner.is_alive())
                self.assertFalse(manager.is_ready())  # in-flight 结果被丢弃
                with self.assertRaises(Exception):
                    manager.load()  # retired: 旧引用不能再把模型拉回来
            finally:
                release.set()
                mm.clear_moondream_manager()

    def test_reset_close_waits_for_inflight_inference(self):
        # 第六轮 review P2: reset(close) 只拿 load/state 锁, 不等 in-flight 推理
        # -- 旧 backend 在 manager 已 closed/unloaded 后仍在推理。
        import threading

        from agent_tools.function_tools.screen import model_manager as mm

        entered = threading.Event()
        release = threading.Event()

        class _Backend:
            def query(self, image, prompt):
                entered.set()
                release.wait(timeout=10)
                return SimpleNamespace(text="ok")

        with patch.object(mm, "load_moondream_backend", return_value=_Backend()), \
                patch.object(mm.MoondreamModelManager, "_validate_config", lambda self: None), \
                patch.object(mm.MoondreamModelManager, "_assert_cuda_available",
                             lambda self: None), \
                patch.object(mm.MoondreamModelManager, "_prepare_image",
                             lambda self, image: image):
            manager = mm.MoondreamModelManager(_screen_cfg(True))
            manager.load()
            outcomes: list = []

            def _query():
                try:
                    outcomes.append(manager.query(object(), "q"))
                except Exception as exc:  # noqa: BLE001
                    outcomes.append(exc)

            infer = threading.Thread(target=_query)
            infer.start()
            self.assertTrue(entered.wait(timeout=10))
            closer = threading.Thread(target=lambda: manager.reset(close=True))
            closer.start()
            closer.join(timeout=0.3)
            self.assertTrue(closer.is_alive())  # reset 必须等推理完(旧代码已返回)
            release.set()
            infer.join(10)
            closer.join(10)
            self.assertEqual(outcomes, ["ok"])  # 已开始的推理完整收尾
            self.assertEqual(manager.get_status(), mm.STATUS_UNLOADED)

    def test_query_paused_before_infer_lock_refuses_after_close(self):
        # 第六轮 review P2 的第二形态: query 在 _infer_lock 外已拿到旧 backend
        # 后暂停, reset(close) 完成 -- 排队的推理必须拒绝, 绝不再碰旧 backend,
        # 失败也不得把 closed manager 状态改写成 error。
        import threading

        from agent_tools.function_tools.screen import model_manager as mm

        paused = threading.Event()
        resume = threading.Event()
        backend_calls: list = []

        class _Backend:
            def query(self, image, prompt):
                backend_calls.append(1)
                return SimpleNamespace(text="ok")

        def slow_prompt(self, question, reasoning=False):
            paused.set()
            resume.wait(timeout=10)
            return "prompt"

        with patch.object(mm, "load_moondream_backend", return_value=_Backend()), \
                patch.object(mm.MoondreamModelManager, "_validate_config", lambda self: None), \
                patch.object(mm.MoondreamModelManager, "_assert_cuda_available",
                             lambda self: None), \
                patch.object(mm.MoondreamModelManager, "_prepare_image",
                             lambda self, image: image), \
                patch.object(mm.MoondreamModelManager, "_build_prompt", slow_prompt):
            manager = mm.MoondreamModelManager(_screen_cfg(True))
            manager.load()
            errors: list = []

            def _query():
                try:
                    manager.query(object(), "q")
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            t = threading.Thread(target=_query)
            t.start()
            self.assertTrue(paused.wait(timeout=10))
            manager.reset(close=True)  # infer 锁此刻空闲 -> reset 立即完成
            resume.set()
            t.join(10)
            self.assertEqual(backend_calls, [])  # 旧 backend 绝不再被推理
            self.assertEqual(len(errors), 1)
            self.assertEqual(manager.get_status(), mm.STATUS_UNLOADED)  # 未被改写成 error

    def test_close_request_visible_to_queued_inference_before_reset_waits(self):
        # 第七轮 review P2 钉序: q1(已在推理) → close requested → q2(已排队未进
        # backend)必须拒绝 → reset 等 q1 收尾后返回。closing 状态必须在 reset
        # 开始等 infer 锁**之前**就对排队者可见, 不依赖 RLock 公平性。
        import threading
        import time

        from agent_tools.function_tools.screen import model_manager as mm

        entered1 = threading.Event()
        release1 = threading.Event()
        paused2 = threading.Event()
        resume2 = threading.Event()
        backend_calls: list = []

        class _Backend:
            def query(self, image, prompt):
                backend_calls.append(prompt)
                entered1.set()
                release1.wait(timeout=10)
                return SimpleNamespace(text="ok")

        def gated_prompt(self, question, reasoning=False):
            if question == "q2":
                paused2.set()
                resume2.wait(timeout=10)
            return question

        with patch.object(mm, "load_moondream_backend", return_value=_Backend()), \
                patch.object(mm.MoondreamModelManager, "_validate_config", lambda self: None), \
                patch.object(mm.MoondreamModelManager, "_assert_cuda_available",
                             lambda self: None), \
                patch.object(mm.MoondreamModelManager, "_prepare_image",
                             lambda self, image: image), \
                patch.object(mm.MoondreamModelManager, "_build_prompt", gated_prompt):
            manager = mm.MoondreamModelManager(_screen_cfg(True))
            manager.load()
            outcomes: dict = {}

            def _query(tag):
                try:
                    outcomes[tag] = manager.query(object(), tag)
                except Exception as exc:  # noqa: BLE001
                    outcomes[tag] = exc

            q1 = threading.Thread(target=_query, args=("q1",))
            q1.start()
            self.assertTrue(entered1.wait(timeout=10))     # ① q1 已在推理
            q2 = threading.Thread(target=_query, args=("q2",))
            q2.start()
            self.assertTrue(paused2.wait(timeout=10))      # ② q2 已排队(load 之后)
            closer = threading.Thread(target=lambda: manager.reset(close=True))
            closer.start()                                  # ③ close requested
            deadline = time.time() + 2
            visible = False
            while time.time() < deadline:
                if manager.get_status_details().get("closed"):
                    visible = True
                    break
                time.sleep(0.02)
            self.assertTrue(visible, "closing 必须在 reset 等锁期间就可见")
            self.assertTrue(closer.is_alive())              # reset 仍在等 q1
            resume2.set()                                   # q2 去抢 infer 锁
            release1.set()                                  # q1 收尾
            q1.join(10)
            q2.join(10)
            closer.join(10)
            self.assertEqual(outcomes["q1"], "ok")          # q1 完整结束
            self.assertIsInstance(outcomes["q2"], Exception)  # ④ q2 被拒绝
            self.assertEqual(backend_calls, ["q1"])         # 旧 backend 只见过 q1
            self.assertEqual(manager.get_status(), mm.STATUS_UNLOADED)

    def test_reset_close_is_bounded_when_inference_hangs(self):
        # 第八轮 review P2: Q1 backend 永不返回 -> reset(close=True) 无限等
        # infer 锁, enabled->disabled 重装配挂死。等待必须有界并如实告警。
        import threading

        from agent_tools.function_tools.screen import model_manager as mm

        entered = threading.Event()
        hang_forever = threading.Event()  # 故意永不 set

        class _HungBackend:
            def query(self, image, prompt):
                entered.set()
                hang_forever.wait(timeout=60)
                return SimpleNamespace(text="late")

        import dataclasses

        cfg = dataclasses.replace(_screen_cfg(True), infer_timeout_sec=0.2)  # 缩短有界等待
        with patch.object(mm, "load_moondream_backend", return_value=_HungBackend()), \
                patch.object(mm.MoondreamModelManager, "_validate_config", lambda self: None), \
                patch.object(mm.MoondreamModelManager, "_assert_cuda_available",
                             lambda self: None), \
                patch.object(mm.MoondreamModelManager, "_prepare_image",
                             lambda self, image: image):
            manager = mm.MoondreamModelManager(cfg)
            manager.load()
            hung = threading.Thread(
                target=lambda: self._swallow(lambda: manager.query(object(), "q")),
                daemon=True)
            hung.start()
            self.assertTrue(entered.wait(timeout=10))
            import time as time_mod

            started = time_mod.time()
            manager.reset(close=True)  # 不得无限阻塞
            elapsed = time_mod.time() - started
            self.assertLess(elapsed, 10, f"reset blocked {elapsed:.1f}s on hung inference")
            self.assertEqual(manager.get_status(), mm.STATUS_UNLOADED)
            hang_forever.set()

    @staticmethod
    def _swallow(fn):
        try:
            fn()
        except Exception:
            pass


    def test_slow_load_cannot_revive_a_closed_manager(self):
        # 第九轮 P1: load 持锁超过 reset 超时 -> reset 返回 closed/unloaded ->
        # load 完成后不得把 backend 写回(模型/显存复活)。
        import dataclasses
        import threading

        from agent_tools.function_tools.screen import model_manager as mm

        entered = threading.Event()
        release = threading.Event()

        def slow_backend(config):
            entered.set()
            release.wait(timeout=30)
            return object()

        cfg = dataclasses.replace(_screen_cfg(True), infer_timeout_sec=0.2)
        with patch.object(mm, "load_moondream_backend", side_effect=slow_backend), \
                patch.object(mm.MoondreamModelManager, "_validate_config", lambda self: None), \
                patch.object(mm.MoondreamModelManager, "_assert_cuda_available",
                             lambda self: None):
            manager = mm.MoondreamModelManager(cfg)
            outcomes: list = []

            def _load():
                try:
                    manager.load()
                    outcomes.append("loaded")
                except Exception as exc:  # noqa: BLE001
                    outcomes.append(type(exc).__name__)

            loader = threading.Thread(target=_load)
            loader.start()
            self.assertTrue(entered.wait(timeout=10))
            manager.reset(close=True)  # 有界超时后强制清态返回
            release.set()
            loader.join(10)
            self.assertFalse(loader.is_alive(), "loader 未结束(死锁?)")  # 防假绿
            self.assertEqual(outcomes, ["ScreenToolError"])  # 明确 CLEARED 丢弃
            self.assertFalse(manager.is_ready(), "closed manager 被慢 load 复活")
            self.assertEqual(manager.get_status(), mm.STATUS_UNLOADED)

    def test_infinite_infer_timeout_does_not_crash_reset(self):
        # 第九轮 P2: inf/1e308 直接传 RLock.acquire 会 OverflowError。
        import dataclasses

        from agent_tools.function_tools.screen import model_manager as mm

        cfg = dataclasses.replace(_screen_cfg(True), infer_timeout_sec=float("inf"))
        manager = mm.MoondreamModelManager(cfg)
        manager.reset(close=True)  # 不得抛 OverflowError
        self.assertEqual(manager.get_status(), mm.STATUS_UNLOADED)

    def test_preload_async_on_a_retired_manager_fails_cleanly(self):
        # 第四轮 review P3: 已关闭的旧 manager 调 preload_async 不得再创建
        # executor / 把状态翻成 loading -- 直接返回 failed future。
        from agent_tools.function_tools.screen import model_manager as mm

        manager = mm.MoondreamModelManager(_screen_cfg(True))
        manager.reset(close=True)
        future = manager.preload_async()
        self.assertTrue(future.done())
        with self.assertRaises(Exception):
            future.result()
        self.assertIsNone(manager._executor)  # 生命周期保持干净
        self.assertEqual(manager.get_status(), mm.STATUS_UNLOADED)


class SttWarmupFlagTest(unittest.TestCase):
    """stt.warmup_on_startup existed in SttConfig from day one but _warmup_stt
    never read it -- the model warmed on every startup regardless."""

    def _adapter(self):
        calls: list = []

        class _Adapter:
            def warmup(self):
                calls.append(1)
                return {"ok": True, "duration_ms": 5}

        return _Adapter(), calls

    def test_flag_false_skips_the_model_load(self):
        adapter, calls = self._adapter()
        events: list = []
        _warmup_stt(adapter, lambda s, m: events.append((s, m)), warmup_on_startup=False)
        self.assertEqual(calls, [])
        self.assertEqual(events[-1][0], "ready")
        self.assertIn("已关闭", events[-1][1])

    def test_flag_true_warms(self):
        adapter, calls = self._adapter()
        _warmup_stt(adapter, lambda s, m: None, warmup_on_startup=True)
        self.assertEqual(calls, [1])

    def test_app_host_wires_the_config_flag(self):
        adapter, calls = self._adapter()
        host = AppHost()
        host.chat_engine = SimpleNamespace(model="gpt-x")
        host.tts_adapter = SimpleNamespace(name="dummy")  # no warmup surface
        host.stt_adapter = adapter
        host.config = AppConfig()  # bare host has config=None until initialize()
        host.config.stt.warmup_on_startup = False
        host.warmup(lambda s, m: None)
        self.assertEqual(calls, [])
        host.config.stt.warmup_on_startup = True
        host.warmup(lambda s, m: None)
        self.assertEqual(calls, [1])


class FasterWhisperWarmupDrainTest(unittest.TestCase):
    def test_warmup_iterates_the_lazy_segments_generator(self):
        # faster-whisper's transcribe() returns a LAZY generator; encode/decode
        # only run while iterating. A warmup that never iterates only loaded
        # weights while claiming to have warmed the decode path.
        from spica.adapters.stt.faster_whisper import FasterWhisperAdapter

        consumed: list = []

        def _lazy_segments():
            consumed.append(True)
            yield SimpleNamespace(text="")

        adapter = FasterWhisperAdapter(
            model="x", device="cpu", compute_type="int8", language="zh"
        )
        fake_model = SimpleNamespace(
            transcribe=lambda *a, **k: (_lazy_segments(), SimpleNamespace())
        )
        adapter._ensure_model = lambda: fake_model
        result = adapter.warmup()
        self.assertTrue(result["ok"])
        self.assertEqual(consumed, [True])


if __name__ == "__main__":
    unittest.main()
