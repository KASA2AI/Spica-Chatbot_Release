import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.runtime import run_voice_pipeline
from agent.state import AgentServices, AgentState
from agent_tools.function_tools import TOOL_SCHEMAS
from agent_tools.tts.schemas import TTSRequest, TTSResult
from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore


class FakeResponse:
    def __init__(self, text):
        self.id = "fake-screen-response"
        self.output_text = text
        self.output = []
        self.usage = SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15)


class FakeResponses:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self.text)


class FakeLLMClient:
    def __init__(self, text='{"answer":"スクリーンショットにはブラウザが見えます。","emotion":"happy","emotion_reason":"画面説明。"}'):
        self.responses = FakeResponses(text)


class FakeTTS:
    name = "fake_tts"

    def synthesize(self, request):
        assert isinstance(request, TTSRequest)
        return TTSResult(ok=True, provider=self.name, audio_url=None, audio_path=None)


def make_attachment():
    return {
        "kind": "screen_capture",
        "target": "selected_region",
        "source": "manual_region_selection",
        "captured_at": "2026-06-06T00:00:00+00:00",
        "image_bytes": b"jpeg-bytes",
        "mime_type": "image/jpeg",
        "original_resolution": {"width": 100, "height": 80},
        "sent_resolution": {"width": 100, "height": 80},
        "downscaled": False,
        "format": "jpeg",
        "quality": 75,
        "region": {
            "screen_name": "primary",
            "screen_index": 0,
            "logical": {"x": 1, "y": 2, "width": 100, "height": 80},
            "physical": {"x": 1, "y": 2, "width": 100, "height": 80},
            "device_pixel_ratio": 1.0,
        },
    }


def fake_observation(question):
    return {
        "schema_version": "screen_observation.v1",
        "type": "screen_observation",
        "request": {
            "user_question": question,
            "question_type": "general_observation",
            "target": "selected_region",
        },
        "capture": {
            "captured_scope": "selected_region",
            "source": "manual_region_selection",
        },
        "answer": {"direct_answer": "ブラウザが見えます。", "confidence": 0.9},
        "followup": {
            "context_for_next_turn": "selected region shows a browser",
            "needs_followup_capture": False,
            "suggested_capture": None,
        },
        "visible_text": {"raw": "FULL OCR SHOULD NOT ENTER PROMPT"},
    }


def make_services(tmpdir, llm):
    return AgentServices(
        llm_client=llm,
        tts_adapter=FakeTTS(),
        visual_tool=None,
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
        tool_functions={"inspect_screen": lambda **kwargs: (_ for _ in ()).throw(AssertionError("inspect_screen should not run"))},
        tool_schemas=TOOL_SCHEMAS,
    )


def test_empty_input_with_pending_screenshot_uses_default_question_and_injects_observation():
    with tempfile.TemporaryDirectory() as tmpdir:
        llm = FakeLLMClient()
        services = make_services(tmpdir, llm)
        calls = []

        def fake_analyzer(*, attachment, user_question):
            calls.append({"attachment": attachment, "question": user_question})
            return fake_observation(user_question)

        with patch("agent.nodes.analyze_screen_attachment", fake_analyzer):
            state = run_voice_pipeline(
                AgentState(conversation_id="c1", user_input="", screen_attachment=make_attachment()),
                services,
            )

        assert calls[0]["question"] == "请查看这张截图并概括内容。"
        assert calls[0]["attachment"]["target"] == "selected_region"
        assert "tools" not in llm.responses.calls[0]
        assert "[SCREEN_OBSERVATION]" in llm.responses.calls[0]["input"]
        assert "jpeg-bytes" not in llm.responses.calls[0]["input"]
        assert "FULL OCR SHOULD NOT ENTER PROMPT" not in llm.responses.calls[0]["input"]
        assert state.response_payload["answer"] == "スクリーンショットにはブラウザが見えます。"
        assert state.tools[0]["name"] == "screen_analyzer"
        assert state.tools[0]["ok"] is True


def test_pending_screenshot_disables_repeat_automatic_inspect_screen_even_for_screen_text():
    with tempfile.TemporaryDirectory() as tmpdir:
        llm = FakeLLMClient()
        services = make_services(tmpdir, llm)

        with patch("agent.nodes.analyze_screen_attachment", lambda *, attachment, user_question: fake_observation(user_question)):
            state = run_voice_pipeline(
                AgentState(conversation_id="c1", user_input="看一下我屏幕", screen_attachment=make_attachment()),
                services,
            )

        assert "tools" not in llm.responses.calls[0]
        assert state.metadata["use_tools"] is False
        assert state.metadata["selected_tool_schema_count"] == 0
        assert json.loads(json.dumps(state.screen_observation, ensure_ascii=False))["request"]["target"] == "selected_region"


def test_screen_followup_context_enters_next_turn_prompt_without_raw_image_or_ocr():
    with tempfile.TemporaryDirectory() as tmpdir:
        llm = FakeLLMClient()
        services = make_services(tmpdir, llm)

        with patch("agent.nodes.analyze_screen_attachment", lambda *, attachment, user_question: fake_observation(user_question)):
            run_voice_pipeline(
                AgentState(conversation_id="c1", user_input="这是什么", screen_attachment=make_attachment()),
                services,
            )

        run_voice_pipeline(AgentState(conversation_id="c1", user_input="那怎么解决？"), services)
        followup_prompt = llm.responses.calls[-1]["input"]

        assert "[前回の画面観察]" in followup_prompt
        assert "selected region shows a browser" in followup_prompt
        assert "jpeg-bytes" not in followup_prompt
        assert "FULL OCR SHOULD NOT ENTER PROMPT" not in followup_prompt
