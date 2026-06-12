"""Phase 9 step 1: the watch_game_screen thin shell -- captures the BOUND game
window (generic window-capture form), feeds the shared ScreenAnalysisPort, and
returns the observation; not playing -> NO_ACTIVE_COMPANION (no full-screen
fallback). Plus the router offer-gate and the stages roster expansion (特判一:
the record logic itself is byte-identical for inspect_screen).
"""

import json
import unittest
from types import SimpleNamespace

from PIL import Image

from agent_tools.function_tools.router import is_screen_intent_explicit, tool_success
from agent_tools.function_tools.screen.schema import ScreenToolError
from agent_tools.function_tools.screen.tool import INSPECT_SCREEN_SCHEMA
from spica.adapters.tools.watch_game_screen import (
    WATCH_GAME_SCREEN_SCHEMA,
    WatchGameScreenTool,
)
from spica.config.schema import AppConfig
from spica.galgame.session import GalgameState
from spica.host.app_host import AppHost
from spica.plugins.registry import CapabilityRegistry
from spica.ports.screen_capture import CaptureImage
from spica.ports.window_locator import WindowGeometry
from spica.runtime.context import PromptBundle, TurnContext, TurnRequest
from spica.runtime.deps import TurnDeps
from spica.runtime.observer import NoopTurnObserver
from spica.runtime.stages import call_llm_node, record_screen_tool_result
from spica.runtime.tools import RegistryToolSet


class _Locator:
    def __init__(self, geometry=WindowGeometry(10, 20, 300, 200)):
        self.geometry = geometry
        self.asked = []

    def get_window_geometry(self, window_id):
        self.asked.append(window_id)
        return self.geometry


class _Capture:
    def __init__(self):
        self.rects = []

    def capture_rect(self, left, top, width, height):
        self.rects.append((left, top, width, height))
        img = Image.new("RGB", (max(1, width), max(1, height)), (30, 30, 30))
        return CaptureImage(image=img, width=img.width, height=img.height)


class _Analysis:
    def __init__(self):
        self.calls = []

    def analyze_image(self, image, mode, prompt=None, *, config=None, capture=None, performance=None, question_type=None):
        self.calls.append({
            "size": (image.width, image.height), "mode": mode, "prompt": prompt,
            "capture": capture, "question_type": question_type,
        })
        return {"schema_version": "screen_observation.v1", "request": {"target": mode}}


class ThinShellTest(unittest.TestCase):
    def test_captures_the_bound_window_and_analyzes(self):
        locator, capture, analysis = _Locator(), _Capture(), _Analysis()
        tool = WatchGameScreenTool(
            analysis, lambda: ("limelight", "0x42", locator, capture, GalgameState.PLAYING)
        )
        result = tool.run(question="这个角色是谁")
        self.assertEqual(result["schema_version"], "screen_observation.v1")
        self.assertEqual(locator.asked, ["0x42"])  # the BOUND window, not full screen
        self.assertEqual(capture.rects, [(10, 20, 300, 200)])  # its geometry
        call = analysis.calls[0]
        self.assertEqual(call["mode"], "game_window")
        self.assertEqual(call["prompt"], "这个角色是谁")
        self.assertEqual(call["size"], (300, 200))
        self.assertEqual(call["capture"]["window"], {"window_id": "0x42", "game_id": "limelight"})

    def test_no_active_companion_raises(self):
        tool = WatchGameScreenTool(_Analysis(), lambda: None)  # not playing
        with self.assertRaises(ScreenToolError) as caught:
            tool.run(question="看看画面")
        self.assertEqual(caught.exception.code, "NO_ACTIVE_COMPANION")

    def test_no_active_companion_via_registry_toolset(self):
        # End to end through the runtime tool surface: raise -> tool_error string.
        from spica.plugins.registry import CapabilityRegistry

        registry = CapabilityRegistry()
        tool = WatchGameScreenTool(_Analysis(), lambda: None)
        registry.register_tool(tool.schema(), tool.run)
        result = json.loads(RegistryToolSet(registry).run("watch_game_screen", '{"question": "看看"}'))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NO_ACTIVE_COMPANION")

    def test_window_gone_raises_unavailable(self):
        locator = _Locator(geometry=None)
        tool = WatchGameScreenTool(
            _Analysis(), lambda: ("g", "0x1", locator, _Capture(), GalgameState.PLAYING)
        )
        with self.assertRaises(ScreenToolError) as caught:
            tool.run(question="看看")
        self.assertEqual(caught.exception.code, "GAME_WINDOW_UNAVAILABLE")


class PrivacyGateTest(unittest.TestCase):
    """External-review #1 (CLAUDE.md §4 隐私承诺对账): outside the OCR-monitored
    visible states the tool refuses BEFORE any capture -- the rect under a
    lost/paused window may show another application. The refusal is a ToolError
    envelope (the turn survives; she can explain WHY she can't look)."""

    @staticmethod
    def _tool(state):
        locator, capture, analysis = _Locator(), _Capture(), _Analysis()
        tool = WatchGameScreenTool(
            analysis, lambda: ("g", "0x1", locator, capture, state)
        )
        return tool, capture

    def test_window_lost_refused_with_zero_captures(self):
        tool, capture = self._tool(GalgameState.WINDOW_LOST)
        with self.assertRaises(ScreenToolError) as caught:
            tool.run(question="现在画面上是什么")
        self.assertEqual(caught.exception.code, "GAME_WINDOW_NOT_SAFE")
        self.assertIn("挡住", caught.exception.message)  # window-lost wording
        self.assertEqual(capture.rects, [])  # privacy pin: capture_rect NEVER called

    def test_window_lost_envelope_through_registry_toolset(self):
        # End to end through the runtime tool surface: envelope, not a crashed turn.
        tool, capture = self._tool(GalgameState.WINDOW_LOST)
        registry = CapabilityRegistry()
        registry.register_tool(tool.schema(), tool.run)
        result = json.loads(
            RegistryToolSet(registry).run("watch_game_screen", '{"question": "看看"}')
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "GAME_WINDOW_NOT_SAFE")
        self.assertEqual(capture.rects, [])

    def test_paused_refused_with_paused_wording(self):
        tool, capture = self._tool(GalgameState.PAUSED)
        with self.assertRaises(ScreenToolError) as caught:
            tool.run(question="看看画面")
        self.assertEqual(caught.exception.code, "GAME_WINDOW_NOT_SAFE")
        self.assertIn("暂停", caught.exception.message)
        self.assertEqual(capture.rects, [])

    def test_monitored_states_allowed(self):
        # CHOICE_CHECKING is the tool's PRIMARY scenario ("该选哪个" happens during
        # choice detection) -- a strict PLAYING-only gate would kill it;
        # BACKGROUND_SUMMARIZING is normal play with a summary running behind.
        for state in (
            GalgameState.PLAYING,
            GalgameState.CHOICE_CHECKING,
            GalgameState.BACKGROUND_SUMMARIZING,
        ):
            with self.subTest(state=state.value):
                tool, capture = self._tool(state)
                result = tool.run(question="该选哪个")
                self.assertEqual(result["schema_version"], "screen_observation.v1")
                self.assertEqual(capture.rects, [(10, 20, 300, 200)])  # captured normally


class OfferGateTest(unittest.TestCase):
    """Trigger-layer refactor: watch_game_screen is offered by STATE (the
    registry's available predicate), not by wordlist; "call or not" is the LLM's
    structured decision. inspect_screen keeps its word gate byte-identically."""

    def _toolset(self, playing):
        registry = CapabilityRegistry()
        registry.register_tool(INSPECT_SCREEN_SCHEMA, lambda **kw: "x")  # legacy two-arg form
        registry.register_tool(
            WATCH_GAME_SCREEN_SCHEMA, lambda **kw: "x",
            available=lambda: playing[0], intent_gated=False,
        )
        return RegistryToolSet(registry)

    def test_offered_for_any_text_while_playing(self):
        playing = [True]
        toolset = self._toolset(playing)
        for text in ("她叫什么名字", "这立绘也太好看了吧", "今天天气怎么样"):  # NO watch words needed
            with self.subTest(text=text):
                names = {s.get("name") for s in toolset.schemas_for_user_text(text)}
                self.assertIn("watch_game_screen", names)

    def test_not_offered_when_not_playing(self):
        playing = [False]
        toolset = self._toolset(playing)
        self.assertEqual(toolset.schemas_for_user_text("帮我看看这个角色"), [])

    def test_inspect_screen_word_gate_unchanged(self):
        # Regression pin: inspect's double gate is untouched by the refactor.
        playing = [False]
        toolset = self._toolset(playing)
        names = {s.get("name") for s in toolset.schemas_for_user_text("帮我看看屏幕上有什么")}
        self.assertEqual(names, {"inspect_screen"})
        self.assertTrue(is_screen_intent_explicit("帮我看看屏幕上有什么"))
        self.assertFalse(is_screen_intent_explicit("今天有点累"))
        playing[0] = True  # while playing, a screen-worded text offers BOTH
        names = {s.get("name") for s in toolset.schemas_for_user_text("帮我看看屏幕上有什么")}
        self.assertEqual(names, {"inspect_screen", "watch_game_screen"})


class RegistryCompatTest(unittest.TestCase):
    def test_legacy_two_arg_register_defaults_gated_and_available(self):
        registry = CapabilityRegistry()
        registry.register_tool(INSPECT_SCREEN_SCHEMA, lambda **kw: "x")  # old signature
        self.assertTrue(registry.tool_intent_gated("inspect_screen"))  # gated by default
        self.assertEqual(len(registry.tool_schemas()), 1)  # always offered at state level

    def test_function_table_registry_path_unchanged(self):
        # The legacy/test path (no tool_intent_gated query) -> everything word-gated,
        # byte-identical to the pre-refactor behaviour.
        toolset = RegistryToolSet.from_function_table(
            [INSPECT_SCREEN_SCHEMA], {"inspect_screen": lambda **kw: "x"}
        )
        self.assertEqual(toolset.schemas_for_user_text("今天有点累"), [])
        self.assertEqual(len(toolset.schemas_for_user_text("帮我看看屏幕")), 1)


class ToolsFieldTest(unittest.TestCase):
    """Pinned (review requirement): a plain non-companion turn's LLM request carries
    NO tools field -- byte-identical to before; a companion turn carries the watch
    tool (the expected per-turn cost)."""

    def _llm_request(self, playing, user_input="你好呀"):
        registry = CapabilityRegistry()
        registry.register_tool(INSPECT_SCREEN_SCHEMA, lambda **kw: "x")
        registry.register_tool(
            WATCH_GAME_SCREEN_SCHEMA, lambda **kw: "x",
            available=lambda: playing, intent_gated=False,
        )
        requests = []

        class _Adapter:
            def prefers_chat_completions(self):
                return False

            def create_responses(self, **kwargs):
                requests.append(kwargs)
                return SimpleNamespace(
                    id="r1",
                    output_text='{"answer":"好","emotion":"happy","emotion_reason":"x"}',
                    output=[],
                    usage=None,
                )

        ctx = TurnContext(TurnRequest(user_input=user_input))
        ctx.user_input = user_input
        ctx.prompt = PromptBundle(prompt_input="p")
        deps = TurnDeps(
            config=AppConfig(), llm=_Adapter(), tts=None, visual=None, memory=None,
            tools=RegistryToolSet(registry),
        )
        services = SimpleNamespace(llm_client=object(), tool_schemas=[])
        call_llm_node(ctx, services, deps)
        return requests[0]

    def test_plain_turn_request_has_no_tools_field(self):
        self.assertNotIn("tools", self._llm_request(playing=False))

    def test_companion_turn_request_carries_watch_tool(self):
        request = self._llm_request(playing=True)
        self.assertIn("tools", request)
        names = {schema.get("name") for schema in request["tools"]}
        self.assertIn("watch_game_screen", names)


def _observation_payload(target):
    return tool_success({
        "schema_version": "screen_observation.v1",
        "request": {"target": target},
        "capture": {"source": "automatic_screenshot"},
    })


class RecordRosterTest(unittest.TestCase):
    """特判一: only the ROSTER grew; the record logic is identical for both names."""

    def _record(self, tool_name, target="full_screen"):
        ctx = TurnContext(TurnRequest(user_input="x"))
        record_screen_tool_result(ctx, NoopTurnObserver(), tool_name, _observation_payload(target))
        return ctx

    def test_watch_game_screen_result_is_lifted_into_ctx(self):
        ctx = self._record("watch_game_screen", target="game_window")
        self.assertIsNotNone(ctx.screen_observation)
        self.assertEqual(ctx.screen_observation["request"]["target"], "game_window")
        self.assertTrue(ctx.metadata["screen_observation_used"])
        self.assertEqual(ctx.metadata["screen_observation_target"], "game_window")

    def test_inspect_screen_path_unchanged(self):
        ctx = self._record("inspect_screen")
        self.assertIsNotNone(ctx.screen_observation)
        self.assertEqual(ctx.metadata["screen_observation_schema"], "screen_observation.v1")
        self.assertEqual(ctx.metadata["screen_observation_source"], "automatic_screenshot")

    def test_other_tools_still_skipped(self):
        ctx = self._record("some_other_tool")
        self.assertIsNone(ctx.screen_observation)
        self.assertNotIn("screen_observation_used", ctx.metadata)


class StaleNoteTest(unittest.TestCase):
    """Stale-frame fix (plan d): the next-turn context self-identifies as a
    PREVIOUS-turn snapshot (d-a), and the description instructs a re-capture for
    current-screen questions (d-b). 特判一 intact: the old observation REMAINS in
    context and still carries its content for follow-ups."""

    @staticmethod
    def _observation(target):
        return {
            "schema_version": "screen_observation.v1",
            "request": {"target": target},
            "capture": {"source": "automatic_screenshot"},
            "followup": {"context_for_next_turn": "雪鹰穿着白色连衣裙站在天台。"},
        }

    def test_next_turn_context_carries_stale_note_and_content(self):
        from agent_tools.function_tools.screen.schema import (
            screen_observation_context_for_next_turn,
        )

        text = screen_observation_context_for_next_turn(self._observation("game_window"))
        self.assertTrue(text.startswith("[上一轮查看的画面，非当前画面]"))  # d-a self-identification
        self.assertIn("雪鹰穿着白色连衣裙", text)  # 特判一: content preserved for follow-ups
        # the shared generator improves inspect_screen the same way
        inspect_text = screen_observation_context_for_next_turn(self._observation("full_screen"))
        self.assertTrue(inspect_text.startswith("[上一轮查看的画面，非当前画面]"))
        self.assertIn("full_screen", inspect_text)

    def test_description_instructs_recapture_for_current_screen(self):
        description = WATCH_GAME_SCREEN_SCHEMA["description"]
        self.assertIn("必须重新调用", description)  # d-b guidance
        self.assertIn("不要依赖之前的观察结果", description)
        self.assertIn("追问", description)  # ...while follow-ups about THAT view stay allowed


class HostWiringTest(unittest.TestCase):
    @staticmethod
    def _offered_names(host):
        names = set()
        for schema in host.registry.tool_schemas():
            names.add(schema.get("name") or (schema.get("function") or {}).get("name"))
        return names

    def test_tool_registered_and_state_gated_on_host_registry(self):
        host = AppHost()  # registration happens in __init__ (closure over the host)
        self.assertIsNotNone(host.registry.tool_handler("watch_game_screen"))
        self.assertFalse(host.registry.tool_intent_gated("watch_game_screen"))  # state, not words
        # NOT offered before initialize / while not playing (available predicate)...
        self.assertNotIn("watch_game_screen", self._offered_names(host))
        # ...offered as soon as the companion play is live.
        host._companion_controller = SimpleNamespace(
            current_watch_target=lambda: ("g1", "0x9"),
            session=SimpleNamespace(state=GalgameState.PLAYING),
        )
        host.services = SimpleNamespace(window_locator_adapter="LOC", screen_capture_adapter="CAP")
        self.assertIn("watch_game_screen", self._offered_names(host))

    def test_watch_context_provider(self):
        host = AppHost()
        self.assertIsNone(host._companion_watch_context())  # no controller yet
        host._companion_controller = SimpleNamespace(
            current_watch_target=lambda: ("g1", "0x9"),
            session=SimpleNamespace(state=GalgameState.PLAYING),
        )
        host.services = SimpleNamespace(window_locator_adapter="LOC", screen_capture_adapter="CAP")
        self.assertEqual(
            host._companion_watch_context(),
            ("g1", "0x9", "LOC", "CAP", GalgameState.PLAYING),
        )
        # the state element is LIVE: a lost window shows up in the same tuple
        host._companion_controller.session = SimpleNamespace(state=GalgameState.WINDOW_LOST)
        self.assertEqual(host._companion_watch_context()[4], GalgameState.WINDOW_LOST)
        # target published but session gone (stop race) -> treated as not playing
        host._companion_controller.session = None
        self.assertIsNone(host._companion_watch_context())
        host._companion_controller = SimpleNamespace(current_watch_target=lambda: None)  # playing ended
        self.assertIsNone(host._companion_watch_context())


if __name__ == "__main__":
    unittest.main()
