import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from memory.store import SQLiteMemoryStore
from agent.nodes import build_prompt_node, call_llm_node, parse_reply_node, validate_input_node
from memory.recent import RecentMemory
from agent.runtime import run_voice_pipeline
from agent.state import AgentServices
from spica.runtime.context import TurnContext, TurnRequest
from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions, is_screen_intent_explicit, should_use_tools
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


class FakeToolResponses:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return SimpleNamespace(
                id="fake-tool-call",
                output_text="",
                output=[
                    SimpleNamespace(
                        type="function_call",
                        name="inspect_screen",
                        arguments=json.dumps(
                            {"target": "full_screen", "question": "看一下我屏幕"},
                            ensure_ascii=False,
                        ),
                    )
                ],
                usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
            )
        return FakeResponse('{"answer":"画面にはエラーは見えません。","emotion":"happy","emotion_reason":"画面観察結果の説明。"}')


class FakeToolLLMClient:
    def __init__(self):
        self.responses = FakeToolResponses()


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
            state = run_voice_pipeline(TurnContext(TurnRequest(conversation_id="c1", user_input="")), services)
            self.assertEqual(state.response_payload["error"]["code"], "EMPTY_MESSAGE")
            self.assertEqual(state.response_payload["audio_url"], None)

    def test_normal_chat_does_not_pass_tools(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            llm = FakeLLMClient()
            services = make_services(tmpdir, llm=llm)
            state = TurnContext(TurnRequest(conversation_id="c1", user_input="你好"))
            state = validate_input_node(state, services)
            state = build_prompt_node(state, services)
            state = call_llm_node(state, services)
            self.assertNotIn("tools", llm.responses.calls[0])
            self.assertFalse(state.metadata["use_tools"])

    def test_default_demo_tool_requests_do_not_pass_tools(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            llm = FakeLLMClient()
            services = make_services(tmpdir, llm=llm)
            state = TurnContext(TurnRequest(conversation_id="c1", user_input="现在几点"))
            state = validate_input_node(state, services)
            state = build_prompt_node(state, services)
            state = call_llm_node(state, services)
            self.assertNotIn("tools", llm.responses.calls[0])
            self.assertFalse(state.metadata["use_tools"])
            self.assertFalse(should_use_tools("现在几点"))

    def test_screen_intent_passes_tools(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            llm = FakeLLMClient()
            services = make_services(tmpdir, llm=llm)
            state = TurnContext(TurnRequest(conversation_id="c1", user_input="看一下我屏幕"))
            state = validate_input_node(state, services)
            state = build_prompt_node(state, services)
            state = call_llm_node(state, services)

            self.assertIn("tools", llm.responses.calls[0])
            self.assertEqual(llm.responses.calls[0]["tools"][0]["name"], "inspect_screen")
            self.assertTrue(state.metadata["use_tools"])

    def test_screen_intent_examples(self):
        should_trigger = [
            "Spica，看看我现在屏幕在干嘛",
            "现在我主屏幕上面的报错是什么",
            "我现在正在浏览的网站是什么",
            "桌面的女孩可能出自哪个动漫",
            "浏览器现在打开了几个网站",
            "看一下任务栏有几个窗口",
        ]
        should_not_trigger = [
            "屏幕尺寸买多大合适",
            "桌面整理有什么建议",
            "浏览器推荐哪个好",
            "游戏陪玩功能怎么设计",
        ]
        self.assertEqual([is_screen_intent_explicit(text) for text in should_trigger], [True] * len(should_trigger))
        self.assertEqual([is_screen_intent_explicit(text) for text in should_not_trigger], [False] * len(should_not_trigger))

    def test_english_screen_intent_examples(self):
        should_trigger = [
            "Spica, what is on my screen?",
            "What is the error on my display?",
            "How many windows are on the taskbar?",
            "Please inspect my browser screen.",
            "What is in my current window?",
        ]
        self.assertEqual([is_screen_intent_explicit(text) for text in should_trigger], [True] * len(should_trigger))
        self.assertTrue(should_use_tools("How many windows are on the taskbar?"))

    def test_screen_tool_result_can_drive_final_json_reply(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            llm = FakeToolLLMClient()
            services = make_services(tmpdir, llm=llm)
            calls = []

            def fake_inspect_screen(target, question):
                calls.append({"target": target, "question": question})
                return json.dumps(
                    {
                        "ok": True,
                        "data": {
                            "schema_version": "screen_observation.v1",
                            "type": "screen_observation",
                            "request": {
                                "user_question": question,
                                "question_type": "general_observation",
                                "target": target,
                            },
                            "capture": {
                                "captured_scope": "full_screen",
                                "source": "automatic_screenshot",
                            },
                            "answer": {"direct_answer": "画面にエラーは見えません。", "confidence": 0.9},
                            "followup": {"context_for_next_turn": "No visible error.", "needs_followup_capture": False, "suggested_capture": None},
                        },
                        "error": None,
                    },
                    ensure_ascii=False,
                )

            services.tool_functions = {"inspect_screen": fake_inspect_screen}
            state = TurnContext(TurnRequest(conversation_id="c1", user_input="看一下我屏幕"))
            state = validate_input_node(state, services)
            state = build_prompt_node(state, services)
            state = call_llm_node(state, services)
            state = parse_reply_node(state, services)

            self.assertEqual(calls, [{"target": "full_screen", "question": "看一下我屏幕"}])
            self.assertEqual(len(llm.responses.calls), 2)
            self.assertIn("[TOOL_RESULTS]", llm.responses.calls[1]["input"])
            self.assertEqual(state.answer.answer, "画面にはエラーは見えません。")

    def test_pipeline_returns_compatible_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tts = FakeTTS()
            services = make_services(tmpdir, tts=tts, visual=FakeVisual())
            state = run_voice_pipeline(TurnContext(TurnRequest(conversation_id="c1", user_input="你好")), services)
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
            state = TurnContext(TurnRequest(conversation_id="c1", user_input="你好"))
            state = validate_input_node(state, services)
            state = build_prompt_node(state, services)
            state = call_llm_node(state, services)

            self.assertEqual(state.answer.parsed_reply, None)
            self.assertIn("こんにちは", state.answer.raw_model_output)
            self.assertEqual(llm.chat.completions.calls[0]["messages"][0]["role"], "user")
            self.assertEqual(state.timing["agent_rounds"], 1)


if __name__ == "__main__":
    unittest.main()
