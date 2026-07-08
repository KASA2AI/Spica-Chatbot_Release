"""Phase 8: GalgameSummarizer parse/route-guess + dangling recovery (LLM mocked)."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.galgame.models import PlaySession, StoryLine, StoryLineStatus, utc_now_iso
from spica.galgame.summarizer import (
    GalgameSummarizer,
    SummaryError,
    recover_dangling_sessions,
)
from spica.ports.model import BoundModel

GOOD = (
    '{"summary_zh":"麦和六花一起去真澄镇","characters":["麦","六花"],'
    '"major_events":["前往真澄镇"],"unresolved_threads":["神社的秘密"],'
    '"key_lines":["一起去吧"],"emotional_tone":"日常",'
    '"route_guess":{"name":"六花线","confidence":0.6,"evidence":["六花同行"]},'
    '"chapter_guess":{"title":"Day 1","confidence":0.9},'
    '"relations":[{"character_a":"麦","character_b":"六花","relation_summary":"同行","confidence":0.7,"evidence":["一起出发"]}]}'
)


class _FakeLLM:
    def __init__(self, text="", *, raise_=False):
        self.text = text
        self.raise_ = raise_
        self.calls = []

    # Adapter-side TextModel v2 shape (Phase 6a): the summarizer holds a
    # BoundModel, whose adapter half this fake plays.
    def complete(self, prompt, *, model):
        self.calls.append((prompt, model))
        if self.raise_:
            raise RuntimeError("llm down")
        return self.text


def _summarizer(fake):
    return GalgameSummarizer(BoundModel(fake, "m"))


def _line(line_id, text, session_id="S1"):
    return StoryLine(
        line_id=line_id, session_id=session_id, game_id="ABC", text=text, timestamp="t",
        status=StoryLineStatus.COMMITTED,
    )


class SummarizerParseTest(unittest.TestCase):
    def test_parses_structured_result(self):
        result = _summarizer(_FakeLLM(GOOD)).summarize([_line("L1", "おはよう")])
        self.assertEqual(result.summary_zh, "麦和六花一起去真澄镇")
        self.assertEqual(result.characters, ["麦", "六花"])
        self.assertEqual(result.route_guess["name"], "六花线")
        self.assertEqual(result.relations[0]["character_b"], "六花")

    def test_route_guess_has_no_confirmed_key(self):
        # §13.5: the LLM result is a GUESS -- it must not assert "confirmed".
        result = _summarizer(_FakeLLM(GOOD)).summarize([_line("L1", "x")])
        self.assertNotIn("confirmed", result.route_guess)

    def test_json_in_code_fence_is_extracted(self):
        result = _summarizer(_FakeLLM("```json\n" + GOOD + "\n```")).summarize([_line("L1", "x")])
        self.assertEqual(result.summary_zh, "麦和六花一起去真澄镇")

    def test_garbage_output_raises_summary_error(self):
        with self.assertRaises(SummaryError):
            _summarizer(_FakeLLM("not json at all")).summarize([_line("L1", "x")])

    def test_llm_failure_raises_summary_error(self):
        with self.assertRaises(SummaryError):
            _summarizer(_FakeLLM(raise_=True)).summarize([_line("L1", "x")])

    def test_empty_lines_raises(self):
        with self.assertRaises(SummaryError):
            _summarizer(_FakeLLM(GOOD)).summarize([])


class RecoverDanglingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mem = GameMemorySqliteAdapter(Path(self._tmp.name) / "g.sqlite3")
        # a session left active with no ended_at + two committed (unsummarized) lines
        self.mem.add_play_session(PlaySession(session_id="S1", game_id="ABC", started_at=utc_now_iso(), state="active"))
        for lid, text in [("L1", "おはよう"), ("L2", "いってきます")]:
            self.mem.add_story_line(_line(lid, text))

    def test_recovers_with_summary_and_marks_ended(self):
        recovered = recover_dangling_sessions(self.mem, _summarizer(_FakeLLM(GOOD)))
        self.assertEqual(recovered, ["S1"])
        summaries = self.mem.recent_summaries("ABC")
        self.assertEqual(len(summaries), 1)
        self.assertEqual(set(summaries[0].source_line_ids), {"L1", "L2"})
        self.assertEqual(self.mem.get_play_session("S1").state, "ended")

    def test_failed_recovery_marks_interrupted_and_keeps_lines(self):
        recover_dangling_sessions(self.mem, _summarizer(_FakeLLM(raise_=True)))
        self.assertEqual(self.mem.recent_summaries("ABC"), [])  # nothing persisted
        self.assertEqual(self.mem.get_play_session("S1").state, "interrupted")
        # lines remain unsummarized -> still recoverable later
        self.assertEqual(len(self.mem.unsummarized_committed_story_lines("ABC")), 2)


if __name__ == "__main__":
    unittest.main()
