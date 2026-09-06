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
        if kwargs.get("stream"):
            return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="ok"))])])
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


class EmptyChatStreamTest(unittest.TestCase):
    def client(self, fallback_text):
        client = _RecordingClient()

        def create(**kwargs):
            client.calls.append(kwargs)
            if kwargs.get("stream"):
                return iter([SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=None, reasoning_content="not a final answer"),
                    finish_reason="stop",
                )])])
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=fallback_text)
            )], usage=None)

        client.chat.create = create
        return client

    def test_reasoning_only_stream_uses_one_existing_answer_fallback(self):
        client = self.client("valid answer")
        result = list(OpenAICompatibleAdapter(client).stream(
            "prompt", model="deepseek-v4-flash", state=_stream_state()
        ))
        self.assertEqual("".join(result), "valid answer")
        self.assertEqual([call["stream"] for call in client.calls], [True, False])

    def test_empty_stream_and_empty_fallback_are_not_a_successful_reply(self):
        client = self.client("")
        with self.assertRaisesRegex(RuntimeError, "no answer content"):
            list(OpenAICompatibleAdapter(client).stream(
                "prompt", model="deepseek-v4-flash", state=_stream_state()
            ))
        self.assertEqual(len(client.calls), 2)

    def test_healthy_stream_needs_no_extra_request(self):
        client = _RecordingClient()
        result = list(OpenAICompatibleAdapter(client).stream(
            "prompt", model="deepseek-v4-flash", state=_stream_state()
        ))
        self.assertEqual(result, ["ok"])
        self.assertEqual(len(client.calls), 1)


def _stream_state():
    return SimpleNamespace(timing={}, response_id=None)


if __name__ == "__main__":
    unittest.main()
