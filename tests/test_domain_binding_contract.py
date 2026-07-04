"""Phase 8-c0 characterization: the domain-binding surface, pinned BEFORE the
ActiveDomainRouter / DomainTurnBinding / PrivacyGate seams land.

Each class pins one Phase 8 design ruling's protection baseline (MIGRATION_PLAN
Phase 8「设计裁决」, 2026-07-04). The FULL GameTurnBinding-lane request snapshot
(field-for-field no-provider equality, three-fields-only delta, double-wrap
guard, §27① caller-conversation preservation) already lives in
tests/test_chat_engine_game_binding.py -- deliberately NOT duplicated here;
this file adds only the gaps plus the Phase 8 baselines:

1. RequestLaneGapTest      -- 裁决 2: the `or "default"` half of
   memory_conversation_id (empty caller conversation) on the legacy lane.
2. PublishDisciplineTest   -- 裁决 1/6: publish-LAST / clear-FIRST as an
   OBSERVABLE controller contract (local snapshots; the future router sink
   must preserve exactly this).
3. GalgameOnlyClosureTest  -- 裁决 修正 1: `_companion_game_binding()` is a
   pure pass-through of the CONTROLLER snapshot. Protection baseline for
   8-c1: it must NEVER be rewired to an unconditional `router.current()`
   (a higher-priority co-watch binding would be misread as GameTurnBinding
   by the note/reaction closures that consume this method).
4. WatchContextTest        -- 裁决 4: today's bare 5-tuple shape
   (game_id, window_id, locator, capture, state) and its three None paths --
   the WatchContext NamedTuple flip (8-c2) must keep these semantics.
5. SystemTurnDomainIdTest  -- 裁决 2 前缀半: a system turn's domain identity
   IS its conversation_id; `source` is telemetry-only (structurally absent
   from TurnRequest) and never reaches the gate.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.config.schema import AppConfig
from spica.core.chat_engine import ChatEngine
from spica.core.proactive import compose_system_directive_message
from spica.galgame.companion_controller import (
    GalgameCompanionController,
    GalgameCompanionError,
)
from spica.galgame.context_contributor import galgame_contributor
from spica.galgame.session import GalgameState
from spica.host.app_host import AppHost
from spica.ports.ocr import OcrResult
from spica.ports.screen_capture import CaptureImage
from spica.ports.window_locator import WindowGeometry, WindowSafetyResult
from spica.runtime.context import GameContextRequest, GameTurnBinding, TurnRequest
from spica.runtime.services import AgentServices


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


class _StubSummarizer:
    def summarize(self, lines, *, recent_summaries=None, progress=None):
        raise AssertionError("summarizer must not run in these tests")


def _engine() -> ChatEngine:
    services = AgentServices(
        llm_client=None, tts_adapter=None, visual_tool=None,
        memory_store=None, recent_memory=None, config={},
        llm_adapter=object(), memory_adapter=object(),
    )
    return ChatEngine(services, AppConfig())


_BINDING = GameTurnBinding(
    conversation_id="galgame::limelight::playthrough::default",
    game_context_request=GameContextRequest(mode="active", game_id="limelight"),
)


class RequestLaneGapTest(unittest.TestCase):
    """裁决 2 gap: memory_conversation_id = caller original OR "default"."""

    def test_empty_caller_conversation_falls_back_to_default(self):
        engine = _engine()
        engine.set_game_binding_provider(lambda: _BINDING)
        req = engine._request("你好", "", None, None, None, True, "chat", None)
        self.assertEqual(req.conversation_id, _BINDING.conversation_id)
        self.assertEqual(req.memory_conversation_id, "default")  # the `or "default"` half

    def test_plain_lane_empty_conversation_defaults(self):
        req = _engine()._request("你好", "", None, None, None, True, "chat", None)
        self.assertEqual(req.conversation_id, "default")
        self.assertIsNone(req.memory_conversation_id)


class ControllerHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mem = GameMemorySqliteAdapter(Path(self._tmp.name) / "galgame.sqlite3")

    def _controller(self):
        return GalgameCompanionController(
            self.mem, _Capture(), _Locator(), _InertOcr(),
            summarizer=None, emit=lambda event: None,
            summary_trigger_chars=100000, interval_seconds=60.0,
        )


class PublishDisciplineTest(ControllerHarness):
    """裁决 1/6 baseline: publish-LAST / clear-FIRST observable contract.

    The future router sink is called at EXACTLY these two points; whatever the
    sink does, this local-snapshot contract must survive byte-for-byte.
    """

    def test_start_publishes_binding_and_watch_target(self):
        c = self._controller()
        c.start("0x1", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
        try:
            binding = c.current_game_context()
            self.assertIsInstance(binding, GameTurnBinding)
            self.assertTrue(binding.conversation_id.startswith("galgame::g1"))
            self.assertEqual(binding.game_context_request.mode, "active")
            target = c.current_watch_target()
            self.assertEqual(target, ("g1", "0x1"))
        finally:
            c.stop()

    def test_stop_clears_both_immediately(self):
        c = self._controller()
        c.start("0x1", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
        c.stop()
        self.assertIsNone(c.current_game_context())
        self.assertIsNone(c.current_watch_target())

    def test_failed_start_publishes_nothing(self):
        c = self._controller()
        with self.assertRaises(GalgameCompanionError):
            c.start("0x1", game_id="g1")  # no profile, no ratios -> fails early
        self.assertIsNone(c.current_game_context())
        self.assertIsNone(c.current_watch_target())


class GalgameOnlyClosureTest(unittest.TestCase):
    """裁决 修正 1 baseline: `_companion_game_binding` is a controller-snapshot
    pass-through. 8-c1 MUST keep it galgame-only -- never an unconditional
    `router.current()` read (note/reaction closures consume this method and
    would misread a higher-priority co-watch binding as a GameTurnBinding)."""

    def test_no_controller_returns_none(self):
        host = AppHost()
        self.assertIsNone(host._companion_game_binding())

    def test_with_controller_passes_snapshot_through(self):
        host = AppHost()
        sentinel = _BINDING
        host._companion_controller = SimpleNamespace(current_game_context=lambda: sentinel)
        self.assertIs(host._companion_game_binding(), sentinel)


class WatchContextTest(unittest.TestCase):
    """裁决 4 baseline: today's bare 5-tuple + its None paths. The 8-c2
    WatchContext NamedTuple flip must preserve every branch's semantics."""

    def _host(self):
        host = AppHost()
        host.services = SimpleNamespace(
            window_locator_adapter=object(), screen_capture_adapter=object()
        )
        return host

    def test_none_without_controller(self):
        host = AppHost()  # services also None before initialize
        self.assertIsNone(host._companion_watch_context())

    def test_none_when_no_live_target(self):
        host = self._host()
        host._companion_controller = SimpleNamespace(
            current_watch_target=lambda: None, session=None
        )
        self.assertIsNone(host._companion_watch_context())

    def test_none_when_session_gone_after_target(self):
        # stop() clears the watch target FIRST -> this is the narrow race branch:
        # target still visible but session already None -> treated as not playing.
        host = self._host()
        host._companion_controller = SimpleNamespace(
            current_watch_target=lambda: ("g1", "0x1"), session=None
        )
        self.assertIsNone(host._companion_watch_context())

    def test_live_target_returns_bare_five_tuple(self):
        host = self._host()
        host._companion_controller = SimpleNamespace(
            current_watch_target=lambda: ("g1", "0x1"),
            session=SimpleNamespace(state=GalgameState.PLAYING),
        )
        context = host._companion_watch_context()
        self.assertEqual(
            context,
            (
                "g1",
                "0x1",
                host.services.window_locator_adapter,
                host.services.screen_capture_adapter,
                GalgameState.PLAYING,
            ),
        )


class SystemTurnDomainIdTest(unittest.TestCase):
    """裁决 2 前缀半 baseline: system-turn domain identity = conversation_id;
    `source` is telemetry-only and structurally cannot reach the gate."""

    def test_source_never_reaches_stream_voice(self):
        engine = _engine()
        calls = []
        with patch.object(
            ChatEngine, "stream_voice",
            lambda self, user_input, **kwargs: calls.append((user_input, kwargs)) or iter(()),
        ):
            list(engine.stream_system_turn(
                "唱完了", conversation_id="galgame::g1::playthrough::default", source="song"
            ))
            list(engine.stream_system_turn(
                "唱完了", conversation_id="galgame::g1::playthrough::default", source="other"
            ))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])  # source made ZERO difference
        user_input, kwargs = calls[0]
        self.assertEqual(user_input, compose_system_directive_message("唱完了"))
        self.assertEqual(kwargs["conversation_id"], "galgame::g1::playthrough::default")
        self.assertEqual(kwargs["interaction_mode"], "system")
        self.assertNotIn("source", kwargs)  # telemetry label only, dropped before the turn

    def test_request_has_no_source_field_and_gate_reads_conversation_id(self):
        # Structural half: TurnRequest carries NO source field at all, so no gate
        # can ever depend on it; the galgame gate recognizes a system turn by the
        # domain conversation prefix alone.
        import dataclasses

        self.assertNotIn("source", {f.name for f in dataclasses.fields(TurnRequest)})
        req = TurnRequest(
            user_input="[系统指令] x", conversation_id="galgame::g1::playthrough::default",
            interaction_mode="system",
        )
        self.assertEqual(galgame_contributor.mode(req), "active")
        plain = TurnRequest(user_input="x", conversation_id="default", interaction_mode="system")
        self.assertEqual(galgame_contributor.mode(plain), "none")


if __name__ == "__main__":
    unittest.main()
