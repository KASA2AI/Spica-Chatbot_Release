"""Phase 3 integration: the gated stage is inserted in BOTH chains.

Proves retrieve_game_context_node runs in the production sync chain
(run_voice_pipeline) AND the streaming orchestrator (stream_voice_events): a
galgame turn's injected [GAME_PROGRESS] reaches the prompt actually handed to the
LLM, while a plain chat turn's prompt carries no [GAME_*] section (none-branch
no-op end to end). Fakes are self-contained (mirrors the golden tests).
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
from spica.galgame.manual import ManualGameMemory
from spica.runtime.context import GameContextRequest, TurnContext, TurnRequest
from spica.runtime.orchestrator import stream_voice_events
from spica.runtime.services import AgentServices
from spica.runtime.sync_chain import run_voice_pipeline


# --- self-contained fakes (mirror the golden tests) ------------------------ #

class _FakeResponse:
    def __init__(self, text):
        self.id = "game-ctx-response"
        self.output_text = text
        self.output = []
        self.usage = SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15)


class _FakeResponses:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)  # records the prompt under "input"
        if kwargs.get("stream"):
            chunks = [self.text[i:i + 9] for i in range(0, len(self.text), 9)]
            events = [SimpleNamespace(type="response.output_text.delta", delta=c) for c in chunks]
            events.append(SimpleNamespace(type="response.completed", response=_FakeResponse(self.text)))
            return iter(events)
        return _FakeResponse(self.text)


class _FakeLLMClient:
    def __init__(self, text):
        self.responses = _FakeResponses(text)


class _FakeTTS:
    name = "game_ctx_tts"

    def synthesize(self, request):
        assert isinstance(request, TTSRequest)
        return TTSResult(
            ok=True, provider=self.name, audio_url="/static/x.wav", audio_path="/tmp/x.wav",
            chunks=[{"index": 0, "text": request.text, "audio_url": "/static/x.wav", "audio_path": "/tmp/x.wav"}],
            timing={"tts_total_ms": 1.0}, duration_ms=1.0,
        )


class _FakeVisual:
    def build_visual_payload(self, answer, emotion, requested_costume=None, requested_mode=None):
        return {"costume": "校服spica", "classifier_version": "x", "cues": [{"index": 0, "text": answer}]}

    def prepare_stream_context(self, requested_costume=None, requested_mode=None):
        return {"costume": "school", "costume_mode": "fixed", "dialog": {}, "character": {}, "classifier_version": "x"}

    def build_unit_visual_payload(self, **kwargs):
        return {
            "costume": "school", "costume_mode": "fixed", "classifier_version": "x",
            "selection_source": "local_vote_classifier", "selection_error": None,
            "classifier": {"duration_ms": 1.0, "confidence": 0.9, "signals": ["x"]},
            "dialog": {}, "character": {},
            "cue": {"index": kwargs["unit_index"], "text": kwargs["current_unit_text"],
                    "expression_id": "002", "hand_pose": "normal", "image_url": "/x.png", "reason": "x"},
        }


_RAW_ANSWER = json.dumps({"answer": "了解。", "emotion": "happy", "emotion_reason": "x"}, ensure_ascii=False)


def _make_services(tmpdir):
    gm = GameMemorySqliteAdapter(Path(tmpdir) / "galgame.sqlite3")
    ManualGameMemory(gm, character_id="spica", user_id="麦").manual_set_progress_state(
        "ABC", chapter={"title": "第一章", "confidence": 0.7}, current_scene_summary="在教室对话"
    )
    services = AgentServices(
        llm_client=_FakeLLMClient(_RAW_ANSWER),
        tts_adapter=_FakeTTS(),
        visual_tool=_FakeVisual(),
        memory_store=SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3"),
        recent_memory=RecentMemory(max_turns=3),
        config={
            "model": "test-model", "character_profile": "profile", "recent_context_limit": 3,
            "long_term_memory_limit": 5, "max_tool_rounds": 2, "character_id": "spica",
            "interlocutor_name": "麦",
        },
        logger=lambda *a, **k: None,
        tool_functions=default_tool_functions(),
        tool_schemas=TOOL_SCHEMAS,
    )
    services.game_memory_adapter = gm
    return services


def _galgame_request():
    return TurnRequest(
        user_input="刚才发生什么了", conversation_id="default", interaction_mode="galgame",
        game_context_request=GameContextRequest(mode="active", game_id="ABC"),
    )


def _chat_request():
    return TurnRequest(user_input="你好", conversation_id="default")


class GameContextInSyncChainTest(unittest.TestCase):
    def test_galgame_turn_injects_into_llm_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = _make_services(tmp)
            run_voice_pipeline(TurnContext(_galgame_request()), services)
            prompt = services.llm_client.responses.calls[0]["input"]
            self.assertIn("[GAME_PROGRESS]", prompt)
            self.assertIn("在教室对话", prompt)

    def test_chat_turn_has_no_game_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = _make_services(tmp)
            run_voice_pipeline(TurnContext(_chat_request()), services)
            prompt = services.llm_client.responses.calls[0]["input"]
            self.assertNotIn("[GAME_PROGRESS]", prompt)
            self.assertNotIn("[GAME_", prompt)


class GameContextInStreamingChainTest(unittest.TestCase):
    def _stream(self, services, request):
        return list(stream_voice_events(TurnContext(request), services))

    def test_galgame_turn_injects_into_streamed_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = _make_services(tmp)
            self._stream(services, _galgame_request())
            prompt = services.llm_client.responses.calls[-1]["input"]
            self.assertIn("[GAME_PROGRESS]", prompt)
            self.assertIn("在教室对话", prompt)

    def test_chat_turn_has_no_game_sections_streamed(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = _make_services(tmp)
            self._stream(services, _chat_request())
            prompt = services.llm_client.responses.calls[-1]["input"]
            self.assertNotIn("[GAME_PROGRESS]", prompt)
            self.assertNotIn("[GAME_", prompt)


if __name__ == "__main__":
    unittest.main()
