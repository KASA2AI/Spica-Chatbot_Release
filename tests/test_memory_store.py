import tempfile
import unittest
from pathlib import Path

from memory.store import SQLiteMemoryStore


class MemoryStoreTest(unittest.TestCase):
    def test_unrelated_queries_do_not_inject_pinned_memory_or_reinforce_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.add_memory("c1", "user", "喜欢雨天听夏影", 0.9, pinned=True)
            for query in ("你呢", "谢谢", "", "主板供电接口"):
                self.assertEqual(store.search_memories("c1", query), [])
            for _ in range(3):
                self.assertEqual(len(store.search_memories("c1", "雨天")), 1)
            self.assertEqual(store.list_memories("c1")[0]["use_count"], 0)

    def test_add_search_and_clear(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            memory_id = store.add_memory("c1", "user", "我喜欢安静的回答", 0.8)

            results = store.search_memories("c1", "我喜欢安静", limit=5)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["id"], memory_id)
            self.assertEqual(results[0]["use_count"], 0)

            updated = store.list_memories("c1")
            self.assertEqual(updated[0]["use_count"], 0)  # retrieval is not user support

            store.clear_memories("c1")
            self.assertEqual(store.list_memories("c1"), [])

    def test_upsert_memory_updates_existing_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            first_id = store.upsert_memory(
                "c1",
                "user",
                "kasa喜欢薄饼披萨",
                0.6,
                memory_key="user:preference:pizza",
                memory_type="preference:like",
            )
            second_id = store.upsert_memory(
                "c1",
                "user",
                "kasa喜欢石窑薄饼披萨",
                0.9,
                memory_key="user:preference:pizza",
                memory_type="preference:like",
            )

            memories = store.list_memories("c1")
            self.assertEqual(first_id, second_id)
            self.assertEqual(len(memories), 1)
            self.assertEqual(memories[0]["content"], "kasa喜欢石窑薄饼披萨")
            self.assertEqual(memories[0]["importance"], 0.9)
            self.assertEqual(memories[0]["memory_type"], "preference:like")

    def test_search_uses_cjk_bigrams(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.upsert_memory("c1", "user", "kasa提到自己的名字是kasa", 0.85)

            results = store.search_memories("c1", "我叫什么名字", limit=5)
            self.assertEqual(len(results), 1)
            self.assertIn("名字", results[0]["content"])


if __name__ == "__main__":
    unittest.main()
