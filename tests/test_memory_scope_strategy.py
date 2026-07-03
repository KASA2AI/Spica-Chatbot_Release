"""Phase 2: MemoryScopeStrategy / CharacterScope semantics + symmetry pins.

Four pins (migration plan Phase 2 "characterization tests to add"):

1. strategy method semantics -- recent_key is character-scoped, ltm_scope keeps
   the §27① effective_memory_conversation_id fallback, defaults resolve through
   the scope.py single home, clear_targets returns the symmetric pair;
2. retrieve/commit SYMMETRY on the production surfaces: for the same request,
   the scope the retrieve node hands ``memory.retrieve`` equals the scope the
   commit hands ``commit_turn``, and ``load_recent_context_node`` reads back the
   exact bucket ``save_stream_memory`` wrote;
3. clear SYMMETRY: ``ChatEngine.clear_memory`` empties BOTH the scoped recent
   bucket and the scoped long-term namespace (the pre-Phase-2 asymmetry cleared
   a bare recent key nobody writes to anymore);
4. rename LIVE-READ: ``set_interlocutor_name``'s in-place config mutation is
   visible to the very next scope resolution (pins the live semantics so a later
   phase cannot silently freeze the scope).
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions
from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from spica.adapters.memory.sqlite import scoped_conversation_id
from spica.config.schema import AppConfig, CharacterConfig
from spica.core.chat_engine import ChatEngine
from spica.runtime.context import StreamedAnswer, TurnContext, TurnRequest
from spica.runtime.deps import TurnDeps
from spica.runtime.memory_commit import save_stream_memory
from spica.runtime.scope import (
    DEFAULT_CHARACTER_ID,
    MemoryScopeStrategy,
    character_scope_from_config,
)
from spica.runtime.services import AgentServices
from spica.runtime.stages import load_recent_context_node, retrieve_long_term_memory_node
from spica.runtime.tools import RegistryToolSet


def _config(character_id="spica", interlocutor="麦") -> AppConfig:
    return AppConfig(
        character=CharacterConfig(character_id=character_id, interlocutor_name=interlocutor)
    )


class StrategySemanticsTest(unittest.TestCase):
    def test_recent_key_is_character_scoped(self):
        strategy = MemoryScopeStrategy(_config("kira", "レン"))
        req = TurnRequest(user_input="x", conversation_id="c9")
        self.assertEqual(strategy.recent_key(req), scoped_conversation_id("kira", "c9"))

    def test_ltm_scope_keeps_effective_memory_conversation_id(self):
        strategy = MemoryScopeStrategy(_config())
        galgame = TurnRequest(
            user_input="x",
            conversation_id="galgame::ABC::playthrough::default",
            memory_conversation_id="default",
        )
        scope = strategy.ltm_scope(galgame)
        # §27①: the galgame turn keeps reading/writing the ORIGIN conversation.
        self.assertEqual(
            (scope.character_id, scope.user_id, scope.conversation_id),
            ("spica", "麦", "default"),
        )
        # Unset -> falls back to the raw conversation_id (plain chat, unchanged).
        plain = strategy.ltm_scope(TurnRequest(user_input="x", conversation_id="c1"))
        self.assertEqual(plain.conversation_id, "c1")

    def test_defaults_resolve_via_the_single_home(self):
        scope = character_scope_from_config(AppConfig())  # identity fields unset
        self.assertEqual((scope.character_id, scope.user_id), (DEFAULT_CHARACTER_ID, "麦"))

    def test_clear_targets_pair_is_scoped_and_symmetric(self):
        strategy = MemoryScopeStrategy(_config())
        recent_key, ltm_conversation_id = strategy.clear_targets("c1")
        self.assertEqual(recent_key, scoped_conversation_id("spica", "c1"))
        self.assertEqual(recent_key, ltm_conversation_id)  # today the pair coincides


class _RecordingMemoryPort:
    """Records the scopes the production read/write surfaces hand to the port."""

    def __init__(self):
        self.retrieve_scopes = []
        self.commit_scopes = []

    def retrieve(self, scope, query, limit=5):
        self.retrieve_scopes.append(scope)
        return []

    def commit_turn(self, scope, user_input, answer, meta=None):
        self.commit_scopes.append(scope)
        return {}


def _deps(config: AppConfig, memory, recent=None) -> TurnDeps:
    return TurnDeps(
        config=config,
        llm=None,
        tts=None,
        visual=None,
        memory=memory,
        tools=RegistryToolSet.from_function_table([], {}),
        recent=recent,  # Phase 5: deps.recent
    )  # jobs defaults to InlineJobRunner -> commit runs synchronously


class RetrieveCommitSymmetryTest(unittest.TestCase):
    def test_same_request_reads_and_writes_one_scope_pair(self):
        config = _config()
        memory = _RecordingMemoryPort()
        recent = RecentMemory()
        services = SimpleNamespace(recent_memory=recent)
        req = TurnRequest(
            user_input="记住这个",
            conversation_id="galgame::ABC::playthrough::default",
            memory_conversation_id="default",
            include_user_time_context=False,
        )

        write_ctx = TurnContext(req)
        write_ctx.answer = StreamedAnswer(answer="好。")
        save_stream_memory(write_ctx, services, _deps(config, memory, recent))

        read_ctx = TurnContext(req)
        load_recent_context_node(read_ctx, services, _deps(config, memory, recent))
        retrieve_long_term_memory_node(read_ctx, services, _deps(config, memory, recent))

        # LTM symmetry: the committed triple IS the retrieved triple.
        [committed] = memory.commit_scopes
        [retrieved] = memory.retrieve_scopes
        self.assertEqual(
            (committed.character_id, committed.user_id, committed.conversation_id),
            (retrieved.character_id, retrieved.user_id, retrieved.conversation_id),
        )
        # recent symmetry: the read node found the turn the write stored -> the
        # scoped bucket key is shared by construction, not by coincidence.
        self.assertEqual(len(read_ctx.recent.recent_context), 1)
        self.assertEqual(read_ctx.recent.recent_context[0]["user_text"], "记住这个")

    def test_two_characters_never_share_a_recent_bucket(self):
        recent = RecentMemory()
        services = SimpleNamespace(recent_memory=recent)
        req = TurnRequest(
            user_input="悄悄话", conversation_id="shared", include_user_time_context=False
        )
        ctx = TurnContext(req)
        ctx.answer = StreamedAnswer(answer="嗯。")
        save_stream_memory(ctx, services, _deps(_config("spica"), _RecordingMemoryPort(), recent))

        other = TurnContext(req)
        load_recent_context_node(other, services, _deps(_config("second-chara"), _RecordingMemoryPort(), recent))
        self.assertEqual(other.recent.recent_context, [])


def _engine_services(store: SQLiteMemoryStore) -> AgentServices:
    # Mirrors test_memory_pipeline_e2e._services: the minimal real bundle a
    # ChatEngine needs for memory-surface tests (no LLM/TTS ever invoked here).
    return AgentServices(
        llm_client=None,
        tts_adapter=None,
        visual_tool=None,
        memory_store=store,
        recent_memory=RecentMemory(max_turns=3),
        config={
            "model": "fake-model",
            "character_profile": "p",
            "interlocutor_name": "麦",
            "character_id": "spica",
            "recent_context_limit": 3,
            "long_term_memory_limit": 5,
            "max_tool_rounds": 2,
        },
        logger=lambda *a, **k: None,
        tool_functions=default_tool_functions(),
        tool_schemas=TOOL_SCHEMAS,
    )


def _engine(store: SQLiteMemoryStore) -> ChatEngine:
    return ChatEngine(
        _engine_services(store),
        AppConfig(
            character=CharacterConfig(
                character_id="spica", interlocutor_name="麦", profile_override="p"
            )
        ),
    )


class ClearSymmetryTest(unittest.TestCase):
    def test_clear_memory_empties_scoped_recent_and_scoped_ltm(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMemoryStore(Path(tmp) / "m.sqlite3")
            engine = _engine(store)
            scoped = scoped_conversation_id("spica", "c1")

            # Seed both halves through the production surfaces.
            engine.remember("我喜欢简短回答", conversation_id="c1")
            ctx = TurnContext(
                TurnRequest(
                    user_input="你好", conversation_id="c1", include_user_time_context=False
                )
            )
            ctx.answer = StreamedAnswer(answer="嗯。")
            save_stream_memory(ctx, engine.services, engine.deps)

            self.assertTrue(engine.services.recent_memory.get_recent(scoped))
            self.assertTrue(store.list_memories(scoped))

            engine.clear_memory("c1", clear_long_term=True)

            # Both sides of the SAME scoped conversation are empty -- the recent
            # half used to clear the bare key and silently miss the real bucket.
            self.assertEqual(engine.services.recent_memory.get_recent(scoped), [])
            self.assertEqual(store.list_memories(scoped), [])


class RenameLiveReadTest(unittest.TestCase):
    def test_strategy_follows_in_place_config_rename(self):
        config = _config()
        strategy = MemoryScopeStrategy(config)
        req = TurnRequest(user_input="x", conversation_id="c1")
        self.assertEqual(strategy.ltm_scope(req).user_id, "麦")
        # The exact mutation shape set_interlocutor_name performs (same object).
        config.character.interlocutor_name = "レン"
        self.assertEqual(strategy.ltm_scope(req).user_id, "レン")

    def test_engine_rename_is_visible_to_its_strategy(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(SQLiteMemoryStore(Path(tmp) / "m.sqlite3"))
            req = TurnRequest(user_input="x", conversation_id="c1")
            self.assertEqual(engine._memory_scope.ltm_scope(req).user_id, "麦")
            engine.set_interlocutor_name("レン")
            self.assertEqual(engine._memory_scope.ltm_scope(req).user_id, "レン")


if __name__ == "__main__":
    unittest.main()
