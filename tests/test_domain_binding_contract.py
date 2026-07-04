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


# --------------------------------------------------------------------------- #
# Phase 8-c1 contracts (appended after the c0 baselines above): the generic
# DomainTurnBinding lane, the registry-based double-wrap guard, the router's
# galgame-only isolation, and the exploding-sink safety (设计裁决 1/2/6).
# --------------------------------------------------------------------------- #

import dataclasses

from spica.host.domain_router import ActiveDomainRouter
from spica.runtime.context import DomainContextRequest, DomainTurnBinding


class _CowatchRequest(DomainContextRequest):
    """A minimal second-domain request shape (kw_only subclassing works)."""


_GENERIC_BINDING = DomainTurnBinding(
    conversation_id="cowatch::movie-night",
    context_request=_CowatchRequest(domain="cowatch", mode="active"),
)


class GenericTypesShapeTest(unittest.TestCase):
    """裁决 2: the generic types are frozen; the base is kw_only."""

    def test_domain_context_request_is_frozen_and_kw_only(self):
        with self.assertRaises(TypeError):
            DomainContextRequest("cowatch")  # positional forbidden (kw_only)
        req = DomainContextRequest(domain="cowatch")
        self.assertEqual(req.mode, "none")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            req.mode = "active"

    def test_domain_turn_binding_is_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            _GENERIC_BINDING.conversation_id = "x"


class GenericLaneTest(unittest.TestCase):
    """裁决 2: the DomainTurnBinding lane fills the generic tuple, never the
    galgame slot, and preserves §27① exactly like the legacy lane."""

    def _engine_with_generic(self):
        engine = _engine()
        engine.set_game_binding_provider(lambda: _GENERIC_BINDING)
        return engine

    def test_generic_lane_fills_tuple_not_game_slot(self):
        req = self._engine_with_generic()._request(
            "你好", "default", None, None, None, True, "chat", None
        )
        self.assertEqual(req.conversation_id, "cowatch::movie-night")
        self.assertEqual(req.domain_context_requests, (_GENERIC_BINDING.context_request,))
        self.assertIsNone(req.game_context_request)  # galgame-only slot untouched

    def test_generic_lane_preserves_caller_conversation(self):
        req = self._engine_with_generic()._request(
            "你好", "side_chat", None, None, None, True, "chat", None
        )
        self.assertEqual(req.memory_conversation_id, "side_chat")  # §27①

    def test_generic_lane_empty_caller_falls_back_to_default(self):
        req = self._engine_with_generic()._request(
            "你好", "", None, None, None, True, "chat", None
        )
        self.assertEqual(req.memory_conversation_id, "default")

    def test_registered_prefix_double_wrap_guard(self):
        # A caller already inside ANY registered domain namespace is taken
        # as-is even while a binding is active (galgame:: is the only
        # registered prefix today -> byte-identical to the old guard).
        engine = self._engine_with_generic()
        manual_cid = "galgame::other::playthrough::ng+"
        req = engine._request("你好", manual_cid, None, None, None, True, "chat", None)
        self.assertEqual(req.conversation_id, manual_cid)
        self.assertEqual(req.domain_context_requests, ())
        self.assertIsNone(req.game_context_request)

    def test_unknown_binding_shape_fails_open_to_plain(self):
        engine = _engine()
        engine.set_game_binding_provider(lambda: object())  # wiring bug shape
        req = engine._request("你好", "default", None, None, None, True, "chat", None)
        self.assertEqual(req.conversation_id, "default")
        self.assertEqual(req.domain_context_requests, ())
        self.assertIsNone(req.game_context_request)


class RouterGalgameIsolationTest(unittest.TestCase):
    """裁决 修正 1: a non-galgame high-priority router binding must NOT leak
    into the galgame-only closure -- it keeps reading the controller snapshot."""

    def test_high_priority_generic_binding_does_not_reach_galgame_closure(self):
        host = AppHost()
        host._companion_controller = SimpleNamespace(current_game_context=lambda: _BINDING)
        host.domain_router.publish("cowatch", _GENERIC_BINDING, priority=100)
        # The engine's slot (router.current) sees the generic binding...
        self.assertIs(host.domain_router.current(), _GENERIC_BINDING)
        # ...but the galgame-only closure still sees ONLY the controller snapshot.
        self.assertIs(host._companion_game_binding(), _BINDING)


class _ExplodingSink:
    def publish(self, domain, binding, priority=0):
        raise RuntimeError("sink down")

    def retract(self, domain):
        raise RuntimeError("sink down")


class BindingSinkSafetyTest(ControllerHarness):
    """裁决 6: sink failures are best-effort -- never half-start, never break
    stop, never resurrect a binding."""

    def _controller_with_sink(self, sink):
        return GalgameCompanionController(
            self.mem, _Capture(), _Locator(), _InertOcr(),
            summarizer=None, emit=lambda event: None,
            summary_trigger_chars=100000, interval_seconds=60.0,
            binding_sink=sink,
        )

    def test_exploding_sink_does_not_block_start_or_stop(self):
        c = self._controller_with_sink(_ExplodingSink())
        c.start("0x1", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))  # no raise
        self.assertIsNotNone(c.current_game_context())  # local publish intact
        c.stop()  # no raise
        self.assertIsNone(c.current_game_context())     # clear-FIRST intact
        self.assertIsNone(c.current_watch_target())

    def test_real_router_sink_mirrors_publish_and_retract(self):
        router = ActiveDomainRouter()
        c = self._controller_with_sink(router)
        c.start("0x1", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
        published = router.current_for("galgame")
        self.assertIsInstance(published, GameTurnBinding)
        self.assertIs(published, c.current_game_context())  # the SAME snapshot object
        c.stop()
        self.assertIsNone(router.current_for("galgame"))    # retracted at clear-FIRST
        self.assertIsNone(router.current())

    def test_failed_start_leaves_router_empty(self):
        router = ActiveDomainRouter()
        c = self._controller_with_sink(router)
        with self.assertRaises(GalgameCompanionError):
            c.start("0x1", game_id="g1")  # no ratios -> fails before publish
        self.assertIsNone(router.current())
        self.assertIsNone(router.current_for("galgame"))


if __name__ == "__main__":
    unittest.main()
