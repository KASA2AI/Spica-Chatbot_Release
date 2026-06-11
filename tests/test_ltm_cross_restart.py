"""Cross-restart long-term memory persistence (FINDINGS #16 regression).

The Initial-release UI used a per-launch uuid4 conversation_id, so every
long-term memory landed in a ``spica::<uuid>`` silo and became unreachable
after restart -- exposed by the play-history card (written to spica::default,
never scanned). The fix aligns the UI on the stable "default" id.

"Restart" is simulated faithfully: the SAME sqlite file, a FRESH store instance
per launch (the store opens a new connection per call; a new instance is
process-equivalent), retrieval through the REAL turn node
(retrieve_long_term_memory_node), not a store shortcut.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from memory.store import SQLiteMemoryStore
from spica.adapters.memory.sqlite import SqliteMemoryAdapter, scoped_conversation_id
from spica.config.schema import AppConfig, CharacterConfig
from spica.runtime.context import TurnContext, TurnRequest
from spica.runtime.deps import TurnDeps
from spica.runtime.stages import retrieve_long_term_memory_node
from spica.runtime.tools import RegistryToolSet


def _retrieve(store: SQLiteMemoryStore, request: TurnRequest) -> list[str]:
    ctx = TurnContext(request)
    deps = TurnDeps(
        config=AppConfig(character=CharacterConfig(character_id="spica", interlocutor_name="麦")),
        llm=None,
        tts=None,
        visual=None,
        memory=SqliteMemoryAdapter(store),
        tools=RegistryToolSet.from_function_table([], {}),
    )
    retrieve_long_term_memory_node(ctx, None, deps)
    return [m["content"] for m in (ctx.recent.long_term_memories if ctx.recent else [])]


CARD = (
    "麦和我一起玩了游戏《LimeLight Lemonade Jam》（limelight）。"
    "主人公（男主角）是雪鹰。最近剧情：雪鹰在天台向主人公告白。（2026-06-10）"
)


class CrossRestartPersistenceTest(unittest.TestCase):
    def test_memory_survives_restart_under_stable_conversation_id(self):
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.sqlite3"
            # -- "launch 1": write one memory (the real card, the real write shape)
            launch1 = SQLiteMemoryStore(db)
            launch1.upsert_memory(
                conversation_id=scoped_conversation_id("spica", "default"),
                scope="relationship",
                content=CARD,
                importance=0.85,
                memory_key="galgame_history:limelight",
                memory_type="experience",
                source="galgame_companion",
            )
            del launch1
            # -- "launch 2": a FRESH store over the same file; a plain turn on the
            # STABLE conversation_id retrieves it through the real turn node.
            launch2 = SQLiteMemoryStore(db)
            texts = _retrieve(
                launch2,
                TurnRequest(user_input="刚刚玩的limelight男主叫什么", conversation_id="default"),
            )
            self.assertTrue(any("雪鹰" in text for text in texts), texts)
            self.assertTrue(any("limelight" in text.lower() for text in texts))

    def test_uuid_silo_was_the_bug_not_the_scoring(self):
        # Pin the failure MECHANISM the fix removed: the same card, queried from a
        # per-launch-uuid conversation (the old UI behaviour), is NEVER scanned --
        # retrieval is silo-scoped, not score-limited.
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.sqlite3"
            store = SQLiteMemoryStore(db)
            store.upsert_memory(
                conversation_id=scoped_conversation_id("spica", "default"),
                scope="relationship",
                content=CARD,
                importance=0.85,
                memory_key="galgame_history:limelight",
            )
            texts = _retrieve(
                store,
                TurnRequest(
                    user_input="刚刚玩的limelight男主叫什么",
                    conversation_id="60951983-e18f-415d-b753-bdd612a0babe",  # uuid-style launch silo
                ),
            )
            self.assertEqual(texts, [])  # perfect card, wrong silo -> invisible


if __name__ == "__main__":
    unittest.main()
