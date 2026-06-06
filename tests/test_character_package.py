"""Phase 7a: CharacterPackage loading + per-character memory isolation."""

import json
import tempfile
import unittest
from pathlib import Path

from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from spica.adapters.memory import SqliteMemoryAdapter
from spica.core.character import CharacterPackage, load_character_package
from spica.ports.memory import MemoryScope

_REPO = Path(__file__).resolve().parents[1]


class CharacterPackageLoadTest(unittest.TestCase):
    def test_load_spica_package(self):
        pkg = load_character_package(_REPO / "spica_data" / "Spica_skill")
        self.assertIsInstance(pkg, CharacterPackage)
        self.assertEqual(pkg.character_id, "spica")  # from meta.json "slug"
        self.assertEqual(pkg.name, "辻倉朱比華")
        self.assertEqual(pkg.char_name, "スピカ")  # default when meta has no char_name
        self.assertTrue(pkg.skill_dir and pkg.skill_dir.endswith("Spica_skill"))

    def test_load_second_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "mini"
            root.mkdir()
            (root / "meta.json").write_text(
                json.dumps({"slug": "mini", "name": "Mini", "char_name": "ミニ"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (root / "persona.md").write_text("ミニのプロフィール。", encoding="utf-8")
            pkg = load_character_package(root)
        self.assertEqual(pkg.character_id, "mini")
        self.assertEqual(pkg.char_name, "ミニ")

    def test_character_id_falls_back_to_dir_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "no_meta"
            root.mkdir()
            pkg = load_character_package(root)
        self.assertEqual(pkg.character_id, "no_meta")


class MemoryIsolationTest(unittest.TestCase):
    def test_memory_isolated_by_character_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMemoryStore(Path(tmp) / "m.sqlite3")
            adapter = SqliteMemoryAdapter(store, RecentMemory(max_turns=3))
            scope_a = MemoryScope(character_id="char_a", user_id="麦", conversation_id="c1")
            scope_b = MemoryScope(character_id="char_b", user_id="麦", conversation_id="c1")

            adapter.commit_turn(scope_a, "我喜欢简短回答", "うん。", meta={"interlocutor_name": "麦"})

            # Same character sees its memory; a different character does not.
            self.assertTrue(adapter.retrieve(scope_a, "简短", limit=5))
            self.assertEqual(adapter.retrieve(scope_b, "简短", limit=5), [])

    def test_same_character_different_conversation_still_keyed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMemoryStore(Path(tmp) / "m.sqlite3")
            adapter = SqliteMemoryAdapter(store, RecentMemory(max_turns=3))
            a_c1 = MemoryScope(character_id="char_a", user_id="麦", conversation_id="c1")
            a_c2 = MemoryScope(character_id="char_a", user_id="麦", conversation_id="c2")
            adapter.commit_turn(a_c1, "我喜欢简短回答", "うん。", meta={"interlocutor_name": "麦"})
            self.assertTrue(adapter.retrieve(a_c1, "简短", limit=5))
            self.assertEqual(adapter.retrieve(a_c2, "简短", limit=5), [])


if __name__ == "__main__":
    unittest.main()
