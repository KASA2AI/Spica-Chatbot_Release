"""Phase 1 smoke test for the AppHost composition root.

Verifies the new package root imports, the management-surface placeholder, and
that ``conversation_surface`` aliases the held agent. ``initialize()`` is NOT
called here -- it constructs the real SimpleAgent/TTS/Visual stack which needs
runtime config and credentials; full wiring is covered by the manual launch and
the Phase 0 golden suite.
"""

import unittest

from spica.host.app_host import AppHost


class AppHostSmokeTest(unittest.TestCase):
    def test_package_root_imports_with_empty_services(self):
        host = AppHost()
        self.assertIsNone(host.chat_engine)
        self.assertIsNone(host.services)
        self.assertIsNone(host.visual_tool)
        self.assertIsNone(host.tts_adapter)
        self.assertIsNone(host.tts_tool)

    def test_conversation_surface_is_chat_engine(self):
        host = AppHost()
        self.assertIsNone(host.conversation_surface)  # before initialize()
        sentinel = object()
        host.chat_engine = sentinel
        self.assertIs(host.conversation_surface, sentinel)

    def test_management_surface_lists_builtin_adapters(self):
        # Phase 8: management_surface is implemented; before initialize() it
        # already exposes the registry's built-in adapters and no plugins.
        host = AppHost()
        ms = host.management_surface
        self.assertIn("openai_compatible", ms.list_adapters("llm"))
        self.assertIn("sqlite", ms.list_adapters("memory"))
        self.assertEqual(ms.list_plugins(), [])


class CompanionControllerAccessorTest(unittest.TestCase):
    """Stage 2: the host-side seam pieces. The initialize() wiring line itself
    (set_game_binding_provider call) is NOT unit-testable without a full
    initialize -- covered by diff review + the --ask real-machine probe; the
    mechanism on both sides is unit-tested (here + test_chat_engine_game_binding)."""

    @staticmethod
    def _host_with_fake_services() -> AppHost:
        from types import SimpleNamespace

        from spica.config.schema import AppConfig

        host = AppHost()
        host.config = AppConfig()
        host.services = SimpleNamespace(
            game_memory_adapter=object(),
            screen_capture_adapter=object(),
            window_locator_adapter=object(),
            ocr_adapter=object(),
            llm_adapter=None,  # -> _new_summarizer() returns None
        )
        return host

    def test_companion_controller_accessor_caches(self):
        host = self._host_with_fake_services()
        first = host.companion_controller()
        self.assertIs(host.companion_controller(), first)  # singleton
        self.assertIsNot(host.new_companion_controller(), first)  # builder stays fresh

    def test_companion_game_binding_none_without_controller(self):
        host = AppHost()  # no controller ever built
        self.assertIsNone(host._companion_game_binding())

    def test_companion_game_binding_passes_through(self):
        from types import SimpleNamespace

        host = AppHost()
        sentinel = object()
        host._companion_controller = SimpleNamespace(current_game_context=lambda: sentinel)
        self.assertIs(host._companion_game_binding(), sentinel)


class CompanionSinkOrderingTest(unittest.TestCase):
    """Stage 3 attach ordering: a sink attached BEFORE the first
    companion_controller() construction is the one the controller's session emits
    through (the controller binds host.companion_sink at build time)."""

    def test_attach_before_accessor_routes_events_through_sink(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace

        from PIL import Image

        from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
        from spica.config.schema import AppConfig
        from spica.ports.ocr import OcrResult
        from spica.ports.screen_capture import CaptureImage
        from spica.ports.window_locator import WindowGeometry, WindowSafetyResult

        class _Locator:
            def get_window_geometry(self, window_id):
                return WindowGeometry(0, 0, 100, 100)

            def check_safety(self, window_id, rule, overlay_window_id=None):
                return WindowSafetyResult(ok=True)

        class _Capture:
            def capture_rect(self, left, top, width, height):
                img = Image.new("RGB", (max(1, width), max(1, height)), (30, 30, 30))
                return CaptureImage(image=img, width=img.width, height=img.height)

        class _InertOcr:
            def recognize(self, image):
                return OcrResult(text="")

        with TemporaryDirectory() as tmp:
            host = AppHost()
            host.config = AppConfig()
            host.services = SimpleNamespace(
                game_memory_adapter=GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3"),
                screen_capture_adapter=_Capture(),
                window_locator_adapter=_Locator(),
                ocr_adapter=_InertOcr(),
                llm_adapter=None,  # no summarizer -> stop() is fast
            )
            received = []
            host.attach_companion_sink(received.append)  # BEFORE first accessor use
            controller = host.companion_controller()
            controller.start("0x1", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
            controller.stop()
            kinds = [getattr(event, "kind", "") for event in received]
            self.assertIn("galgame_status_changed", kinds)  # flowed through OUR sink
            self.assertIn("galgame_summary_done", kinds)  # end() leg too


class HistoryBridgeTest(unittest.TestCase):
    """B 方案 (FINDINGS #15): _record_play_history upserts into the character's
    DEFAULT-scope memory; same game overwrites (explicit memory_key), different
    games coexist -- the approved coverage policy, guarded against a REAL store."""

    def _host_with_store(self, tmp):
        from pathlib import Path
        from types import SimpleNamespace

        from memory.store import SQLiteMemoryStore
        from spica.config.schema import AppConfig

        host = AppHost()
        host.config = AppConfig()
        host.services = SimpleNamespace(memory_store=SQLiteMemoryStore(Path(tmp) / "memory.sqlite3"))
        return host

    def test_record_lands_in_default_scope_with_game_key(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            host = self._host_with_store(tmp)
            host._record_play_history("limelight", "麦和我一起玩了游戏《LimeLight》。")
            rows = host.services.memory_store.list_memories("spica::default")
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["memory_key"], "galgame_history:limelight")
            self.assertEqual(row["scope"], "relationship")
            self.assertEqual(row["memory_type"], "experience")
            self.assertEqual(row["source"], "galgame_companion")
            self.assertAlmostEqual(row["importance"], 0.85)
            self.assertFalse(row["pinned"])

    def test_same_game_overwrites_different_game_coexists(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            host = self._host_with_store(tmp)
            host._record_play_history("limelight", "第一次游玩的履历卡。")
            host._record_play_history("limelight", "第二次游玩的履历卡（更新）。")
            rows = host.services.memory_store.list_memories("spica::default")
            self.assertEqual(len(rows), 1)  # same game -> ONE card, overwritten
            self.assertEqual(rows[0]["content"], "第二次游玩的履历卡（更新）。")
            host._record_play_history("anemoi", "anemoi 的履历卡。")
            rows = host.services.memory_store.list_memories("spica::default")
            self.assertEqual(len(rows), 2)  # different game -> coexists


class RecoverHistoryTest(unittest.TestCase):
    """B 方案: a recovered (dangling) session never ran stop() -> its history card
    is written by the recover wrapper. Fake LLM port; everything else real."""

    def test_recover_writes_history_card(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace

        from memory.store import SQLiteMemoryStore
        from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
        from spica.config.schema import AppConfig
        from spica.galgame.models import PlaySession, StoryLine, StoryLineStatus, utc_now_iso

        fake_summary = json.dumps({
            "summary_zh": "雪鹰在天台向主人公告白。", "characters": ["雪鹰"],
            "major_events": ["告白"], "unresolved_threads": [], "key_lines": [],
            "emotional_tone": "暧昧", "route_guess": {"name": "雪鹰", "confidence": 0.8, "evidence": []},
            "chapter_guess": {}, "relations": [],
        }, ensure_ascii=False)

        class _FakeLLM:
            # Adapter-side TextModel v2 shape (Phase 6a): this fake sits at
            # services.llm_adapter -- the adapter half BoundModel calls into.
            def complete(self, prompt, *, model):
                return fake_summary

        with TemporaryDirectory() as tmp:
            game_memory = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
            # a dangling play session (crash residue) with one committed line
            game_memory.add_play_session(PlaySession(
                session_id="S1", game_id="limelight", started_at=utc_now_iso(), state="active",
            ))
            game_memory.add_story_line(StoryLine(
                line_id="L1", session_id="S1", game_id="limelight",
                text="雪鹰：好きです。", timestamp=utc_now_iso(),
                status=StoryLineStatus.COMMITTED,
            ))
            host = AppHost()
            host.config = AppConfig()
            host.services = SimpleNamespace(
                llm_adapter=_FakeLLM(),
                game_memory_adapter=game_memory,
                memory_store=SQLiteMemoryStore(Path(tmp) / "memory.sqlite3"),
            )
            recovered = host.recover_dangling_companion_sessions()
            self.assertEqual(recovered, ["S1"])
            self.assertEqual(game_memory.get_play_session("S1").state, "ended")  # 補總結 + ended
            rows = host.services.memory_store.list_memories("spica::default")
            self.assertEqual(len(rows), 1)  # the history card landed
            self.assertEqual(rows[0]["memory_key"], "galgame_history:limelight")
            self.assertIn("一起玩了游戏《limelight》", rows[0]["content"])
            # NB: recover only 補總結 (it does NOT update GameProgressState), so the
            # card carries the summary text, not a route phrase.
            self.assertIn("最近剧情：雪鹰在天台向主人公告白", rows[0]["content"])


class RecoverInterruptedHistoryTest(unittest.TestCase):
    """AR-C1 §9.1-5 characterization (D2a=A2): an INTERRUPTED recovery (LLM failed,
    lines kept) still counts in the recovered return value and the Host still writes
    its play-history card. Existing tests only cover the success card; this pins the
    interrupted branch -- recovery/host production code is untouched this phase."""

    def test_interrupted_recovery_still_writes_history_card(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace

        from memory.store import SQLiteMemoryStore
        from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
        from spica.config.schema import AppConfig
        from spica.galgame.models import (
            PlaySession,
            StoryLine,
            StoryLineStatus,
            StorySummary,
            utc_now_iso,
        )

        class _FailLLM:
            def complete(self, prompt, *, model):
                raise RuntimeError("llm down")

        with TemporaryDirectory() as tmp:
            game_memory = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
            # an EARLIER successful play left a summary -> the history card is
            # composable even though this recovery pass fails.
            game_memory.add_summary(StorySummary(
                summary_id="OLD", game_id="limelight", session_id="S0",
                source_line_ids=["L0"], summary_zh="老剧情。",
                created_at=utc_now_iso(), updated_at=utc_now_iso(),
            ))
            # crash residue: dangling session with one unsummarized committed line
            game_memory.add_play_session(PlaySession(
                session_id="S1", game_id="limelight", started_at=utc_now_iso(), state="active",
            ))
            game_memory.add_story_line(StoryLine(
                line_id="L1", session_id="S1", game_id="limelight",
                text="雪鹰：好きです。", timestamp=utc_now_iso(),
                status=StoryLineStatus.COMMITTED,
            ))
            host = AppHost()
            host.config = AppConfig()
            host.services = SimpleNamespace(
                llm_adapter=_FailLLM(),
                game_memory_adapter=game_memory,
                memory_store=SQLiteMemoryStore(Path(tmp) / "memory.sqlite3"),
            )
            recovered = host.recover_dangling_companion_sessions()
            self.assertEqual(recovered, ["S1"])  # interrupted still counted
            self.assertEqual(game_memory.get_play_session("S1").state, "interrupted")
            # the batch stays unsummarized (recoverable later)...
            self.assertEqual(
                [l.line_id for l in game_memory.unsummarized_committed_story_lines("limelight")],
                ["L1"],
            )
            # ...and the Host still wrote the card (best-effort, D2a=A2 current shape)
            rows = host.services.memory_store.list_memories("spica::default")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["memory_key"], "galgame_history:limelight")


if __name__ == "__main__":
    unittest.main()
