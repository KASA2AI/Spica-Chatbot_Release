"""P1: multi-round tool loop on the REAL streaming chain (plan D, chainable flag).

Assembly mirrors test_chat_tool_round / test_note_game_observation (= the
verify_watch_chain scene-B shape): real CapabilityRegistry + real ChatEngine +
fake chat client recording every request. Single-tool zero-change is guarded by
test_chat_tool_round.py staying green UNCHANGED (the hard gate); here:

1. Chain rounds: a chainable tool keeps the loop alive -- probe 3x (all
   non-streaming WITH tools), execute 2x, the no-calls probe's text IS the
   final answer (prefetched channel, zero streamed calls).
2. Non-chainable single-shot: exactly [probe(tools), followup(stream, no
   tools)] -- no third call, the P1 loop never engages.
3. Overflow: max_tool_rounds probes all return calls -> graceful forced final
   (streamed, no tools, prompt-noted), WARNING logged, turn ends in done.
4. Generic compaction (F4): inspect's registered compactor reproduces the
   legacy name-based special case byte for byte; small outputs untouched;
   oversized outputs hit the 8000-char head-tail cap.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions
from agent_tools.tts.schemas import TTSRequest, TTSResult
from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.config.schema import AppConfig
from spica.core.chat_engine import ChatEngine
from spica.plugins.registry import CapabilityRegistry
from spica.runtime.services import AgentServices
from spica.runtime.stages import (
    _compact_screen_tool_output,
    _compact_tool_history_for_prompt,
)
from spica.runtime.tool_round import build_tool_followup_prompt

RAW_ANSWER = json.dumps(
    {"answer": "翻完了。", "emotion": "happy", "emotion_reason": "x"},
    ensure_ascii=False,
)
QUESTION = "把这本翻三页"

FLIP_PAGE_SCHEMA = {
    "type": "function",
    "name": "flip_page",
    "strict": True,
    "description": "翻一页(测试用 chainable 工具)。",
    "parameters": {
        "type": "object",
        "properties": {"step": {"type": "integer", "description": "页码"}},
        "required": ["step"],
        "additionalProperties": False,
    },
}


class _ChainChatAPI:
    """Non-streaming probes WITH tools return a flip_page call while the budget
    lasts, then content; streamed calls return the final answer chunks."""

    def __init__(self, calls, tool_call_budget):
        self._calls = calls
        self._budget = tool_call_budget
        self._step = 0
        self.completions = self

    def create(self, **kwargs):
        self._calls.append(("chat.completions.create", kwargs))
        if kwargs.get("stream"):
            def chunks():
                yield SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=RAW_ANSWER))])
            return chunks()
        if kwargs.get("tools") and self._budget > 0:
            self._budget -= 1
            self._step += 1
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="",
                tool_calls=[SimpleNamespace(id=f"call_{self._step}", type="function",
                    function=SimpleNamespace(
                        name="flip_page",
                        arguments=json.dumps({"step": self._step})))],
            ))], usage=None)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=RAW_ANSWER))], usage=None)


def _deepseek_client(calls, tool_call_budget):
    return SimpleNamespace(base_url="https://api.deepseek.com/v1",
                           chat=_ChainChatAPI(calls, tool_call_budget))


class _FakeTTS:
    name = "fake_tts"

    def synthesize(self, request):
        assert isinstance(request, TTSRequest)
        return TTSResult(ok=True, provider=self.name, audio_url="/x.wav", audio_path="/tmp/x.wav",
                         chunks=[{"index": 0, "text": request.text, "audio_url": "/x.wav", "audio_path": "/tmp/x.wav"}],
                         timing={"tts_total_ms": 1.0}, duration_ms=1.0)


class _FakeVisual:
    def build_visual_payload(self, answer, emotion, requested_costume=None, requested_mode=None):
        return {"costume": "school", "classifier_version": "x", "cues": [{"index": 0, "text": answer}]}

    def prepare_stream_context(self, requested_costume=None, requested_mode=None):
        return {"costume": "school", "costume_mode": "fixed", "dialog": {}, "character": {}, "classifier_version": "x"}

    def build_unit_visual_payload(self, **kwargs):
        return {"costume": "school", "costume_mode": "fixed", "classifier_version": "x",
                "selection_source": "x", "selection_error": None,
                "classifier": {"duration_ms": 1.0, "confidence": 0.9, "signals": []},
                "dialog": {}, "character": {},
                "cue": {"index": kwargs["unit_index"], "text": kwargs["current_unit_text"],
                        "expression_id": "002", "hand_pose": "normal", "image_url": "/x.png", "reason": "x"}}


def _build_engine(client, tmp, *, chainable):
    executions = []
    registry = CapabilityRegistry()

    def flip_page(**kwargs):
        executions.append(kwargs)
        return {"page": kwargs.get("step")}

    registry.register_tool(FLIP_PAGE_SCHEMA, flip_page, available=lambda: True,
                           intent_gated=False, chainable=chainable)
    services = AgentServices(
        llm_client=client, tts_adapter=_FakeTTS(), visual_tool=_FakeVisual(),
        memory_store=SQLiteMemoryStore(Path(tmp) / "m.sqlite3"),
        recent_memory=RecentMemory(max_turns=3),
        game_memory_adapter=GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3"),
        config={"model": "test-model", "character_profile": "p", "recent_context_limit": 3,
                "long_term_memory_limit": 5, "max_tool_rounds": 3, "character_id": "spica",
                "interlocutor_name": "麦"},
        logger=lambda *a, **k: None,
        tool_functions=default_tool_functions(), tool_schemas=TOOL_SCHEMAS,
    )
    services.tool_registry = registry
    engine = ChatEngine(services, AppConfig())
    return engine, executions


def _stream(engine, question):
    events = list(engine.stream_voice(question))
    done = next((e for e in events if e.get("event") == "done"), None)
    error = next((e for e in events if e.get("event") == "error"), None)
    answer = (done or {}).get("data", {}).get("answer", "")
    return answer, error


def _nested_names(tools):
    return [(t.get("function") or {}).get("name") for t in tools]


class ChainRoundsTest(unittest.TestCase):
    """Contract 1: chainable tool -> the loop probes again after execution."""

    def test_two_chained_rounds_then_prefetched_answer(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            engine, executions = _build_engine(
                _deepseek_client(calls, tool_call_budget=2), tmp, chainable=True)
            answer, error = _stream(engine, QUESTION)

        self.assertIsNone(error)
        self.assertEqual(answer, "翻完了。")
        # Three probes, ALL non-streaming WITH tools; the final answer came from
        # probe 3's text (prefetched channel) -- ZERO streamed calls.
        self.assertEqual(len(calls), 3)
        for _method, kwargs in calls:
            self.assertNotIn("stream", kwargs)
            self.assertEqual(_nested_names(kwargs["tools"]), ["flip_page"])
        # The tool really ran twice, with the chained arguments.
        self.assertEqual([e["step"] for e in executions], [1, 2])
        # Round-2 probe carried round-1's result; round-3 carried both.
        # Outputs sit JSON-escaped inside the [TOOL_RESULTS] dump.
        round2_prompt = calls[1][1]["messages"][0]["content"]
        self.assertIn("[TOOL_RESULTS]", round2_prompt)
        self.assertIn('\\"page\\": 1', round2_prompt)
        round3_prompt = calls[2][1]["messages"][0]["content"]
        self.assertIn('\\"page\\": 1', round3_prompt)
        self.assertIn('\\"page\\": 2', round3_prompt)


class NonChainableSingleRoundTest(unittest.TestCase):
    """Contract 2: without the chainable flag the P1 loop never engages -- the
    request sequence stays exactly [probe(tools), followup(stream, no tools)]."""

    def test_single_shot_tool_keeps_two_call_sequence(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            engine, executions = _build_engine(
                _deepseek_client(calls, tool_call_budget=99), tmp, chainable=False)
            answer, error = _stream(engine, QUESTION)

        self.assertIsNone(error)
        self.assertEqual(answer, "翻完了。")
        self.assertEqual(len(calls), 2)  # no third call, ever
        probe = calls[0][1]
        self.assertNotIn("stream", probe)
        self.assertEqual(_nested_names(probe["tools"]), ["flip_page"])
        followup = calls[1][1]
        self.assertTrue(followup.get("stream"))
        self.assertNotIn("tools", followup)
        self.assertIn("[TOOL_RESULTS]", followup["messages"][0]["content"])
        self.assertEqual(len(executions), 1)


class LoopOverflowTest(unittest.TestCase):
    """Contract 3: budget exhausted -> graceful forced final, not an error."""

    def test_exceeded_forces_streamed_final_without_tools(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            engine, executions = _build_engine(
                _deepseek_client(calls, tool_call_budget=99), tmp, chainable=True)
            with self.assertLogs("spica.runtime.tool_round", level="WARNING") as logs:
                answer, error = _stream(engine, QUESTION)

        self.assertIsNone(error)  # she always answers -- no error event
        self.assertEqual(answer, "翻完了。")
        self.assertTrue(any("max_tool_rounds" in line for line in logs.output))
        # max_tool_rounds=3 probes (all returned calls, all executed) + the
        # forced final: streamed, WITHOUT tools, prompt-noted to stop.
        self.assertEqual(len(calls), 4)
        probes, final = calls[:3], calls[3][1]
        for _method, kwargs in probes:
            self.assertNotIn("stream", kwargs)
            self.assertIn("tools", kwargs)
        self.assertTrue(final.get("stream"))
        self.assertNotIn("tools", final)
        final_prompt = final["messages"][0]["content"]
        self.assertIn("不要再调用工具", final_prompt)
        self.assertIn('\\"page\\": 3', final_prompt)  # JSON-escaped inside [TOOL_RESULTS]
        self.assertEqual([e["step"] for e in executions], [1, 2, 3])


class GenericCompactionTest(unittest.TestCase):
    """Contract 4 (F4): two-layer compaction preserves today's behaviour."""

    _OBSERVATION = {
        "ok": True,
        "data": {
            "schema_version": "screen_observation.v1",
            "request": {"user_question": "q" * 500, "question_type": "x", "target": "screen"},
            "capture": {"captured_scope": "window", "source": "automatic_screenshot"},
            "answer": {"text": "a girl"},
            "followup": {"context_for_next_turn": "..."},
        },
    }

    def test_inspect_registered_compactor_equals_legacy_special_case(self):
        history = [{"name": "inspect_screen", "arguments": "{}",
                    "output": json.dumps(self._OBSERVATION, ensure_ascii=False)}]
        legacy = _compact_tool_history_for_prompt([dict(h) for h in history])
        via_lookup = _compact_tool_history_for_prompt(
            [dict(h) for h in history],
            lambda name: _compact_screen_tool_output if name == "inspect_screen" else None,
        )
        self.assertEqual(legacy, via_lookup)  # byte-identical either path

    def test_small_outputs_untouched_and_oversized_capped(self):
        small = {"name": "watch_game_screen", "arguments": "{}", "output": "x" * 2000}
        self.assertEqual(
            _compact_tool_history_for_prompt([dict(small)])[0]["output"], "x" * 2000)
        huge = {"name": "browse_page", "arguments": "{}", "output": "y" * 9000}
        capped = _compact_tool_history_for_prompt([dict(huge)])[0]["output"]
        self.assertIn("...[truncated 1000 chars]...", capped)
        self.assertTrue(capped.startswith("y" * 100) and capped.endswith("y" * 100))
        self.assertLess(len(capped), 9000)

    def test_force_final_prompt_carries_stop_note(self):
        history = [{"name": "t", "arguments": "{}", "output": "{}"}]
        plain = build_tool_followup_prompt("p", history)
        forced = build_tool_followup_prompt("p", history, force_final=True)
        self.assertNotIn("不要再调用工具", plain)
        self.assertIn("不要再调用工具，基于已有结果回答。", forced)


class RegistryChainableFlagTest(unittest.TestCase):
    def test_chainable_and_compactor_default_off(self):
        registry = CapabilityRegistry()
        registry.register_tool(FLIP_PAGE_SCHEMA, lambda **k: {})
        self.assertFalse(registry.tool_chainable("flip_page"))
        self.assertIsNone(registry.tool_compact_output("flip_page"))
        self.assertFalse(registry.tool_chainable("unknown_tool"))

    def test_chainable_and_compactor_declared(self):
        registry = CapabilityRegistry()
        compactor = lambda output: output[:3]  # noqa: E731
        registry.register_tool(FLIP_PAGE_SCHEMA, lambda **k: {},
                               chainable=True, compact_output=compactor)
        self.assertTrue(registry.tool_chainable("flip_page"))
        self.assertIs(registry.tool_compact_output("flip_page"), compactor)


if __name__ == "__main__":
    unittest.main()
