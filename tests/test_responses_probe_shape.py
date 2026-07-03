"""Phase 0 characterization: Responses-API request shapes (OO migration).

Pins two client-level request shapes the Phase 7 ToolCallingModel flip must
preserve byte for byte:

(a) the TOOL PROBE sends the registry-provided schemas VERBATIM as the ``tools``
    payload of ``client.responses.create`` (plus round accounting and the
    "[TOOL_RESULTS]" followup prompt after a single-shot tool);
(b) a NO-TOOL final request carries ``stream=True`` and NO ``tools`` key at all
    (driven at the adapter level via ``iter_response_text`` -- with no tools,
    ``prepare_prompt_for_streaming`` returns without issuing any request, so it
    cannot carry this assertion).

HARD RULE (migration plan Phase 0 #3): the fake sits at the OpenAI CLIENT layer
(``client.responses.create`` records kwargs, after tests/test_turn_contract.py's
fakes) -- never at the LLMPort layer, where a fake would stop measuring anything
once Phase 7 swaps the port implementation.
"""

import json
import unittest
from types import SimpleNamespace

from spica.adapters.llm.openai_compatible import OpenAICompatibleAdapter
from spica.config.schema import AppConfig, CharacterConfig
from spica.plugins.registry import CapabilityRegistry
from spica.runtime.context import PromptBundle, TurnContext, TurnRequest
from spica.runtime.deps import TurnDeps
from spica.runtime.observer import DefaultTurnObserver
from spica.runtime.tool_round import prepare_prompt_for_streaming
from spica.runtime.tools import RegistryToolSet

BASE_PROMPT = "[CURRENT_USER_INPUT]\n看看这个"

# A self-contained flat tool schema (top-level name/description/parameters --
# the registry stores and offers it verbatim; that verbatim pass-through is
# exactly what assertion (a) pins).
ECHO_SCHEMA = {
    "type": "function",
    "name": "echo_probe",
    "description": "Phase 0 fixture: echo the given text back.",
    "parameters": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
}


class _RecordingResponses:
    """OpenAI-Responses fake: records every create(**kwargs), answers per script."""

    def __init__(self, script):
        self.calls = []
        self._script = script

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._script(kwargs)


class _FakeClient:
    base_url = ""  # not a deepseek endpoint -> the adapter takes the Responses path

    def __init__(self, script):
        self.responses = _RecordingResponses(script)


class ToolProbeShapeTest(unittest.TestCase):
    """(a) Tool probe shape via prepare_prompt_for_streaming + the REAL adapter.
    No orchestrator, no TTS, no visual."""

    def _run_probe(self):
        def script(kwargs):
            # The (single) non-stream probe answers with one echo_probe call.
            return SimpleNamespace(
                id="probe-1",
                output_text="",
                output=[
                    SimpleNamespace(
                        type="function_call",
                        name="echo_probe",
                        arguments='{"text": "hi"}',
                    )
                ],
                usage=None,
            )

        client = _FakeClient(script)
        registry = CapabilityRegistry()
        # intent_gated=False -> supplied regardless of the router wordlist, so
        # this test does not couple to the wordlist's vocabulary.
        registry.register_tool(
            ECHO_SCHEMA, lambda text="": f"echoed:{text}", intent_gated=False
        )

        ctx = TurnContext(TurnRequest(user_input="看看这个", conversation_id="default"))
        ctx.prompt = PromptBundle(prompt_input=BASE_PROMPT)
        deps = TurnDeps(
            config=AppConfig(
                character=CharacterConfig(character_id="spica", interlocutor_name="麦")
            ),
            llm=OpenAICompatibleAdapter(client),
            tts=None,
            visual=None,
            memory=None,
            tools=RegistryToolSet(registry),
            observer=DefaultTurnObserver(ctx.timing),
        )
        services = SimpleNamespace(llm_client=client, tool_schemas=[])
        statuses = []
        prompt, stream = prepare_prompt_for_streaming(
            ctx, services, lambda kind, text: statuses.append((kind, text)), deps
        )
        return client, registry, ctx, prompt, stream

    def test_probe_tools_payload_is_registry_schemas_verbatim(self):
        client, registry, _ctx, _prompt, _stream = self._run_probe()
        self.assertEqual(len(client.responses.calls), 1)  # one probe, no other request
        kwargs = client.responses.calls[0]
        self.assertNotIn("stream", kwargs)  # the probe is the non-streaming one-shot
        self.assertEqual(kwargs["input"], BASE_PROMPT)
        # The tools payload is the registry's offering, byte for byte.
        self.assertEqual(kwargs["tools"], registry.tool_schemas())
        self.assertEqual(
            json.dumps(kwargs["tools"], ensure_ascii=False, sort_keys=True),
            json.dumps(registry.tool_schemas(), ensure_ascii=False, sort_keys=True),
        )

    def test_probe_round_accounting_marks(self):
        _client, _registry, ctx, _prompt, _stream = self._run_probe()
        self.assertEqual(ctx.timing["agent_rounds"], 1)
        self.assertIn("agent_response_initial_ms", ctx.timing)

    def test_single_shot_tool_followup_prompt_carries_tool_results(self):
        _client, _registry, _ctx, prompt, stream = self._run_probe()
        self.assertIsNone(stream)  # single-shot Responses path returns a prompt, no generator
        self.assertTrue(prompt.startswith(BASE_PROMPT))
        self.assertIn("[TOOL_RESULTS]", prompt)
        self.assertIn("echoed:hi", prompt)  # the executed tool's output reached the followup


class NoToolFinalRequestShapeTest(unittest.TestCase):
    """(b) Adapter-level: a no-tool final request has stream=True and NO tools key."""

    def test_final_request_has_stream_true_and_no_tools_key(self):
        def script(kwargs):
            # Streaming create -> an iterator of Responses streaming events.
            return iter(
                [
                    SimpleNamespace(type="response.output_text.delta", delta="你好"),
                    SimpleNamespace(type="response.output_text.delta", delta="呀"),
                    SimpleNamespace(
                        type="response.completed",
                        response=SimpleNamespace(id="final-1", usage=None),
                    ),
                ]
            )

        client = _FakeClient(script)
        adapter = OpenAICompatibleAdapter(client)
        state = SimpleNamespace(timing={}, response_id=None)

        text = "".join(
            adapter.iter_response_text({"model": "gpt-4.1-mini", "input": "你好"}, state)
        )

        self.assertEqual(text, "你好呀")
        self.assertEqual(len(client.responses.calls), 1)
        kwargs = client.responses.calls[0]
        self.assertIs(kwargs["stream"], True)
        self.assertNotIn("tools", kwargs)


if __name__ == "__main__":
    unittest.main()
