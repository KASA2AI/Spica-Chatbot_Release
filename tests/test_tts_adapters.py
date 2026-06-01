import unittest

from agent.runtime import run_voice_pipeline
from agent.state import AgentServices, AgentState
from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from agent_tools.tts.adapters.dummy import DummyTTSAdapter
from agent_tools.tts.adapters.gptsovits_current import CurrentGPTSoVITSAdapter
from agent_tools.tts.schemas import TTSRequest, TTSResult


class MockGPTSoVITSService:
    def __init__(self, raw=None, exc=None):
        self.raw = raw or {}
        self.exc = exc
        self.calls = []

    def synthesize(self, text, emotion, tts_param_overrides=None):
        self.calls.append(
            {
                "text": text,
                "emotion": emotion,
                "tts_param_overrides": tts_param_overrides,
            }
        )
        if self.exc:
            raise self.exc
        return self.raw


class FakeResponse:
    id = "fake-response"
    output = []
    usage = None

    def __init__(self):
        self.output_text = '{"answer":"こんにちは。","emotion":"happy","emotion_reason":"挨拶。"}'


class FakeResponses:
    def create(self, **kwargs):
        return FakeResponse()


class FakeLLMClient:
    def __init__(self):
        self.responses = FakeResponses()


class MetadataOnlyPrivateFieldsTTS:
    name = "metadata_only"

    def synthesize(self, request):
        return TTSResult(
            ok=True,
            provider=self.name,
            audio_path="/tmp/fake.wav",
            audio_url="/static/generated_voice/fake.wav",
            chunks=[{"index": 0, "text": request.text, "audio_path": "/tmp/fake.wav"}],
            timing={"tts_total_ms": 1.0},
            metadata={
                "sampling_rate": 32000,
                "tts_param": {"top_k": 15},
                "reference": {"prompt_text": "private"},
            },
        )


class TTSAdaptersTest(unittest.TestCase):
    def test_tts_request_and_result_construct(self):
        request = TTSRequest(text="こんにちは", emotion="happy")
        result = TTSResult(ok=True, provider="test", audio_path="/tmp/test.wav")

        self.assertEqual(request.language, "ja")
        self.assertEqual(request.output_format, "wav")
        self.assertTrue(result.ok)
        self.assertEqual(result.audio_path, "/tmp/test.wav")

    def test_dummy_adapter_without_audio_returns_standard_error(self):
        result = DummyTTSAdapter().synthesize(TTSRequest(text="hello"))

        self.assertFalse(result.ok)
        self.assertEqual(result.provider, "dummy")
        self.assertIn("no test audio", result.error)

    def test_dummy_adapter_with_audio_returns_standard_result(self):
        result = DummyTTSAdapter(audio_path="/tmp/test.wav").synthesize(TTSRequest(text="hello"))

        self.assertTrue(result.ok)
        self.assertEqual(result.audio_path, "/tmp/test.wav")
        self.assertEqual(result.chunks[0]["text"], "hello")

    def test_current_gptsovits_adapter_maps_raw_dict(self):
        raw = {
            "ok": True,
            "audio_path": "/tmp/current.wav",
            "audio_url": "/static/generated_voice/current.wav",
            "sampling_rate": 32000,
            "tts_chunks": ["こんにちは。"],
            "tts_chunk_audio": [
                {
                    "index": 0,
                    "text": "こんにちは。",
                    "audio_path": "/tmp/current_chunk.wav",
                    "audio_url": "/static/generated_voice/current_chunk.wav",
                }
            ],
            "tts_params": {"top_k": 15},
            "reference": {"prompt_text": "private"},
            "timing": {"tts_total_ms": 12.5},
        }
        service = MockGPTSoVITSService(raw=raw)
        adapter = CurrentGPTSoVITSAdapter(service)
        result = adapter.synthesize(
            TTSRequest(
                text="こんにちは。",
                emotion="happy",
                speed=1.25,
                extra={"tts_param_overrides": {"top_k": 10}},
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.provider, "gptsovits_current")
        self.assertEqual(result.audio_path, "/tmp/current.wav")
        self.assertEqual(result.audio_url, "/static/generated_voice/current.wav")
        self.assertEqual(result.sample_rate, 32000)
        self.assertEqual(result.duration_ms, 12.5)
        self.assertEqual(result.chunks[0]["audio_path"], "/tmp/current_chunk.wav")
        self.assertIs(result.metadata, raw)
        self.assertEqual(service.calls[0]["tts_param_overrides"], {"top_k": 10, "speed": 1.25})

    def test_current_gptsovits_adapter_catches_service_exception(self):
        adapter = CurrentGPTSoVITSAdapter(MockGPTSoVITSService(exc=RuntimeError("boom")))
        result = adapter.synthesize(TTSRequest(text="こんにちは。", emotion="happy"))

        self.assertFalse(result.ok)
        self.assertEqual(result.provider, "gptsovits_current")
        self.assertIn("boom", result.error)

    def test_agent_payload_does_not_depend_on_gptsovits_private_fields(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            services = AgentServices(
                llm_client=FakeLLMClient(),
                tts_adapter=MetadataOnlyPrivateFieldsTTS(),
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
                tool_functions={},
                tool_schemas=[],
            )
            state = run_voice_pipeline(AgentState(conversation_id="c1", user_input="你好"), services)

        payload = state.response_payload
        self.assertEqual(payload["audio_path"], "/tmp/fake.wav")
        self.assertEqual(payload["tts_chunks"], ["こんにちは。"])
        self.assertNotIn("sampling_rate", payload)
        self.assertNotIn("tts_param", payload)
        self.assertNotIn("reference", payload)


if __name__ == "__main__":
    unittest.main()
