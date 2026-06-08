"""C7 unit tests for ScreenAnalysisPort + the local adapter.

The adapter is a thin, behaviour-preserving pass-through to the existing
``analyze_screen_image_local`` engine -- the formalization that lets the
inspect_screen tool and the manual-attachment stage share one analysis adapter.
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_tools.function_tools.screen.schema import ScreenToolError
from spica.adapters.screen import LocalMoondreamScreenAnalysis
from spica.adapters.tools import InspectScreenTool
from spica.plugins.registry import CapabilityRegistry
from spica.ports.screen import ScreenAnalysisPort
from spica.runtime.tools import RegistryToolSet


class _SpyScreen:
    def __init__(self, observation):
        self.observation = observation
        self.calls = []

    def analyze_image(self, image, mode, prompt=None, **kw):
        self.calls.append((image, mode, prompt, kw))
        return self.observation


class ScreenAnalysisAdapterTest(unittest.TestCase):
    def test_adapter_conforms_to_port(self):
        self.assertIsInstance(LocalMoondreamScreenAnalysis(), ScreenAnalysisPort)

    def test_analyze_image_delegates_to_the_local_engine(self):
        observation = {"schema_version": "screen_observation.v1"}
        with patch(
            "spica.adapters.screen.local_moondream.analyze_screen_image_local",
            return_value=observation,
        ) as engine:
            out = LocalMoondreamScreenAnalysis().analyze_image(
                "IMG",
                "full_screen",
                "屏幕上有什么",
                config="CFG",
                capture={"source": "x"},
                performance={"capture_ms": 1.0},
                question_type="general_observation",
            )
        self.assertIs(out, observation)
        engine.assert_called_once_with(
            "IMG",
            "full_screen",
            "屏幕上有什么",
            config="CFG",
            capture={"source": "x"},
            performance={"capture_ms": 1.0},
            question_type="general_observation",
        )


class InspectScreenToolTest(unittest.TestCase):
    def test_schema_name_is_inspect_screen(self):
        self.assertEqual(InspectScreenTool(_SpyScreen({})).schema()["name"], "inspect_screen")

    def test_intent_gate_blocks_and_never_captures(self):
        screen = _SpyScreen({})
        with self.assertRaises(ScreenToolError) as cm:
            InspectScreenTool(screen).run(target="full_screen", question="你好")  # no screen intent
        self.assertEqual(cm.exception.code, "SCREEN_INTENT_NOT_EXPLICIT")
        self.assertEqual(screen.calls, [])  # gate fails before any capture/analyze

    def test_non_full_screen_target_is_rejected(self):
        with self.assertRaises(ScreenToolError) as cm:
            InspectScreenTool(_SpyScreen({})).run(target="region", question="帮我看看屏幕")
        self.assertEqual(cm.exception.code, "SCREEN_INTENT_NOT_EXPLICIT")

    def test_explicit_intent_captures_and_delegates_to_port(self):
        observation = {"schema_version": "screen_observation.v1"}
        screen = _SpyScreen(observation)
        capture = SimpleNamespace(image="IMG", metadata={})
        with patch("spica.adapters.tools.screen.capture_full_screen", return_value=capture), patch(
            "spica.adapters.tools.screen.load_screen_config",
            return_value=SimpleNamespace(capture_format="png"),
        ):
            out = InspectScreenTool(screen).run(target="full_screen", question="帮我看看屏幕上有没有报错")
        self.assertIs(out, observation)
        self.assertEqual(len(screen.calls), 1)
        image, mode, prompt, _kw = screen.calls[0]
        self.assertEqual((image, mode, prompt), ("IMG", "full_screen", "帮我看看屏幕上有没有报错"))


class RegistryToolAccessorTest(unittest.TestCase):
    def test_register_then_read_schemas_and_handler(self):
        registry = CapabilityRegistry()
        tool = InspectScreenTool(_SpyScreen({}))
        handler = tool.run
        registry.register_tool(tool.schema(), handler)
        self.assertEqual([s["name"] for s in registry.tool_schemas()], ["inspect_screen"])
        self.assertIs(registry.tool_handler("inspect_screen"), handler)
        self.assertIsNone(registry.tool_handler("not_a_tool"))


class RegistryToolSetInspectScreenTest(unittest.TestCase):
    """N5: inspect_screen resolves AND runs through the registry-backed ToolSet."""

    def _toolset(self, screen):
        registry = CapabilityRegistry()
        tool = InspectScreenTool(screen)
        registry.register_tool(tool.schema(), tool.run)
        return RegistryToolSet(registry)

    def test_intent_gate_selects_only_on_explicit_screen_intent(self):
        toolset = self._toolset(_SpyScreen({}))
        self.assertEqual(
            [s["name"] for s in toolset.schemas_for_user_text("帮我看看屏幕上有没有报错")],
            ["inspect_screen"],
        )
        self.assertEqual(toolset.schemas_for_user_text("你好"), [])

    def test_run_executes_toolport_and_wraps_dict_in_tool_success(self):
        observation = {"schema_version": "screen_observation.v1"}
        toolset = self._toolset(_SpyScreen(observation))
        with patch(
            "spica.adapters.tools.screen.capture_full_screen",
            return_value=SimpleNamespace(image="IMG", metadata={}),
        ), patch(
            "spica.adapters.tools.screen.load_screen_config",
            return_value=SimpleNamespace(capture_format="png"),
        ):
            out = toolset.run(
                "inspect_screen",
                '{"target": "full_screen", "question": "帮我看看屏幕上有没有报错"}',
            )
        parsed = json.loads(out)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["data"], observation)

    def test_defensive_gate_at_tool_layer_maps_to_tool_error(self):
        toolset = self._toolset(_SpyScreen({}))
        blocked = json.loads(
            toolset.run("inspect_screen", '{"target": "full_screen", "question": "你好"}')
        )
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"]["code"], "SCREEN_INTENT_NOT_EXPLICIT")


if __name__ == "__main__":
    unittest.main()
