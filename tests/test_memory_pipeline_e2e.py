"""End-to-end regression for the long-term-memory read/write key (Phase 5/7).

Auto-extracted long-term memory is written under a character-namespaced
conversation_id (``f"{character_id}::{conversation_id}"``) by ``commit_turn``,
but the retrieve path used to read it back with a *bare* conversation_id -- so
"remember X this turn, recall it next turn" silently returned nothing. These
tests drive the full voice pipeline with self-contained fakes (no real LLM/TTS)
and assert:

1. a memory written one turn is retrievable the next turn (and reaches the prompt);
2. two different characters never read each other's long-term memory;
3. ChatEngine's manual remember/list/clear use the same character namespace.

Short-term recent memory stays on the bare conversation_id throughout.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.runtime import run_voice_pipeline
from agent.state import AgentServices, AgentState
from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions
from agent_tools.tts.schemas import TTSRequest, TTSResult
from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from spica.adapters.memory.sqlite import scoped_conversation_id
from spica.config.schema import AppConfig, CharacterConfig
from spica.core.chat_engine import ChatEngine

_REPLY = json.dumps({"answer": "うん。", "emotion": "happy", "emotion_reason": "r"}, ensure_ascii=False)


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
        return _FakeResp(self.text)


class _FakeLLM:
    def __init__(self, text=_REPLY):
        self.responses = _FakeResponses(text)


class _FakeTTS:
    name = "fake_tts"

    def synthesize(self, request):
        assert isinstance(request, TTSRequest)
        return TTSResult(
            ok=True, provider=self.name, audio_url="/v.wav", audio_path="/tmp/v.wav",
            chunks=[{"index": 0, "text": request.text, "audio_url": "/v.wav", "audio_path": "/tmp/v.wav"}],
            timing={"tts_total_ms": 1.0}, duration_ms=1.0,
        )


def _services(store, character_id, interlocutor="麦"):
    return AgentServices(
        llm_client=_FakeLLM(),
        tts_adapter=_FakeTTS(),
        visual_tool=None,
        memory_store=store,
        recent_memory=RecentMemory(max_turns=3),
        config={
            "model": "fake-model",
            "character_profile": "p",
            "interlocutor_name": interlocutor,
            "character_id": character_id,
            "recent_context_limit": 3,
            "long_term_memory_limit": 5,
            "max_tool_rounds": 2,
        },
        logger=lambda *a, **k: None,
        tool_functions=default_tool_functions(),
        tool_schemas=TOOL_SCHEMAS,
    )


def _turn(services, user_input, conversation_id="c1"):
    return run_voice_pipeline(
        AgentState(conversation_id=conversation_id, user_input=user_input), services
    )


class MemoryReadWriteKeyTest(unittest.TestCase):
    def test_memory_written_this_turn_is_retrievable_next_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = _services(SQLiteMemoryStore(Path(tmp) / "m.sqlite3"), character_id="spica")
            _turn(services, "记住我喜欢简短回答")
            state = _turn(services, "简短回答可以吗")
        self.assertTrue(
            state.long_term_memories,
            "memory written last turn must be retrievable this turn (read key must match write key)",
        )
        self.assertTrue(any("简短" in str(m.get("content", "")) for m in state.long_term_memories))
        self.assertIn("简短", str(state.prompt_input))  # and it actually reaches the prompt

    def test_long_term_memory_isolated_across_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMemoryStore(Path(tmp) / "m.sqlite3")  # shared backend
            alpha = _services(store, character_id="alpha")
            beta = _services(store, character_id="beta")
            _turn(alpha, "记住我喜欢简短回答")
            beta_state = _turn(beta, "简短回答可以吗")
            alpha_state = _turn(alpha, "简短回答可以吗")
        self.assertEqual(beta_state.long_term_memories, [], "characters must not read each other's memory")
        self.assertTrue(alpha_state.long_term_memories, "the owning character must still see its own memory")

    def test_chat_engine_manual_memory_uses_character_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMemoryStore(Path(tmp) / "m.sqlite3")
            services = _services(store, character_id="spica")
            engine = ChatEngine(services, AppConfig(character=CharacterConfig(profile_override="p")))

            engine.remember("我喜欢简短回答", conversation_id="c1")
            # written under the character namespace, never the bare conversation_id
            self.assertEqual(store.list_memories("c1"), [])
            self.assertTrue(store.list_memories(scoped_conversation_id("spica", "c1")))
            # list_memory round-trips through the same namespace ...
            self.assertTrue(engine.list_memory("c1"))
            # ... and the auto pipeline retrieves the manually-remembered item
            state = _turn(services, "简短回答可以吗")
            self.assertTrue(any("简短" in str(m.get("content", "")) for m in state.long_term_memories))
            # clearing long-term also targets the namespace
            engine.clear_memory("c1", clear_long_term=True)
            self.assertEqual(store.list_memories(scoped_conversation_id("spica", "c1")), [])


if __name__ == "__main__":
    unittest.main()
