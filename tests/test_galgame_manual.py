"""Phase 2 tests for the manual galgame-memory feed facade.

Locks: the five manual_* write paths land in GameMemoryPort; the
"buffer = unsummarized committed lines" reverse-lookup advances correctly
(welded directly to summary persistence); ChoiceEvent's two paths (options /
selected-text-only).
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.galgame.manual import ManualGameMemory
from spica.galgame.models import StoryLineStatus


class ManualGameMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.adapter = GameMemorySqliteAdapter(Path(self._tmp.name) / "galgame.sqlite3")
        self.facade = ManualGameMemory(self.adapter, character_id="spica", user_id="麦")

    # -- five manual_* round trips -------------------------------------------
    def test_manual_add_story_line_lands_committed(self):
        line_id = self.facade.manual_add_story_line("ABC", "朱比華", "こんにちは")
        committed = self.adapter.committed_story_lines("ABC")
        self.assertEqual([l.line_id for l in committed], [line_id])
        line = committed[0]
        self.assertEqual(line.status, StoryLineStatus.COMMITTED)  # never pending_current
        self.assertEqual(line.source, "manual")
        self.assertEqual(line.speaker, "朱比華")
        self.assertEqual(line.text, "こんにちは")
        self.assertEqual(line.session_id, "manual::ABC::default")

    def test_manual_flush_summary_round_trip(self):
        ids = [
            self.facade.manual_add_story_line("ABC", "A", "l1"),
            self.facade.manual_add_story_line("ABC", "B", "l2"),
        ]
        summary_id = self.facade.manual_flush_summary("ABC")
        self.assertIsNotNone(summary_id)
        summaries = self.adapter.recent_summaries("ABC")
        self.assertEqual([s.summary_id for s in summaries], [summary_id])
        self.assertEqual(set(summaries[0].source_line_ids), set(ids))
        self.assertIn("l1", summaries[0].summary_zh)  # placeholder concat

    def test_manual_set_progress_state_merge_upsert(self):
        self.facade.manual_set_progress_state(
            "ABC", chapter={"title": "第一章", "confidence": 0.7}
        )
        self.facade.manual_set_progress_state("ABC", current_scene_summary="教室")
        state = self.adapter.get_progress_state("ABC")
        # second call updates only the provided field; the first survives (merge).
        self.assertEqual(state.chapter, {"title": "第一章", "confidence": 0.7})
        self.assertEqual(state.current_scene_summary, "教室")
        self.assertTrue(state.last_played_at)  # auto-stamped

    def test_manual_set_progress_state_rejects_unknown_field(self):
        with self.assertRaises(TypeError):
            self.facade.manual_set_progress_state("ABC", not_a_field=1)

    def test_manual_add_companion_beat_scoped(self):
        beat_id = self.facade.manual_add_companion_beat(
            "ABC", "reaction", "我就知道这个人有问题"
        )
        beats = self.adapter.companion_beats("ABC", user_id="麦", character_id="spica")
        self.assertEqual([b.beat_id for b in beats], [beat_id])
        self.assertEqual(beats[0].type, "reaction")
        self.assertEqual(beats[0].content, "我就知道这个人有问题")
        self.assertEqual(
            beats[0].scope,
            {"character_id": "spica", "user_id": "麦", "game_id": "ABC"},
        )

    # -- ChoiceEvent two paths (§14.4) ---------------------------------------
    def test_choice_event_with_options_dict_and_index(self):
        choice_id = self.facade.manual_add_choice_event(
            "ABC",
            options=[{"index": 1, "text": "原谅她"}, {"index": 2, "text": "离开"}],
            selected_option=2,
        )
        event = self.adapter.recent_choice_events("ABC")[0]
        self.assertEqual(event.choice_id, choice_id)
        self.assertEqual(
            event.options, [{"index": 1, "text": "原谅她"}, {"index": 2, "text": "离开"}]
        )
        self.assertEqual(event.selected_option_index, 2)
        self.assertEqual(event.selected_option_text, "离开")
        self.assertEqual(event.selection_source, "user_reported")

    def test_choice_event_with_string_options(self):
        self.facade.manual_add_choice_event(
            "ABC", options=["原谅她", "离开"], selected_option="离开"
        )
        event = self.adapter.recent_choice_events("ABC")[0]
        self.assertEqual(
            event.options, [{"index": 1, "text": "原谅她"}, {"index": 2, "text": "离开"}]
        )
        self.assertEqual(event.selected_option_index, 2)
        self.assertEqual(event.selected_option_text, "离开")

    def test_choice_event_selected_text_only(self):
        # No options known, only a reported selection (§14.4 manual path).
        choice_id = self.facade.manual_add_choice_event("ABC", selected_option="原谅她")
        event = self.adapter.recent_choice_events("ABC")[0]
        self.assertEqual(event.choice_id, choice_id)
        self.assertEqual(event.options, [])
        self.assertIsNone(event.selected_option_index)
        self.assertEqual(event.selected_option_text, "原谅她")
        self.assertEqual(event.selection_source, "user_reported")

    # -- buffer advance (reverse-lookup) -------------------------------------
    def test_buffer_advance_welded_to_summary_persistence(self):
        l1 = self.facade.manual_add_story_line("ABC", "A", "l1")
        l2 = self.facade.manual_add_story_line("ABC", "B", "l2")
        # before flush: both lines are in the buffer
        self.assertEqual(
            {l.line_id for l in self.adapter.unsummarized_committed_story_lines("ABC")},
            {l1, l2},
        )

        summary_id = self.facade.manual_flush_summary("ABC")
        self.assertIsNotNone(summary_id)

        # WELD: right after a successful flush, with NO new lines fed, the buffer
        # is empty -- reverse-lookup advance is driven by the persisted summary.
        self.assertEqual(self.adapter.unsummarized_committed_story_lines("ABC"), [])

        # feed a new line: only it is in the buffer now
        l3 = self.facade.manual_add_story_line("ABC", "A", "l3")
        self.assertEqual(
            {l.line_id for l in self.adapter.unsummarized_committed_story_lines("ABC")},
            {l3},
        )

        # second flush covers only the new line, disjoint from the first batch
        summary_id_2 = self.facade.manual_flush_summary("ABC")
        s2 = next(
            s for s in self.adapter.recent_summaries("ABC", limit=10) if s.summary_id == summary_id_2
        )
        self.assertEqual(set(s2.source_line_ids), {l3})
        self.assertTrue({l1, l2}.isdisjoint(set(s2.source_line_ids)))

    def test_flush_with_empty_buffer_returns_none(self):
        self.assertIsNone(self.facade.manual_flush_summary("ABC"))
        # after a flush drains the buffer, a second flush also no-ops
        self.facade.manual_add_story_line("ABC", "A", "l1")
        self.assertIsNotNone(self.facade.manual_flush_summary("ABC"))
        self.assertIsNone(self.facade.manual_flush_summary("ABC"))

    def test_buffer_isolated_by_playthrough(self):
        a = self.facade.manual_add_story_line("ABC", "A", "la", playthrough_id="default")
        b = self.facade.manual_add_story_line("ABC", "B", "lb", playthrough_id="ng+")
        summary_id = self.facade.manual_flush_summary("ABC", playthrough_id="default")
        # the default-playthrough summary covers only its own line
        s = self.adapter.recent_summaries("ABC", playthrough_id="default")[0]
        self.assertEqual(s.summary_id, summary_id)
        self.assertEqual(set(s.source_line_ids), {a})
        # flushing "default" must not drain the "ng+" buffer
        self.assertEqual(self.adapter.unsummarized_committed_story_lines("ABC", "default"), [])
        self.assertEqual(
            {l.line_id for l in self.adapter.unsummarized_committed_story_lines("ABC", "ng+")},
            {b},
        )


if __name__ == "__main__":
    unittest.main()
