import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from memory.store import SQLiteMemoryStore
from agent.nodes import build_prompt_node, call_llm_node, validate_input_node
from memory.recent import RecentMemory
from agent.runtime import run_voice_pipeline
from agent.state import AgentServices, AgentState
from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions
from agent_tools.tts.schemas import TTSRequest, TTSResult


class FakeResponse:
    def __init__(self, text):
        self.id = "fake-response"
        self.output_text = text
        self.output = []
        self.usage = SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15)


class FakeResponses:
    def __init__(self, text='{"answer":"こんにちは。","emotion":"happy","emotion_reason":"普通の挨拶。"}'):
        self.text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self.text)


class FakeLLMClient:
    def __init__(self, text='{"answer":"こんにちは。","emotion":"happy","emotion_reason":"普通の挨拶。"}'):
        self.responses = FakeResponses(text)


class FakeChatCompletions:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.text))],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
        )


class FakeChat:
    def __init__(self, text):
        self.completions = FakeChatCompletions(text)


class FakeDeepSeekClient:
    def __init__(self, text='{"answer":"こんにちは。","emotion":"happy","emotion_reason":"普通の挨拶。"}'):
        self.base_url = "https://api.deepseek.com/v1"
        self.chat = FakeChat(text)


class FakeTTS:
    name = "fake_tts"

    def __init__(self):
        self.requests = []

    def synthesize(self, request):
        self.requests.append(request)
        assert isinstance(request, TTSRequest)
        return TTSResult(
            ok=True,
            provider=self.name,
            audio_url="/static/generated_voice/fake.wav",
            audio_path="/tmp/fake.wav",
            chunks=[
                {
                    "index": 0,
                    "text": request.text,
                    "audio_url": "/static/generated_voice/fake.wav",
                    "audio_path": "/tmp/fake.wav",
                }
            ],
            timing={"tts_total_ms": 1.0},
            metadata={
                "sampling_rate": 32000,
                "tts_param": {"speed": 1},
                "reference": {"prompt_text": "ref"},
            },
        )


class FakeVisual:
    def build_visual_payload(self, answer, emotion, requested_costume=None, requested_mode=None):
        return {
            "costume": requested_costume or "校服spica",
            "classifier_version": "fake-local",
            "cues": [{"index": 0, "text": answer}],
        }


def make_services(tmpdir, llm=None, tts=None, visual=None):
    return AgentServices(
        llm_client=llm or FakeLLMClient(),
        tts_adapter=tts,
        visual_tool=visual,
        memory_store=SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3"),
        recent_memory=RecentMemory(max_turns=3),
        config={
            "model": "fake-model",
            "character_profile": "profile",
            "recent_context_limit": 3,
            "long_term_memory_limit": 5,
            "max_tool_rounds": 2,
        },
        logger=lambda *args, **kwargs: None,
        tool_functions=default_tool_functions(),
        tool_schemas=TOOL_SCHEMAS,
    )


class PipelineSmokeTest(unittest.TestCase):
    def test_empty_input_returns_compatible_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            services = make_services(tmpdir)
            state = run_voice_pipeline(AgentState(conversation_id="c1", user_input=""), services)
            self.assertEqual(state.response_payload["error"]["code"], "EMPTY_MESSAGE")
            self.assertEqual(state.response_payload["audio_url"], None)

    def test_normal_chat_does_not_pass_tools(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            llm = FakeLLMClient()
            services = make_services(tmpdir, llm=llm)
            state = AgentState(conversation_id="c1", user_input="你好")
            state = validate_input_node(state, services)
            state = build_prompt_node(state, services)
            state = call_llm_node(state, services)
            self.assertNotIn("tools", llm.responses.calls[0])
            self.assertFalse(state.metadata["use_tools"])

    def test_tool_requests_pass_tools(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            llm = FakeLLMClient()
            services = make_services(tmpdir, llm=llm)
            state = AgentState(conversation_id="c1", user_input="现在几点")
            state = validate_input_node(state, services)
            state = build_prompt_node(state, services)
            state = call_llm_node(state, services)
            self.assertIn("tools", llm.responses.calls[0])
            self.assertTrue(state.metadata["use_tools"])

    def test_pipeline_returns_compatible_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tts = FakeTTS()
            services = make_services(tmpdir, tts=tts, visual=FakeVisual())
            state = run_voice_pipeline(AgentState(conversation_id="c1", user_input="你好"), services)
            payload = state.response_payload
            for key in ("answer", "conversation_id", "emotion", "audio_url", "visual", "tools", "timing"):
                self.assertIn(key, payload)
            self.assertEqual(payload["audio_url"], "/static/generated_voice/fake.wav")
            self.assertEqual(payload["tts_chunks"], ["こんにちは。"])
            self.assertNotIn("sampling_rate", payload)
            self.assertNotIn("reference", payload)
            self.assertEqual(len(tts.requests), 1)

    def test_non_stream_deepseek_client_uses_chat_completions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            llm = FakeDeepSeekClient()
            services = make_services(tmpdir, llm=llm)
            state = AgentState(conversation_id="c1", user_input="你好")
            state = validate_input_node(state, services)
            state = build_prompt_node(state, services)
            state = call_llm_node(state, services)

            self.assertEqual(state.parsed_reply, None)
            self.assertIn("こんにちは", state.raw_model_output)
            self.assertEqual(llm.chat.completions.calls[0]["messages"][0]["role"], "user")
            self.assertEqual(state.timing["agent_rounds"], 1)


if __name__ == "__main__":
    unittest.main()
