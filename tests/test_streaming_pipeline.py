import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from memory.store import SQLiteMemoryStore
from memory.recent import RecentMemory
from agent.state import AgentServices, AgentState
from agent.streaming_pipeline import PlayUnitSplitter, build_tts_text, stream_voice_events
from agent_tools.tts import GPTSoVITSTool
from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions
from agent_tools.tts.schemas import TTSRequest, TTSResult


class FakeResponse:
    def __init__(self, text):
        self.id = "fake-stream-response"
        self.output_text = text
        self.output = []
        self.usage = SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15)


class FakeResponses:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            chunks = [self.text[index:index + 9] for index in range(0, len(self.text), 9)]
            events = [
                SimpleNamespace(type="response.output_text.delta", delta=chunk)
                for chunk in chunks
            ]
            events.append(SimpleNamespace(type="response.completed", response=FakeResponse(self.text)))
            return iter(events)
        return FakeResponse(self.text)


class FakeLLMClient:
    def __init__(self, text):
        self.responses = FakeResponses(text)


class FakeChatCompletions:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            chunks = [self.text[index:index + 7] for index in range(0, len(self.text), 7)]
            return iter(
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=chunk))]
                )
                for chunk in chunks
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.text))],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
        )


class FakeChat:
    def __init__(self, text):
        self.completions = FakeChatCompletions(text)


class FakeDeepSeekClient:
    def __init__(self, text):
        self.base_url = "https://api.deepseek.com/v1"
        self.chat = FakeChat(text)


class FakeVisual:
    def __init__(self):
        self.calls = []

    def prepare_stream_context(self, requested_costume=None, requested_mode=None):
        return {
            "costume": requested_costume or "school",
            "costume_mode": requested_mode or "fixed",
            "dialog": {},
            "character": {},
            "classifier_version": "fake-local",
        }

    def build_unit_visual_payload(self, **kwargs):
        self.calls.append(kwargs)
        unit_index = kwargs["unit_index"]
        return {
            "costume": "school",
            "costume_mode": "fixed",
            "classifier_version": "fake-local",
            "selection_source": "local_vote_classifier",
            "selection_error": None,
            "classifier": {"duration_ms": 3.0, "confidence": 0.9, "signals": ["explain"]},
            "dialog": {},
            "character": {},
            "cue": {
                "index": unit_index,
                "text": kwargs["current_unit_text"],
                "expression_id": "002",
                "hand_pose": "normal",
                "image_url": "/visual/file/fake.png",
                "reason": "fake",
            },
        }


class FakeTTS:
    name = "fake_tts"

    def __init__(self):
        self.calls = []

    def synthesize(self, request):
        assert isinstance(request, TTSRequest)
        self.calls.append({"text": request.text, "emotion": request.emotion})
        return TTSResult(
            ok=True,
            provider=self.name,
            audio_url=f"/static/generated_voice/unit_{len(self.calls) - 1}.wav",
            audio_path="/tmp/fake.wav",
            timing={"tts_total_ms": 2.0},
            duration_ms=2.0,
        )


def make_services(tmpdir, answer_text):
    raw = json.dumps(
        {"answer": answer_text, "emotion": "happy", "emotion_reason": "説明口調。"},
        ensure_ascii=False,
    )
    return AgentServices(
        llm_client=FakeLLMClient(raw),
        tts_adapter=FakeTTS(),
        visual_tool=FakeVisual(),
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


class StreamingPipelineTests(unittest.TestCase):
    def test_play_unit_splitter_merges_short_opening_sentence(self):
        text = (
            "もちろん。フーリエ変換は信号を『周波数ごとの成分』に分解します。"
            "連続時間では積分で、離散時間では和で表し、逆変換で元の波形へ戻せます。"
            "複素指数 e^{-iωt} を基底として、低周波はゆっくり、高周波は細かい変化を表します。"
            "必要なら式や具体例も出しますよ。"
        )
        splitter = PlayUnitSplitter(min_chars=18, max_chars=96)
        units = splitter.feed(text) + splitter.flush()

        self.assertEqual(units[0], "もちろん。フーリエ変換は信号を『周波数ごとの成分』に分解します。")
        self.assertEqual(units[1], "連続時間では積分で、離散時間では和で表し、逆変換で元の波形へ戻せます。")
        self.assertEqual(units[2], "複素指数 e^{-iωt} を基底として、低周波はゆっくり、高周波は細かい変化を表します。")
        self.assertEqual(units[3], "必要なら式や具体例も出しますよ。")

    def test_tts_text_reads_math_and_fixes_bad_punctuation(self):
        text = "導関数は f'x →2x です。a fx と e^{-iωt} も説明します、"
        tts_text = build_tts_text(text)

        self.assertIn("エフダッシュエックス", tts_text)
        self.assertIn("は二エックス", tts_text)
        self.assertIn("エーかけるエフエックス", tts_text)
        self.assertIn("イーのマイナスアイオメガティー乗", tts_text)
        self.assertNotIn("f'x", tts_text)
        self.assertNotIn("→2x", tts_text)
        self.assertNotIn("e^{-iωt}", tts_text)
        self.assertNotIn("、。", tts_text)
        self.assertFalse(tts_text.endswith(("、", "，", ",")))

    def test_tts_internal_chunking_keeps_short_opener_with_next_sentence(self):
        tts_tool = GPTSoVITSTool()
        text = tts_tool._normalize_tts_text(
            "もちろん。フーリエ変換は信号を周波数成分に分解します、"
            "低周波と高周波の違いを説明します。"
        )
        chunks = tts_tool._split_tts_text(
            text,
            {"sentence_chunking": True, "max_chunk_chars": 20, "max_chunk_sentences": 1},
        )

        self.assertNotEqual(chunks[0], "もちろん。")
        self.assertTrue(chunks[0].startswith("もちろん。フーリエ変換"))
        self.assertTrue(all("、。" not in chunk for chunk in chunks))
        self.assertTrue(all(not chunk.endswith(("、", "，", ",")) for chunk in chunks))

    def test_stream_events_emit_ordered_unit_ready_without_token_delta(self):
        answer = "もちろん。フーリエ変換は信号を分解します。必要なら具体例も出しますよ。"
        with tempfile.TemporaryDirectory() as tmpdir:
            services = make_services(tmpdir, answer)
            events = list(stream_voice_events(AgentState(conversation_id="c1", user_input="説明して"), services))

        event_names = [event["event"] for event in events]
        unit_events = [event for event in events if event["event"] == "unit_ready"]
        visual_events = [event for event in events if event["event"] == "unit_visual_ready"]
        done = [event for event in events if event["event"] == "done"][-1]["data"]

        self.assertEqual(event_names[0], "status")
        self.assertNotIn("token_delta", event_names)
        self.assertEqual([event["data"]["index"] for event in unit_events], [0, 1])
        self.assertEqual(sorted(event["data"]["index"] for event in visual_events), [0, 1])
        self.assertIn("unit_visual_ready", event_names)
        self.assertIn("unit_ready", event_names)
        self.assertEqual(visual_events[0]["data"]["visual"]["selection_source"], "local_vote_classifier")
        self.assertIn("cue", visual_events[0]["data"])
        self.assertEqual(visual_events[0]["data"]["timing"]["visual_ms"], 3.0)
        self.assertIn("visual_ready_ms", visual_events[0]["data"]["timing"])
        self.assertEqual(unit_events[0]["data"]["visual"]["selection_source"], "local_vote_classifier")
        self.assertEqual(unit_events[0]["data"]["timing"]["visual_ms"], 3.0)
        self.assertEqual(unit_events[0]["data"]["timing"]["tts_ms"], 2.0)
        self.assertEqual(done["units_count"], 2)
        self.assertIn("first_unit_ready_ms", done["timing"])
        self.assertIn("first_llm_delta_ms", done["timing"])
        self.assertIn("first_sentence_ms", done["timing"])
        self.assertIn("first_unit_created_ms", done["timing"])
        self.assertIn("first_tts_start_ms", done["timing"])
        self.assertIn("first_tts_done_ms", done["timing"])
        self.assertEqual(done["timing"]["llm_stream_max_retries"], 0)

    def test_deepseek_client_uses_chat_completions_stream(self):
        answer = "もちろん。Chat Completions の経路で応答します。"
        raw = json.dumps(
            {"answer": answer, "emotion": "happy", "emotion_reason": "説明口調。"},
            ensure_ascii=False,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            services = make_services(tmpdir, answer)
            client = FakeDeepSeekClient(raw)
            services.llm_client = client
            events = list(stream_voice_events(AgentState(conversation_id="c1", user_input="説明して"), services))

        done = [event for event in events if event["event"] == "done"][-1]["data"]
        self.assertEqual(done["answer"], answer)
        self.assertEqual(client.chat.completions.calls[0]["messages"][0]["role"], "user")
        self.assertTrue(client.chat.completions.calls[0]["stream"])
        self.assertEqual(done["timing"]["llm_stream_fallback_reason"], "chat_completions_compatible_client")


if __name__ == "__main__":
    unittest.main()
