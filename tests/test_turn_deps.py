"""C3a: typed turn entry (TurnRequest / TurnDeps / ToolSet).

Locks the new typed surface introduced in C3a:
- TurnRequest defaults;
- RegistryToolSet keeps the intent gate and dispatches runs (registry-backed; for
  the legacy table it behaves exactly like the old TOOL_SCHEMAS / run_local_tool);
- TurnDeps.from_services maps the resolved ports and guarantees the
  observer / jobs / exec_strategy placeholders are non-None and usable.
"""

import unittest
from types import SimpleNamespace

from agent_tools.function_tools import TOOL_SCHEMAS
from spica.config.schema import AppConfig
from spica.runtime.context import TurnRequest
from spica.runtime.deps import TurnDeps
from spica.runtime.tools import RegistryToolSet, ToolSet


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


class RegistryToolSetTest(unittest.TestCase):
    @staticmethod
    def _toolset(tool_fn=None):
        s = _services(tool_fn=tool_fn)
        return RegistryToolSet.from_function_table(s.tool_schemas, s.tool_functions)

    def test_is_a_toolset(self):
        self.assertIsInstance(self._toolset(), ToolSet)

    def test_intent_gate_selects_screen_tool(self):
        self.assertEqual(len(self._toolset().schemas_for_user_text("帮我看看屏幕上有没有报错")), 1)

    def test_intent_gate_blocks_non_screen_text(self):
        self.assertEqual(self._toolset().schemas_for_user_text("你好"), [])

    def test_run_dispatches_to_function_and_passes_string_through(self):
        calls = []
        tools = self._toolset(tool_fn=lambda **kw: calls.append(kw) or "result")
        out = tools.run("inspect_screen", '{"target": "full_screen", "question": "q"}')
        self.assertEqual(out, "result")  # legacy str handler -> passed through, not re-wrapped
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

    def test_from_services_resolves_raw_client_into_a_port(self):
        services = SimpleNamespace(
            llm_adapter=None, llm_client=SimpleNamespace(),
            tts_adapter=None, visual_tool=None,
            memory_adapter=None, memory_store=object(), recent_memory=object(),
            tool_schemas=TOOL_SCHEMAS, tool_functions={"inspect_screen": lambda **k: "ok"},
        )
        deps = TurnDeps.from_services(services, AppConfig())
        # raw client / store got wrapped into resolved ports (no dual-field downstream)
        self.assertIsNotNone(deps.llm)
        self.assertIsNotNone(deps.memory)


class TurnDepsLegacyBridgeTest(unittest.TestCase):
    def _legacy_services(self, **config):
        return SimpleNamespace(
            llm_adapter=None, llm_client=SimpleNamespace(),
            tts_adapter=None, visual_tool=None,
            memory_adapter=None, memory_store=object(), recent_memory=object(),
            tool_schemas=TOOL_SCHEMAS, tool_functions={"inspect_screen": lambda **k: "ok"},
            config=config,
        )

    def test_reverse_maps_dict_config(self):
        deps = TurnDeps.from_legacy_services(self._legacy_services(
            model="m", max_long_term_memories=7, play_unit_min_chars=20,
            interlocutor_name="麦", character_id="spica2",
        ))
        self.assertEqual(deps.config.llm.model, "m")
        self.assertEqual(deps.config.memory.max_long_term_memories, 7)
        self.assertEqual(deps.config.stream.play_unit_min_chars, 20)
        self.assertEqual(deps.config.character.interlocutor_name, "麦")
        self.assertEqual(deps.config.character.character_id, "spica2")
        self.assertIsNotNone(deps.llm)  # raw client wrapped
        self.assertIsNotNone(deps.memory)

    def test_empty_dict_uses_historical_defaults(self):
        deps = TurnDeps.from_legacy_services(self._legacy_services())
        self.assertEqual(deps.config.stream.play_unit_min_chars, 18)
        self.assertEqual(deps.config.stream.play_unit_max_chars, 96)
        self.assertEqual(deps.config.stream.visual_stream_workers, 2)
        self.assertEqual(deps.config.memory.max_long_term_memories, 200)


if __name__ == "__main__":
    unittest.main()
