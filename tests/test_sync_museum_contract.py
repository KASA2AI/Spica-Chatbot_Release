"""Frozen-museum contract characterization (post-Phase-7 review, BUG-1 ledger).

The Phase 7-c2 ``llm_ready`` terminal semantics (adapter OR client) reach the
frozen museum chain through the deps bridge: ``call_llm_node``'s readiness gate
now lets an ADAPTER-ONLY bundle through. This file pins the resulting museum
contract WITHOUT touching the frozen files:

1. adapter-only WITH the v1 face (prefers_chat_completions/create_responses)
   -> the museum runs it fine. This is the intended terminal-semantics
   direction, now pinned instead of implied.
2. adapter-only with a PURE v2 face (complete/stream/probe only -- the "just
   write one adapter" promise shape) -> the museum fails with
   ``NODE_FAILED`` at the first v1 method touch. THIS IS A CONTRACT RECORD,
   NOT A FEATURE REQUEST: the museum (``stages.call_llm_node`` /
   ``sync_chain``) is permanent v1 and only promises adapters carrying the v1
   face; pure v2 adapters belong to the production chain
   (``ChatEngine.stream_voice`` / ``run_voice``), which handles them fully.
   Do NOT "fix" this by editing the frozen files.

Both-none stays LLM_CLIENT_NOT_CONFIGURED (tests/test_llm_client_not_configured
pins it; reinterpreted since 7-c2 as "no LLM capability at all").
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
from spica.runtime.context import TurnContext, TurnRequest
from spica.runtime.services import AgentServices
from spica.runtime.sync_chain import run_voice_pipeline

RAW_ANSWER = json.dumps(
    {"answer": "博物馆答。", "emotion": "happy", "emotion_reason": "x"}, ensure_ascii=False
)


class _FakeTTS:
    name = "fake_tts"

    def synthesize(self, request):
        assert isinstance(request, TTSRequest)
        return TTSResult(ok=True, provider=self.name, audio_url="/x.wav", audio_path="/tmp/x.wav",
                         chunks=[{"index": 0, "text": request.text, "audio_url": "/x.wav", "audio_path": "/tmp/x.wav"}],
                         timing={"tts_total_ms": 1.0}, duration_ms=1.0)


class _FakeVisual:
    def build_visual_payload(self, answer, emotion, requested_costume=None, requested_mode=None):
        return {"costume": "school", "classifier_version": "x", "cues": [{"index": 0, "text": answer}]}


class _V1FacedAdapter:
    """adapter-only bundle carrying the museum's promised v1 face."""

    def prefers_chat_completions(self):
        return False

    def create_responses(self, **kwargs):
        return SimpleNamespace(id="r", output=[], output_text=RAW_ANSWER, usage=None)


class _PureV2Adapter:
    """The 'just write one adapter' promise shape: v2 face only, no v1 methods."""

    def complete(self, prompt, *, model):
        return RAW_ANSWER

    def stream(self, prompt, *, model, state):
        yield RAW_ANSWER

    def probe(self, prompt, tools, *, model, state):
        raise AssertionError("museum must not reach v2 probe")

    def probe_stream(self, prompt, tools, *, model, state):
        return None


def _run_museum(adapter, tmp):
    services = AgentServices(
        llm_client=None,  # adapter-only: the 7-c2 terminal-readiness shape
        tts_adapter=_FakeTTS(), visual_tool=_FakeVisual(),
        memory_store=SQLiteMemoryStore(Path(tmp) / "m.sqlite3"),
        recent_memory=RecentMemory(max_turns=3),
        config={"model": "m", "character_profile": "p", "recent_context_limit": 3,
                "long_term_memory_limit": 5, "max_tool_rounds": 2,
                "character_id": "spica", "interlocutor_name": "麦"},
        logger=lambda *a, **k: None,
        tool_functions=default_tool_functions(), tool_schemas=TOOL_SCHEMAS,
    )
    services.llm_adapter = adapter
    return run_voice_pipeline(
        TurnContext(TurnRequest(user_input="你好", conversation_id="c1")), services
    )


class SyncMuseumContractTest(unittest.TestCase):
    def test_adapter_only_with_v1_face_runs_the_museum(self):
        # 7-c2 terminal readiness reaches the museum gate: adapter-only (no raw
        # client) is READY and the chain completes on the adapter's v1 face.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _run_museum(_V1FacedAdapter(), tmp)
        self.assertIsNone(ctx.error)
        self.assertEqual(ctx.answer.answer, "博物馆答。")

    def test_pure_v2_adapter_fails_loudly_as_node_failed(self):
        # CONTRACT RECORD (not a feature request): the frozen museum only
        # promises v1-faced adapters; a pure v2 adapter passes the readiness
        # gate and then fails at the first v1 method touch. Pinning the shape
        # keeps this drift VISIBLE -- if it ever changes, someone edited the
        # frozen zone or the bridge semantics, and this test forces a look.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _run_museum(_PureV2Adapter(), tmp)
        self.assertIsNotNone(ctx.error)
        self.assertEqual(ctx.error.code, "NODE_FAILED")
        self.assertIn("prefers_chat_completions", ctx.error.message)


if __name__ == "__main__":
    unittest.main()
