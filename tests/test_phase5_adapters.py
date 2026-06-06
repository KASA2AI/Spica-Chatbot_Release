"""Phase 5: ports / registry / adapter contract tests.

Covers the swap-engine acceptance (resolve a capability by config name) and the
LLM / memory adapter behaviour that was moved out of the pipeline. Fakes are
self-contained.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from spica.adapters.llm import OpenAICompatibleAdapter
from spica.adapters.memory import SqliteMemoryAdapter
from spica.plugins.registry import CapabilityRegistry
from spica.ports.memory import MemoryItem, MemoryScope


# --- LLM fakes ------------------------------------------------------------

class _FakeResp:
    def __init__(self, text):
        self.id = "r"
        self.output_text = text
        self.output = []
        self.usage = SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2)


class _FakeResponses:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            chunks = [self.text[i:i + 5] for i in range(0, len(self.text), 5)]
            events = [SimpleNamespace(type="response.output_text.delta", delta=c) for c in chunks]
            events.append(SimpleNamespace(type="response.completed", response=_FakeResp(self.text)))
            return iter(events)
        return _FakeResp(self.text)


class _FakeOpenAI:
    def __init__(self, text="hello world"):
        self.responses = _FakeResponses(text)


class _FakeChatCompletions:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            chunks = [self.text[i:i + 4] for i in range(0, len(self.text), 4)]
            return iter(
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=c))])
                for c in chunks
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.text))],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
        )


class _FakeDeepSeek:
    def __init__(self, text="やあ"):
        self.base_url = "https://api.deepseek.com/v1"
        self.chat = SimpleNamespace(completions=_FakeChatCompletions(text))


def _state():
    return SimpleNamespace(timing={}, response_id=None, raw_model_output=None)


class LLMAdapterTest(unittest.TestCase):
    def test_prefers_chat_completions_branch(self):
        self.assertFalse(OpenAICompatibleAdapter(_FakeOpenAI()).prefers_chat_completions())
        self.assertTrue(OpenAICompatibleAdapter(_FakeDeepSeek()).prefers_chat_completions())

    def test_iter_response_text_streams_openai(self):
        adapter = OpenAICompatibleAdapter(_FakeOpenAI("hello world"))
        out = "".join(adapter.iter_response_text({"model": "m", "input": "hi"}, _state()))
        self.assertEqual(out, "hello world")

    def test_iter_response_text_deepseek_uses_chat_completions(self):
        adapter = OpenAICompatibleAdapter(_FakeDeepSeek("こんにちは"))
        state = _state()
        out = "".join(adapter.iter_response_text({"model": "m", "input": "hi"}, state))
        self.assertEqual(out, "こんにちは")
        self.assertEqual(
            state.timing["llm_stream_fallback_reason"], "chat_completions_compatible_client"
        )

    def test_complete_chat_and_create_responses(self):
        ds = _FakeDeepSeek("テスト")
        adapter = OpenAICompatibleAdapter(ds)
        self.assertEqual(adapter.complete_chat("m", "hi", _state()), "テスト")
        self.assertEqual(ds.chat.completions.calls[0]["messages"][0]["role"], "user")

        op = _FakeOpenAI("x")
        OpenAICompatibleAdapter(op).create_responses(model="m", input="hi")
        self.assertEqual(op.responses.calls[0]["model"], "m")


class MemoryAdapterTest(unittest.TestCase):
    def test_commit_turn_extracts_and_retrieve_returns_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMemoryStore(Path(tmp) / "m.sqlite3")
            adapter = SqliteMemoryAdapter(store, RecentMemory(max_turns=3))
            scope = MemoryScope(character_id="spica", user_id="麦", conversation_id="c1")

            result = adapter.commit_turn(scope, "我喜欢简短回答", "うん。", meta={"interlocutor_name": "麦"})
            self.assertIsInstance(result, dict)

            items = adapter.retrieve(scope, "简短", limit=5)
            self.assertTrue(items)
            self.assertIsInstance(items[0], MemoryItem)
            self.assertIn("简短", items[0].text)

    def test_optional_hooks_are_safe_noops(self):
        adapter = SqliteMemoryAdapter(store=None)
        scope = MemoryScope(character_id="spica", user_id="麦", conversation_id="c1")
        self.assertIsNone(adapter.get_context_block(scope))
        self.assertIsNone(adapter.run_maintenance(scope, "idle"))
        self.assertTrue(adapter.supports("commit_turn"))
        self.assertFalse(adapter.supports("sleep_consolidation"))


class CapabilityRegistryTest(unittest.TestCase):
    def test_resolve_by_name_swaps_engine(self):
        registry = CapabilityRegistry()
        registry.register_llm("openai_compatible", lambda client=None: OpenAICompatibleAdapter(client))
        registry.register_llm("echo", lambda client=None: SimpleNamespace(name="echo", client=client))

        default = registry.resolve_llm("openai_compatible", client="C1")
        swapped = registry.resolve_llm("echo", client="C2")
        self.assertIsInstance(default, OpenAICompatibleAdapter)
        self.assertEqual(swapped.name, "echo")
        self.assertEqual(swapped.client, "C2")

    def test_unknown_name_raises_with_available(self):
        registry = CapabilityRegistry()
        registry.register_tts("dummy", lambda **_: object())
        with self.assertRaises(KeyError):
            registry.resolve_tts("nope")
        self.assertEqual(registry.list_adapters("tts"), ["dummy"])

    def test_builtin_host_registrations(self):
        from spica.host.app_host import AppHost

        host = AppHost()
        self.assertEqual(host.registry.list_adapters("llm"), ["openai_compatible"])
        self.assertIn("dummy", host.registry.list_adapters("tts"))
        self.assertIn("gptsovits_current", host.registry.list_adapters("tts"))
        self.assertEqual(host.registry.list_adapters("visual"), ["spica_diff"])
        self.assertEqual(host.registry.list_adapters("memory"), ["sqlite"])


if __name__ == "__main__":
    unittest.main()
