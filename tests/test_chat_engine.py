"""Phase 6B/6D: ChatEngine drives a turn via the existing pipeline and owns the
character / memory management dissolved from SimpleAgent.

Self-contained fakes; no real LLM/TTS.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.character_loader import replace_mugi_references
from agent.state import AgentServices
from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions
from agent_tools.tts.schemas import TTSRequest, TTSResult
from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from spica.config.schema import AppConfig, CharacterConfig
from spica.core.chat_engine import ChatEngine
from spica.core.events import DoneEvent, RuntimeEvent


class _FakeResp:
    def __init__(self, text):
        self.id = "r"
        self.output_text = text
        self.output = []
        self.usage = SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2)


class _FakeResponses:
    def __init__(self, text):
        self.text = text

    def create(self, **kwargs):
        if kwargs.get("stream"):
            chunks = [self.text[i:i + 9] for i in range(0, len(self.text), 9)]
            events = [SimpleNamespace(type="response.output_text.delta", delta=c) for c in chunks]
            events.append(SimpleNamespace(type="response.completed", response=_FakeResp(self.text)))
            return iter(events)
        return _FakeResp(self.text)


class _FakeLLM:
    def __init__(self, text):
        self.responses = _FakeResponses(text)


class _FakeVisual:
    def build_visual_payload(self, answer, emotion, requested_costume=None, requested_mode=None):
        return {"costume": "school", "classifier_version": "fake", "cues": [{"index": 0, "text": answer}]}

    def prepare_stream_context(self, requested_costume=None, requested_mode=None):
        return {"costume": "school", "costume_mode": "fixed", "classifier_version": "fake"}

    def build_unit_visual_payload(self, **kwargs):
        return {
            "costume": "school", "classifier_version": "fake",
            "selection_source": "local_vote_classifier", "selection_error": None,
            "classifier": {"duration_ms": 3.0}, "dialog": {}, "character": {},
            "cue": {"index": kwargs["unit_index"], "text": kwargs["current_unit_text"],
                    "expression_id": "002", "hand_pose": "normal", "image_url": "/f.png", "reason": "f"},
        }


class _FakeTTS:
    name = "fake_tts"

    def synthesize(self, request):
        assert isinstance(request, TTSRequest)
        return TTSResult(ok=True, provider=self.name, audio_url="/v.wav", audio_path="/tmp/v.wav",
                         chunks=[{"index": 0, "text": request.text, "audio_url": "/v.wav", "audio_path": "/tmp/v.wav"}],
                         timing={"tts_total_ms": 1.0}, duration_ms=1.0)


def _make_services(tmp, answer):
    raw = json.dumps({"answer": answer, "emotion": "happy", "emotion_reason": "r"}, ensure_ascii=False)
    return AgentServices(
        llm_client=_FakeLLM(raw),
        tts_adapter=_FakeTTS(),
        visual_tool=_FakeVisual(),
        memory_store=SQLiteMemoryStore(Path(tmp) / "m.sqlite3"),
        recent_memory=RecentMemory(max_turns=3),
        config={"model": "fake-model", "character_profile": "p", "interlocutor_name": "麦",
                "recent_context_limit": 3, "long_term_memory_limit": 5, "max_tool_rounds": 2},
        logger=lambda *a, **k: None,
        tool_functions=default_tool_functions(),
        tool_schemas=TOOL_SCHEMAS,
    )


def _config():
    # profile_override keeps set_interlocutor_name hermetic (no role-card files).
    return AppConfig(character=CharacterConfig(profile_override="麦のプロフィール"))


ANSWER = "もちろん。フーリエ変換は信号を分解します。必要なら具体例も出しますよ。"


class ChatEngineDrivingTest(unittest.TestCase):
    def _engine(self, tmp):
        return ChatEngine(_make_services(tmp, ANSWER), _config())

    def test_run_voice_drives_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._engine(tmp).run_voice("説明して", conversation_id="c1")
        self.assertEqual(payload["answer"], ANSWER)
        self.assertEqual(payload["conversation_id"], "c1")
        self.assertEqual(payload["audio_url"], "/v.wav")
        self.assertEqual(payload["emotion"]["name"], "happy")

    def test_run_returns_answer_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._engine(tmp).run("説明して"), ANSWER)

    def test_stream_voice_yields_legacy_dicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = list(self._engine(tmp).stream_voice("説明して", conversation_id="c1"))
        self.assertEqual(events[0]["event"], "status")
        done = [e for e in events if e["event"] == "done"][-1]["data"]
        self.assertEqual(done["answer"], ANSWER)
        self.assertEqual(done["units_count"], 2)
        self.assertEqual([e["data"]["index"] for e in events if e["event"] == "unit_ready"], [0, 1])

    def test_stream_voice_runtime_yields_runtime_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = list(self._engine(tmp).stream_voice_runtime("説明して"))
        self.assertTrue(all(isinstance(e, RuntimeEvent) for e in events))
        self.assertIsInstance(events[-1], DoneEvent)
        self.assertEqual(events[-1].answer, ANSWER)


class ChatEngineManagementTest(unittest.TestCase):
    def _engine(self, tmp):
        return ChatEngine(_make_services(tmp, ANSWER), _config())

    def test_model_and_interlocutor_attrs(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(tmp)
        self.assertEqual(engine.model, "fake-model")  # used by StartupWarmupWorker
        self.assertEqual(engine.interlocutor_name, "麦")

    def test_set_interlocutor_name_reloads_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(tmp)
            self.assertEqual(engine.set_interlocutor_name("レン"), "レン")
            self.assertEqual(engine.services.config["interlocutor_name"], "レン")
            self.assertEqual(
                engine.services.config["character_profile"],
                replace_mugi_references("麦のプロフィール", "レン"),
            )

    def test_memory_management(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(tmp)
            mid = engine.remember("好きな色は青", conversation_id="c1")
            self.assertIsInstance(mid, int)
            listed = engine.list_memory("c1")
            self.assertTrue(any("青" in str(m.get("content", "")) for m in listed))
            cleared = engine.clear_memory("c1", clear_long_term=True)
            self.assertTrue(cleared["ok"])
            self.assertTrue(cleared["cleared"]["long_term_memory"])


if __name__ == "__main__":
    unittest.main()
