"""Phase 0 characterization (golden) snapshot for the synchronous voice pipeline.

Locks the CURRENT shape of ``run_voice_pipeline``'s ``response_payload`` for a
fixed input, so later phases cannot silently change the user-visible result --
especially Phase 6B/6D, which move sync driving into ``ChatEngine`` and unify
the sync and streaming paths.

Fakes are self-contained (no shared conftest) per the Phase 0 decision.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from agent.runtime import run_voice_pipeline
from agent.state import AgentServices
from spica.runtime.context import TurnContext, TurnRequest
from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions
from agent_tools.tts.schemas import TTSRequest, TTSResult


class _FakeResponse:
    def __init__(self, text):
        self.id = "golden-sync-response"
        self.output_text = text
        self.output = []
        self.usage = SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15)


class _FakeResponses:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self.text)


class _FakeLLMClient:
    def __init__(self, text):
        self.responses = _FakeResponses(text)


class _FakeTTS:
    name = "golden_tts"

    def __init__(self):
        self.requests = []

    def synthesize(self, request):
        assert isinstance(request, TTSRequest)
        self.requests.append(request)
        return TTSResult(
            ok=True,
            provider=self.name,
            audio_url="/static/generated_voice/golden.wav",
            audio_path="/tmp/golden.wav",
            chunks=[{
                "index": 0,
                "text": request.text,
                "audio_url": "/static/generated_voice/golden.wav",
                "audio_path": "/tmp/golden.wav",
            }],
            timing={"tts_total_ms": 1.0},
            metadata={
                "sampling_rate": 32000,
                "tts_param": {"speed": 1},
                "reference": {"prompt_text": "ref"},
            },
        )


class _FakeVisual:
    def build_visual_payload(self, answer, emotion, requested_costume=None, requested_mode=None):
        return {
            "costume": requested_costume or "校服spica",
            "classifier_version": "golden-local",
            "cues": [{"index": 0, "text": answer}],
        }


def _make_services(tmpdir, llm=None, tts=None, visual=None):
    return AgentServices(
        llm_client=llm or _FakeLLMClient(
            '{"answer":"こんにちは。","emotion":"happy","emotion_reason":"普通の挨拶。"}'
        ),
        tts_adapter=tts,
        visual_tool=visual,
        memory_store=SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3"),
        recent_memory=RecentMemory(max_turns=3),
        config={
            "model": "golden-model",
            "character_profile": "profile",
            "recent_context_limit": 3,
            "long_term_memory_limit": 5,
            "max_tool_rounds": 2,
        },
        logger=lambda *a, **k: None,
        tool_functions=default_tool_functions(),
        tool_schemas=TOOL_SCHEMAS,
    )


class SyncGoldenTest(unittest.TestCase):
    def test_response_payload_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tts = _FakeTTS()
            services = _make_services(tmpdir, tts=tts, visual=_FakeVisual())
            state = run_voice_pipeline(
                TurnContext(TurnRequest(conversation_id="c1", user_input="你好")), services
            )
            payload = state.response_payload

        # Stable user-visible contract of the sync chain. Note: the sync path
        # exposes emotion as a structured dict, while the streaming `done` event
        # exposes it as a plain string -- a divergence Phase 6D must reconcile.
        self.assertEqual(payload["answer"], "こんにちは。")
        self.assertEqual(payload["conversation_id"], "c1")
        self.assertEqual(
            payload["emotion"], {"name": "happy", "label": "喜/乐", "reason": "普通の挨拶。"}
        )
        self.assertEqual(payload["audio_url"], "/static/generated_voice/golden.wav")
        self.assertEqual(payload["tts_chunks"], ["こんにちは。"])
        # Backend-only metadata stays out of the UI payload.
        self.assertNotIn("sampling_rate", payload)
        self.assertNotIn("reference", payload)
        # Structural keys always present.
        for key in ("answer", "conversation_id", "emotion", "audio_url", "visual", "tools", "timing"):
            self.assertIn(key, payload)
        self.assertEqual(len(tts.requests), 1)

    def test_empty_input_error_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            services = _make_services(tmpdir)
            state = run_voice_pipeline(
                TurnContext(TurnRequest(conversation_id="c1", user_input="")), services
            )
            payload = state.response_payload
            self.assertEqual(payload["error"]["code"], "EMPTY_MESSAGE")
            self.assertIsNone(payload["audio_url"])


if __name__ == "__main__":
    unittest.main()
