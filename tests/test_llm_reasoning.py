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
from spica.host.builtins import register_core_capability_catalogue
from spica.plugins.registry import CapabilityRegistry


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

    def test_deepseek_v4_levels_are_explicit_and_older_models_keep_the_default(self):
        for requested, effective in (("low", "low"), ("medium", "high"), ("high", "high")):
            self.assertEqual(_reasoning_chat_kwargs("deepseek-v4-flash", requested),
                {"extra_body": {"thinking": {"type": "enabled"}}, "reasoning_effort": effective})
            self.assertEqual(_reasoning_chat_kwargs("deepseek-chat", requested), {})

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
            "prompt", model="deepseek-v4-flash", state=_stream_state("chat")
        ))
        self.assertEqual("".join(result), "valid answer")
        self.assertEqual([call["stream"] for call in client.calls], [True, False])

    def test_empty_stream_and_empty_fallback_are_not_a_successful_reply(self):
        client = self.client("")
        with self.assertRaisesRegex(RuntimeError, "no answer content"):
            list(OpenAICompatibleAdapter(client).stream(
                "prompt", model="deepseek-v4-flash", state=_stream_state("chat")
            ))
        self.assertEqual(len(client.calls), 2)

    def test_healthy_stream_needs_no_extra_request(self):
        client = _RecordingClient()
        result = list(OpenAICompatibleAdapter(client).stream(
            "prompt", model="deepseek-v4-flash", state=_stream_state("chat")
        ))
        self.assertEqual(result, ["ok"])
        self.assertEqual(len(client.calls), 1)


def _stream_state(interaction_mode=None, *, source=None):
    request = None
    if interaction_mode is not None:
        request = SimpleNamespace(interaction_mode=interaction_mode, source=source)
    return SimpleNamespace(timing={}, response_id=None, request=request)


class SystemTurnReasoningLaneTest(unittest.TestCase):
    def _adapter(self, client, *, main="default", system="none"):
        return OpenAICompatibleAdapter(
            client,
            reasoning_effort=main,
            system_turn_reasoning_effort=system,
        )

    def test_system_stream_uses_dedicated_deepseek_none(self):
        client = _RecordingClient()
        adapter = self._adapter(client)

        list(adapter.stream(
            "system prompt",
            model="deepseek-v4-flash",
            state=_stream_state("system", source="song"),
        ))

        self.assertEqual(
            client.calls[0]["extra_body"],
            {"thinking": {"type": "disabled"}},
        )

    def test_chat_stream_uses_main_default_even_for_system_like_source_and_prompt(self):
        client = _RecordingClient()
        adapter = self._adapter(client)

        list(adapter.stream(
            "system proactive prompt",
            model="deepseek-v4-flash",
            state=_stream_state("chat", source="song"),
        ))

        self.assertNotIn("extra_body", client.calls[0])

    def test_missing_request_falls_back_to_main_reasoning(self):
        client = _RecordingClient()
        adapter = self._adapter(client)

        list(adapter.stream(
            "system-looking prompt",
            model="deepseek-v4-flash",
            state=SimpleNamespace(timing={}, response_id=None),
        ))

        self.assertNotIn("extra_body", client.calls[0])

    def test_tool_probe_streaming_probe_and_complete_ignore_the_system_lane(self):
        client = _RecordingClient()
        adapter = self._adapter(client)

        adapter.probe(
            "probe",
            [],
            model="deepseek-v4-flash",
            state=_stream_state("system"),
        )
        streamed_probe = adapter.probe_stream(
            "streaming probe",
            [],
            model="deepseek-v4-flash",
            state=_stream_state("system"),
        )
        self.assertIsNotNone(streamed_probe)
        list(streamed_probe.deltas)
        adapter.complete("summary", model="deepseek-v4-flash")

        self.assertEqual(len(client.calls), 3)
        for call in client.calls:
            self.assertNotIn("extra_body", call)

    def test_none_dedicated_value_inherits_the_main_reasoning(self):
        client = _RecordingClient()
        adapter = self._adapter(client, main="none", system=None)

        list(adapter.stream(
            "system prompt",
            model="deepseek-v4-flash",
            state=_stream_state("system"),
        ))

        self.assertEqual(
            client.calls[0]["extra_body"],
            {"thinking": {"type": "disabled"}},
        )

    def test_per_request_selection_does_not_mutate_adapter_configuration(self):
        client = _RecordingClient()
        adapter = self._adapter(client)
        before = (adapter._reasoning_effort, adapter._system_turn_reasoning_effort)

        list(adapter.stream(
            "system prompt",
            model="deepseek-v4-flash",
            state=_stream_state("system"),
        ))
        list(adapter.stream(
            "chat prompt",
            model="deepseek-v4-flash",
            state=_stream_state("chat"),
        ))

        self.assertEqual(
            (adapter._reasoning_effort, adapter._system_turn_reasoning_effort),
            before,
        )
        self.assertIn("extra_body", client.calls[0])
        self.assertNotIn("extra_body", client.calls[1])

    def test_builtin_factory_forwards_both_reasoning_values(self):
        registry = CapabilityRegistry()
        register_core_capability_catalogue(registry)

        adapter = registry.resolve_llm(
            "openai_compatible",
            client=_RecordingClient(),
            reasoning_effort="default",
            system_turn_reasoning_effort="none",
        )

        self.assertEqual(adapter._reasoning_effort, "default")
        self.assertEqual(adapter._system_turn_reasoning_effort, "none")


if __name__ == "__main__":
    unittest.main()
