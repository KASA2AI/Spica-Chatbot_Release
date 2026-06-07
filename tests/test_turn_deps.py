"""C3a: typed turn entry (TurnRequest / TurnDeps / ToolSet).

Locks the new typed surface introduced in C3a:
- TurnRequest defaults;
- LegacyFunctionToolSet keeps the intent gate and dispatches runs (no new
  behaviour over the legacy TOOL_SCHEMAS / run_local_tool pair);
- TurnDeps.from_services maps the resolved ports and guarantees the
  observer / jobs / exec_strategy placeholders are non-None and usable.
"""

import unittest
from types import SimpleNamespace

from agent_tools.function_tools import TOOL_SCHEMAS
from spica.config.schema import AppConfig
from spica.runtime.context import TurnRequest
from spica.runtime.deps import TurnDeps
from spica.runtime.tools import LegacyFunctionToolSet, ToolSet


def _services(tool_fn=None):
    return SimpleNamespace(
        llm_adapter="LLM",
        tts_adapter="TTS",
        visual_tool="VIS",
        memory_adapter="MEM",
        tool_schemas=TOOL_SCHEMAS,
        tool_functions={"inspect_screen": tool_fn or (lambda **kw: "ok")},
    )


class TurnRequestTest(unittest.TestCase):
    def test_defaults(self):
        req = TurnRequest(user_input="hi")
        self.assertEqual(req.conversation_id, "default")
        self.assertEqual(req.interaction_mode, "chat")
        self.assertTrue(req.include_user_time_context)
        self.assertIsNone(req.screen_attachment)
        self.assertEqual(req.visual_overrides, {})

    def test_distinct_requests_do_not_share_visual_overrides(self):
        a, b = TurnRequest(user_input="a"), TurnRequest(user_input="b")
        self.assertIsNot(a.visual_overrides, b.visual_overrides)


class LegacyFunctionToolSetTest(unittest.TestCase):
    def test_is_a_toolset(self):
        self.assertIsInstance(LegacyFunctionToolSet.from_services(_services()), ToolSet)

    def test_intent_gate_selects_screen_tool(self):
        tools = LegacyFunctionToolSet.from_services(_services())
        self.assertEqual(len(tools.schemas_for_user_text("帮我看看屏幕上有没有报错")), 1)

    def test_intent_gate_blocks_non_screen_text(self):
        tools = LegacyFunctionToolSet.from_services(_services())
        self.assertEqual(tools.schemas_for_user_text("你好"), [])

    def test_run_dispatches_to_function(self):
        calls = []
        tools = LegacyFunctionToolSet.from_services(
            _services(tool_fn=lambda **kw: calls.append(kw) or "result")
        )
        out = tools.run("inspect_screen", '{"target": "full_screen", "question": "q"}')
        self.assertEqual(out, "result")
        self.assertEqual(calls, [{"target": "full_screen", "question": "q"}])


class TurnDepsTest(unittest.TestCase):
    def test_from_services_maps_ports_and_tools(self):
        deps = TurnDeps.from_services(_services(), AppConfig())
        self.assertEqual((deps.llm, deps.tts, deps.visual, deps.memory), ("LLM", "TTS", "VIS", "MEM"))
        self.assertIsInstance(deps.tools, ToolSet)

    def test_observer_jobs_exec_are_non_none(self):
        deps = TurnDeps.from_services(_services(), AppConfig())
        self.assertIsNotNone(deps.observer)
        self.assertIsNotNone(deps.jobs)
        self.assertIsNotNone(deps.exec_strategy)

    def test_observer_placeholder_is_usable(self):
        observer = TurnDeps.from_services(_services(), AppConfig()).observer
        with observer.span("stage"):
            observer.mark("first_unit_ms", 1.0)
        self.assertEqual(observer.snapshot(), {})

    def test_jobs_placeholder_runs_inline(self):
        deps = TurnDeps.from_services(_services(), AppConfig())
        ran = []
        deps.jobs.submit(lambda: ran.append(1))
        deps.jobs.drain()
        self.assertEqual(ran, [1])

    def test_tools_run_through_deps(self):
        calls = []
        deps = TurnDeps.from_services(
            _services(tool_fn=lambda **kw: calls.append(kw) or "ok"), AppConfig()
        )
        self.assertEqual(deps.tools.run("inspect_screen", "{}"), "ok")
        self.assertEqual(calls, [{}])


if __name__ == "__main__":
    unittest.main()
