"""Play-history card v2 (B 方案, FINDINGS #15): template shape ("游戏" framing /
fronted protagonist sentence / bilingual game name / §13.5 route tiers / one
compact line hard-under the 220-char prompt truncation), the retrieval-keyword
guard (the v1 real-machine failure: zero bigram hits -> filtered out), graceful
degradation, and compose's never-played -> None.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from memory.store import SQLiteMemoryStore
from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.galgame.history import (
    CARD_MAX_CHARS,
    ROUTE_CONFIDENCE_THRESHOLD,
    build_play_history_card,
    compose_play_history,
)
from spica.galgame.manual import ManualGameMemory
from spica.galgame.models import (
    CharacterRelation,
    GameProfile,
    GameProgressState,
    StorySummary,
    utc_now_iso,
)


def _progress(route=None, chapter=None):
    return GameProgressState(
        game_id="g1", route=route or {}, chapter=chapter or {},
        last_played_at="2026-06-10T12:00:00",
    )


def _relation(a="雪鹰", b="主人公", summary="青梅竹马", confidence=0.9):
    return CharacterRelation(
        relation_id=f"rel::{a}::{b}", game_id="g1", character_a=a, character_b=b,
        relation_summary=summary, confidence=confidence, updated_at=utc_now_iso(),
    )


def _summary(text="雪鹰在天台向主人公告白，两人确认了心意。", characters=None):
    return StorySummary(
        summary_id="s1", game_id="g1", summary_zh=text,
        characters=list(characters or []), created_at=utc_now_iso(),
    )


class CardShapeTest(unittest.TestCase):
    def test_full_material_card_shape(self):
        card = build_play_history_card(
            display_name="LimeLight Lemonade Jam",
            game_id="limelight",
            progress=_progress(chapter={"title": "第三章", "confidence": 0.8}),
            relations=[_relation()],
            summaries=[_summary(characters=["雪鹰", "麦穗"])],
            played_at="2026-06-10T12:00:00",
        )
        self.assertIn("一起玩了游戏《LimeLight Lemonade Jam》（limelight）", card)  # bilingual name
        self.assertIn("主人公（男主角）是雪鹰", card)  # fronted protagonist sentence
        self.assertLess(card.index("主人公"), card.index("玩到"))  # protagonist BEFORE progress
        self.assertIn("游戏里的雪鹰和主人公是青梅竹马", card)
        self.assertIn("玩到第三章", card)
        self.assertIn("最近剧情：", card)
        self.assertIn("（2026-06-10）", card)
        self.assertNotIn("\n", card)  # one compact line

    def test_retrieval_keyword_guard(self):
        # The v1 real-machine failure: the card had ZERO bigram/token overlap with
        # "limelight 男主叫什么名字" -> store's keyword filter dropped it. Guard the
        # template against losing its retrieval words again, using the REAL store
        # keyword/normalize logic (memory/store.py search filter).
        card = build_play_history_card(
            display_name="LimeLight Lemonade Jam",
            game_id="limelight",
            summaries=[_summary(characters=["雪鹰"])],
        )
        with TemporaryDirectory() as tmp:
            store = SQLiteMemoryStore(Path(tmp) / "m.sqlite3")
            haystack = store._normalize_for_search(card)
            for query in ("limelight 男主叫什么名字", "主人公叫什么", "我们玩过的游戏主角是谁"):
                with self.subTest(query=query):
                    keywords = store._keywords(query)
                    hits = [keyword for keyword in keywords if keyword and keyword in haystack]
                    self.assertTrue(hits, f"card has no keyword overlap with {query!r}")

    def test_protagonist_heuristic_and_omission(self):
        # Frequency across summaries wins; ties break toward the newest list order.
        card = build_play_history_card(
            display_name="G",
            summaries=[
                _summary(characters=["雪鹰", "雄真"]),  # newest
                _summary(characters=["雄真", "雪鹰"]),
                _summary(characters=["雪鹰"]),
            ],
        )
        self.assertIn("主人公（男主角）是雪鹰", card)  # 3 vs 2
        # Undecidable (no characters anywhere) -> the sentence is OMITTED, never
        # guessed. Plot text deliberately avoids the word 主人公 so the assertion
        # checks the SENTENCE, not the snippet.
        none_card = build_play_history_card(
            display_name="G", summaries=[_summary("两人在天台见面。", characters=[])]
        )
        self.assertNotIn("主人公", none_card)

    def test_real_machine_regression_side_pair_top_confidence(self):
        # The exact real-machine material shape: the highest-confidence relation is
        # a SIDE pair (雄真-杰 0.95) while the protagonist 雪鹰 only appears in
        # summary.characters. v1 named no protagonist at all; v2 must front 雪鹰.
        card = build_play_history_card(
            display_name="LimeLight Lemonade Jam",
            game_id="limelight",
            relations=[_relation(a="雄真", b="杰", summary="双胞胎兄弟", confidence=0.95)],
            summaries=[_summary(characters=["雪鹰", "雄真", "杰"])],
        )
        self.assertIn("主人公（男主角）是雪鹰", card)  # NOT 雄真
        self.assertNotIn("主人公（男主角）是雄真", card)
        self.assertIn("游戏里的雄真和杰是双胞胎兄弟", card)  # the side pair still informs

    def test_relations_top_two_by_confidence(self):
        card = build_play_history_card(
            display_name="G",
            relations=[
                _relation(a="雄真", b="杰", summary="双胞胎兄弟", confidence=0.95),
                _relation(a="雪鹰", b="主人公", summary="青梅竹马", confidence=0.9),
                _relation(a="路人甲", b="路人乙", summary="同学", confidence=0.2),
            ],
        )
        self.assertIn("雄真和杰", card)
        self.assertIn("雪鹰和主人公", card)  # top-2 now included (was top-1)
        self.assertNotIn("路人甲", card)

    def test_route_three_tiers(self):
        confirmed = build_play_history_card(
            display_name="G",
            progress=_progress(route={"confirmed": True, "name": "雪鹰", "confidence": 1.0, "source": "player"}),
        )
        self.assertIn("已确认走雪鹰线", confirmed)

        high_guess = build_play_history_card(
            display_name="G",
            progress=_progress(route={"confirmed": False, "name": "雪鹰", "confidence": 0.8, "source": "llm_guess"}),
        )
        self.assertIn("似乎在雪鹰线", high_guess)
        self.assertNotIn("已确认", high_guess)

        low_guess = build_play_history_card(
            display_name="G",
            progress=_progress(route={"confirmed": False, "name": "雪鹰", "confidence": 0.3, "source": "llm_guess"}),
        )
        self.assertNotIn("雪鹰", low_guess)  # below threshold -> say nothing (§13.5)
        self.assertEqual(ROUTE_CONFIDENCE_THRESHOLD, 0.6)

    def test_budget_under_220_with_extreme_material(self):
        # Greedy assembly: overflowing segments drop WHOLE; head + protagonist stay.
        card = build_play_history_card(
            display_name="超" * 60,
            game_id="x" * 40,
            progress=_progress(
                route={"confirmed": True, "name": "线" * 30, "confidence": 1.0},
                chapter={"title": "章" * 40, "confidence": 0.9},
            ),
            relations=[
                _relation(a="甲" * 25, b="乙" * 25, summary="长" * 80, confidence=0.9),
                _relation(a="丙" * 25, b="丁" * 25, summary="更长" * 50, confidence=0.8),
            ],
            summaries=[_summary("剧" * 300, characters=["主" * 30])],
            played_at="2026-06-10T12:00:00",
        )
        self.assertLessEqual(len(card), CARD_MAX_CHARS)  # hard guarantee by construction
        self.assertIn("一起玩了游戏", card)
        self.assertIn("主人公（男主角）是", card)  # fronted segments survive the squeeze
        self.assertIn("（2026-06-10）", card)  # date tail always present

    def test_degrades_without_optional_material(self):
        card = build_play_history_card(display_name="G", progress=None, relations=[], summaries=None)
        self.assertIn("一起玩了游戏《G》", card)  # framing survives with zero material
        self.assertNotIn("主人公", card)
        self.assertNotIn("玩到", card)
        self.assertNotIn("游戏里的", card)
        self.assertNotIn("最近剧情", card)

    def test_same_name_skips_bilingual_duplicate(self):
        card = build_play_history_card(display_name="limelight", game_id="limelight")
        self.assertEqual(card.count("limelight"), 1)  # no《limelight》（limelight）

    def test_custom_user_name(self):
        card = build_play_history_card(display_name="G", user_name="小麦")
        self.assertTrue(card.startswith("小麦和我一起玩了游戏"))


class ComposeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mem = GameMemorySqliteAdapter(Path(self._tmp.name) / "g.sqlite3")

    def test_never_played_returns_none(self):
        self.assertIsNone(compose_play_history(self.mem, "nope"))
        # a bare GameProfile is just a name, not an experience
        now = utc_now_iso()
        self.mem.upsert_game_profile(GameProfile(game_id="g0", display_name="G0", created_at=now, updated_at=now))
        self.assertIsNone(compose_play_history(self.mem, "g0"))

    def test_compose_uses_display_name_and_store_material(self):
        now = utc_now_iso()
        self.mem.upsert_game_profile(
            GameProfile(game_id="limelight", display_name="LimeLight Lemonade Jam", created_at=now, updated_at=now)
        )
        facade = ManualGameMemory(self.mem, character_id="spica", user_id="麦")
        facade.manual_add_story_line("limelight", "雪鹰", "你好")
        facade.manual_flush_summary("limelight")
        facade.manual_set_progress_state("limelight", chapter={"title": "第一章", "confidence": 0.7})
        card = compose_play_history(self.mem, "limelight")
        self.assertIsNotNone(card)
        self.assertIn("《LimeLight Lemonade Jam》（limelight）", card)  # bilingual from profile + id
        self.assertIn("玩到第一章", card)
        self.assertIn("最近剧情：", card)
        # placeholder summaries carry no characters -> protagonist omitted, not guessed
        self.assertNotIn("主人公", card)

    def test_compose_protagonist_from_summary_characters(self):
        now = utc_now_iso()
        self.mem.add_summary(StorySummary(
            summary_id="s1", game_id="g1", summary_zh="雪鹰登场。",
            characters=["雪鹰", "雄真"], created_at=now, updated_at=now,
        ))
        card = compose_play_history(self.mem, "g1")
        self.assertIn("主人公（男主角）是雪鹰", card)

    def test_compose_falls_back_to_game_id_without_profile(self):
        facade = ManualGameMemory(self.mem, character_id="spica", user_id="麦")
        facade.manual_set_progress_state("g1", chapter={"title": "第一章", "confidence": 0.7})
        card = compose_play_history(self.mem, "g1")
        self.assertIn("《g1》", card)
        self.assertEqual(card.count("g1"), 1)  # display_name == game_id -> no duplicate


if __name__ == "__main__":
    unittest.main()
