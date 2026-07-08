"""Per-provider reasoning/thinking control on the LLM adapter.

deepseek thinking is binary (none = OFF, levels = ON); gpt uses a reasoning_effort
gradient. "default" sends NOTHING (provider's own default -> zero-diff). The model
NAME (not base_url) picks the provider, so a deepseek main + gpt judge each get the
right param off the SAME adapter class.
"""

import unittest
from types import SimpleNamespace

from spica.adapters.llm.openai_compatible import (
    OpenAICompatibleAdapter,
    _reasoning_chat_kwargs,
    _reasoning_responses_kwargs,
)


class ReasoningKwargsTest(unittest.TestCase):
    def test_default_sends_nothing(self):
        self.assertEqual(_reasoning_chat_kwargs("deepseek-v4-flash", "default"), {})
        self.assertEqual(_reasoning_chat_kwargs("gpt-5.4-mini", "default"), {})
        self.assertEqual(_reasoning_responses_kwargs("gpt-5.4-mini", "default"), {})

    def test_deepseek_none_disables_thinking(self):
        self.assertEqual(
            _reasoning_chat_kwargs("deepseek-v4-flash", "none"),
            {"extra_body": {"thinking": {"type": "disabled"}}},
        )

    def test_deepseek_levels_leave_thinking_on(self):
        # deepseek is binary -- low/medium/high are NOT a gradient, just "on" (=send
        # nothing, the provider default is thinking-on).
        for level in ("low", "medium", "high"):
            self.assertEqual(_reasoning_chat_kwargs("deepseek-v4-flash", level), {})

    def test_gpt_effort_chat_and_responses(self):
        self.assertEqual(_reasoning_chat_kwargs("gpt-5.4-mini", "medium"), {"reasoning_effort": "medium"})
        self.assertEqual(_reasoning_chat_kwargs("gpt-5.4-mini", "none"), {"reasoning_effort": "none"})
        self.assertEqual(
            _reasoning_responses_kwargs("gpt-5.4-mini", "low"), {"reasoning": {"effort": "low"}}
        )

    def test_deepseek_has_no_responses_reasoning(self):
        # deepseek uses chat in this app; the responses helper never emits for it.
        self.assertEqual(_reasoning_responses_kwargs("deepseek-v4-flash", "none"), {})

    def test_unknown_model_sends_nothing(self):
        self.assertEqual(_reasoning_chat_kwargs("mistral-large", "medium"), {})
        self.assertEqual(_reasoning_responses_kwargs("mistral-large", "high"), {})


class _RecordingChat:
    def __init__(self, sink):
        self._sink = sink
        self.completions = self

    def create(self, **kwargs):
        self._sink.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))], usage=None)


class _RecordingResponses:
    def __init__(self, sink):
        self._sink = sink

    def create(self, **kwargs):
        self._sink.append(kwargs)
        return SimpleNamespace(output_text="ok", output=[], id="r", usage=None)


class _RecordingClient:
    def __init__(self):
        self.calls = []
        self.chat = _RecordingChat(self.calls)
        self.responses = _RecordingResponses(self.calls)
        self.base_url = "https://api.deepseek.com/v1"


class AdapterInjectionTest(unittest.TestCase):
    def test_complete_chat_injects_deepseek_thinking_off(self):
        client = _RecordingClient()
        OpenAICompatibleAdapter(client, reasoning_effort="none").complete_chat(
            "deepseek-v4-flash", "hi", SimpleNamespace(timing={}))
        self.assertEqual(client.calls[0]["extra_body"], {"thinking": {"type": "disabled"}})

    def test_complete_chat_injects_gpt_effort(self):
        client = _RecordingClient()
        OpenAICompatibleAdapter(client, reasoning_effort="medium").complete_chat(
            "gpt-5.4-mini", "hi", SimpleNamespace(timing={}))
        self.assertEqual(client.calls[0]["reasoning_effort"], "medium")

    def test_chat_tool_probe_injects(self):
        client = _RecordingClient()
        OpenAICompatibleAdapter(client, reasoning_effort="none").create_chat_with_tools(
            model="deepseek-v4-flash", prompt="hi", tools=[], state=SimpleNamespace(timing={}))
        self.assertEqual(client.calls[0]["extra_body"], {"thinking": {"type": "disabled"}})

    def test_create_responses_injects_gpt(self):
        client = _RecordingClient()
        OpenAICompatibleAdapter(client, reasoning_effort="high").create_responses(
            model="gpt-5.4-mini", input="hi")
        self.assertEqual(client.calls[0]["reasoning"], {"effort": "high"})

    def test_default_injects_nothing(self):
        client = _RecordingClient()
        OpenAICompatibleAdapter(client).complete_chat(  # default reasoning_effort
            "deepseek-v4-flash", "hi", SimpleNamespace(timing={}))
        self.assertNotIn("extra_body", client.calls[0])
        self.assertNotIn("reasoning_effort", client.calls[0])


if __name__ == "__main__":
    unittest.main()
