"""Phase 1 tests for the SQLite galgame-memory adapter.

Locks: CRUD + read queries for every entity, status transition routed through
with_status (illegal raises), schema_version stamped, and STORAGE ISOLATION --
the galgame DB is a separate file and writing it never touches memory.sqlite3.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from spica.adapters.game_memory.sqlite import SCHEMA_VERSION, GameMemorySqliteAdapter
from spica.galgame.models import (
    CharacterRelation,
    ChoiceEvent,
    CompanionBeat,
    GameProfile,
    GameProgressState,
    PlaySession,
    StoryLine,
    StoryLineStatus,
    StoryLineStatusError,
    StorySummary,
    utc_now_iso,
)


class GameMemoryAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "galgame.sqlite3"
        self.adapter = GameMemorySqliteAdapter(self.db_path)

    # -- infra ---------------------------------------------------------------
    def test_schema_version_stamped(self):
        self.assertEqual(self.adapter.schema_version(), SCHEMA_VERSION)

    def test_db_file_created_at_injected_path(self):
        self.assertTrue(self.db_path.exists())

    # -- game profile --------------------------------------------------------
    def test_game_profile_round_trip(self):
        profile = GameProfile(
            game_id="ABC", display_name="Some Game",
            created_at=utc_now_iso(), updated_at=utc_now_iso(), aliases=["那个游戏"],
        )
        self.adapter.upsert_game_profile(profile)
        self.assertEqual(self.adapter.get_game_profile("ABC"), profile)
        self.assertIsNone(self.adapter.get_game_profile("MISSING"))

    def test_last_played_game_prefers_most_recent(self):
        self.adapter.upsert_game_profile(GameProfile(
            game_id="OLD", display_name="Old", created_at="t", updated_at="t",
            last_played_at="2026-06-01T10:00:00"))
        self.adapter.upsert_game_profile(GameProfile(
            game_id="NEW", display_name="New", created_at="t", updated_at="t",
            last_played_at="2026-06-09T10:00:00"))
        self.adapter.upsert_game_profile(GameProfile(
            game_id="NEVER", display_name="Never", created_at="t", updated_at="t",
            last_played_at=None))
        self.assertEqual(self.adapter.last_played_game().game_id, "NEW")

    # -- play session --------------------------------------------------------
    def test_play_session_crud_and_dangling(self):
        active = PlaySession(session_id="S1", game_id="ABC", started_at=utc_now_iso(), state="active")
        ended = PlaySession(
            session_id="S2", game_id="ABC", started_at=utc_now_iso(),
            state="ended", ended_at=utc_now_iso())
        self.adapter.add_play_session(active)
        self.adapter.add_play_session(ended)

        dangling = self.adapter.dangling_play_sessions()
        self.assertEqual([s.session_id for s in dangling], ["S1"])

        self.adapter.update_play_session("S1", state="paused", ocr_line_count=5)
        self.assertEqual([s.session_id for s in self.adapter.dangling_play_sessions()], ["S1"])

        self.adapter.update_play_session("S1", state="ended", ended_at=utc_now_iso())
        self.assertEqual(self.adapter.dangling_play_sessions(), [])

    def test_update_unknown_session_raises(self):
        with self.assertRaises(KeyError):
            self.adapter.update_play_session("NOPE", state="ended")

    def test_update_session_rejects_unknown_field(self):
        self.adapter.add_play_session(PlaySession(session_id="S1", game_id="ABC", started_at="t"))
        with self.assertRaises(TypeError):
            self.adapter.update_play_session("S1", not_a_field=1)

    # -- story lines + status transition -------------------------------------
    def test_story_line_add_and_commit_via_status(self):
        line = StoryLine(
            line_id="L1", session_id="S1", game_id="ABC", text="こんにちは",
            timestamp="2026-06-09T10:01:00", status=StoryLineStatus.PENDING_CURRENT)
        self.adapter.add_story_line(line)
        # pending_current is not in the committed buffer yet
        self.assertEqual(self.adapter.committed_story_lines("ABC"), [])

        self.adapter.update_story_line_status("L1", StoryLineStatus.COMMITTED)
        committed = self.adapter.committed_story_lines("ABC")
        self.assertEqual([l.line_id for l in committed], ["L1"])
        self.assertEqual(committed[0].status, StoryLineStatus.COMMITTED)

    def test_illegal_status_transition_raises_at_adapter(self):
        line = StoryLine(
            line_id="L1", session_id="S1", game_id="ABC", text="x",
            timestamp="t", status=StoryLineStatus.COMMITTED)
        self.adapter.add_story_line(line)
        with self.assertRaises(StoryLineStatusError):
            self.adapter.update_story_line_status("L1", StoryLineStatus.PENDING_CURRENT)

    def test_committed_lines_ordered_by_timestamp(self):
        for line_id, ts in [("L2", "2026-06-09T10:02:00"), ("L1", "2026-06-09T10:01:00")]:
            self.adapter.add_story_line(StoryLine(
                line_id=line_id, session_id="S1", game_id="ABC", text="x", timestamp=ts,
                status=StoryLineStatus.COMMITTED))
        self.assertEqual([l.line_id for l in self.adapter.committed_story_lines("ABC")], ["L1", "L2"])

    # -- summaries -----------------------------------------------------------
    def test_summaries_recent_first(self):
        for sid, created in [("SM1", "2026-06-09T10:00:00"), ("SM2", "2026-06-09T11:00:00")]:
            self.adapter.add_summary(StorySummary(
                summary_id=sid, game_id="ABC", created_at=created, summary_zh=sid))
        recent = self.adapter.recent_summaries("ABC", limit=5)
        self.assertEqual([s.summary_id for s in recent], ["SM2", "SM1"])

    # -- progress state ------------------------------------------------------
    def test_progress_state_upsert(self):
        state = GameProgressState(game_id="ABC", current_scene_summary="教室")
        self.adapter.upsert_progress_state(state)
        self.assertEqual(self.adapter.get_progress_state("ABC"), state)
        updated = GameProgressState(game_id="ABC", current_scene_summary="走廊")
        self.adapter.upsert_progress_state(updated)
        self.assertEqual(self.adapter.get_progress_state("ABC").current_scene_summary, "走廊")
        self.assertIsNone(self.adapter.get_progress_state("MISSING"))

    # -- character relations -------------------------------------------------
    def test_character_relations(self):
        self.adapter.upsert_character_relation(CharacterRelation(
            relation_id="R1", game_id="ABC", character_a="朱比華", character_b="麦",
            relation_summary="青梅竹马", updated_at="2026-06-09T10:00:00"))
        rels = self.adapter.character_relations("ABC")
        self.assertEqual([r.relation_id for r in rels], ["R1"])

    # -- choice events -------------------------------------------------------
    def test_choice_event_add_update_recent(self):
        self.adapter.add_choice_event(ChoiceEvent(
            choice_id="C1", game_id="ABC", timestamp="2026-06-09T10:00:00",
            options=[{"index": 1, "text": "原谅她"}, {"index": 2, "text": "离开"}]))
        self.adapter.update_choice_event(
            "C1", selected_option_index=2, selected_option_text="离开",
            selection_source="user_reported")
        recent = self.adapter.recent_choice_events("ABC", limit=5)
        self.assertEqual(recent[0].selected_option_index, 2)
        self.assertEqual(recent[0].selection_source, "user_reported")

    # -- companion beats -----------------------------------------------------
    def test_companion_beats_scoped(self):
        self.adapter.add_companion_beat(CompanionBeat(
            beat_id="B1", game_id="ABC", content="我就知道",
            created_at="2026-06-09T10:00:00",
            scope={"character_id": "spica", "user_id": "麦", "game_id": "ABC"}))
        # other user / character must not match
        self.adapter.add_companion_beat(CompanionBeat(
            beat_id="B2", game_id="ABC", content="别人",
            created_at="2026-06-09T10:01:00",
            scope={"character_id": "other", "user_id": "別人", "game_id": "ABC"}))
        beats = self.adapter.companion_beats("ABC", user_id="麦", character_id="spica")
        self.assertEqual([b.beat_id for b in beats], ["B1"])


class StorageIsolationTest(unittest.TestCase):
    def test_galgame_db_is_separate_file_and_does_not_touch_memory_store(self):
        with TemporaryDirectory() as tmp:
            galgame_path = Path(tmp) / "galgame.sqlite3"
            memory_path = Path(tmp) / "memory.sqlite3"

            # Stand up the real character memory store alongside, write a memory.
            from memory.store import SQLiteMemoryStore

            mem = SQLiteMemoryStore(memory_path)
            mem.add_memory("spica::default", scope="user", content="麦 喜欢慢慢看剧情")

            adapter = GameMemorySqliteAdapter(galgame_path)
            adapter.upsert_game_profile(GameProfile(
                game_id="ABC", display_name="G", created_at="t", updated_at="t"))

            # Distinct files; galgame writes do not appear in the memory store.
            self.assertNotEqual(galgame_path, memory_path)
            self.assertTrue(galgame_path.exists())
            self.assertEqual(len(mem.list_memories("spica::default")), 1)
            self.assertEqual(mem.search_memories("spica::default", "ABC"), [])


if __name__ == "__main__":
    unittest.main()
