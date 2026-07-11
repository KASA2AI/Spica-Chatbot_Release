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


class _FailingMethodMemory:
    """Delegating GameMemoryPort proxy: the NAMED method always raises, everything
    else passes through to the real adapter (AR-C1 §9.1 recovery fault injection)."""

    def __init__(self, real, fail_method):
        self._real = real
        self._fail_method = fail_method

    def __getattr__(self, name):
        if name == self._fail_method:
            def _boom(*args, **kwargs):
                raise RuntimeError(f"injected {name} failure")
            return _boom
        return getattr(self._real, name)


class RecoveryCharacterizationTest(unittest.TestCase):
    """AR-C1 Slice 0 (§9.1): pin recover_dangling_sessions' CURRENT shape. Recovery
    production code is untouched this phase (D2a=A2 / D2b=accepted), so every test
    here must stay green through all AR-C1 slices."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mem = GameMemorySqliteAdapter(Path(self._tmp.name) / "g.sqlite3")

    def _seed_dangling(self, session_id="S1", *, lines=("L1", "L2")):
        self.mem.add_play_session(
            PlaySession(session_id=session_id, game_id="ABC", started_at=utc_now_iso(), state="active")
        )
        for lid in lines:
            self.mem.add_story_line(_line(lid, f"text-{lid}", session_id=session_id))

    def test_update_play_session_failure_propagates_then_next_pass_skips_resummarize(self):
        # §9.1-1: add_summary is INSIDE recovery's try, update_play_session (:203) is
        # OUTSIDE -- its failure propagates out with the summary already durable and
        # the session still dangling.
        self._seed_dangling()
        flaky = _FailingMethodMemory(self.mem, "update_play_session")
        with self.assertRaises(RuntimeError):
            recover_dangling_sessions(flaky, _summarizer(_FakeLLM(GOOD)))
        self.assertEqual(len(self.mem.recent_summaries("ABC")), 1)  # summary durable
        self.assertEqual(self.mem.get_play_session("S1").state, "active")  # still dangling
        self.assertEqual([d.session_id for d in self.mem.dangling_play_sessions()], ["S1"])
        # next-startup pass: reverse-lookup sees the batch as covered -> lines=[],
        # NO second summarize, session goes straight to ended, still one summary.
        working = _FakeLLM(GOOD)
        self.assertEqual(recover_dangling_sessions(self.mem, _summarizer(working)), ["S1"])
        self.assertEqual(working.calls, [])
        self.assertEqual(len(self.mem.recent_summaries("ABC")), 1)
        self.assertEqual(self.mem.get_play_session("S1").state, "ended")

    def test_llm_failure_marks_interrupted_and_still_counts_in_recovered(self):
        # §9.1-2 (LLM branch): interrupted sessions still count in the return value.
        self._seed_dangling()
        recovered = recover_dangling_sessions(self.mem, _summarizer(_FakeLLM(raise_=True)))
        self.assertEqual(recovered, ["S1"])
        self.assertEqual(self.mem.get_play_session("S1").state, "interrupted")
        self.assertEqual(len(self.mem.unsummarized_committed_story_lines("ABC")), 2)

    def test_insert_failure_marks_interrupted_and_still_counts_in_recovered(self):
        # §9.1-2 (insert branch): add_summary raising inside the try folds the same
        # way -- interrupted, lines kept, still counted.
        self._seed_dangling()
        flaky = _FailingMethodMemory(self.mem, "add_summary")
        recovered = recover_dangling_sessions(flaky, _summarizer(_FakeLLM(GOOD)))
        self.assertEqual(recovered, ["S1"])
        self.assertEqual(self.mem.get_play_session("S1").state, "interrupted")
        self.assertEqual(self.mem.recent_summaries("ABC"), [])
        self.assertEqual(len(self.mem.unsummarized_committed_story_lines("ABC")), 2)

    def test_no_lines_dangling_is_finalized_without_summarize(self):
        # §9.1-3: nothing to summarize -> summarize is never called, straight to ended.
        self._seed_dangling(lines=())
        working = _FakeLLM(GOOD)
        recovered = recover_dangling_sessions(self.mem, _summarizer(working))
        self.assertEqual(recovered, ["S1"])
        self.assertEqual(working.calls, [])
        self.assertEqual(self.mem.get_play_session("S1").state, "ended")
        self.assertEqual(self.mem.recent_summaries("ABC"), [])

    def test_successful_recovery_writes_no_progress_or_relations(self):
        # §9.1-4: recovery only 補總結 -- GOOD carries route_guess AND relations, yet
        # neither progress nor relations are written.
        self._seed_dangling()
        recover_dangling_sessions(self.mem, _summarizer(_FakeLLM(GOOD)))
        self.assertIsNone(self.mem.get_progress_state("ABC"))
        self.assertEqual(self.mem.character_relations("ABC"), [])

    def test_d2b_residual_risk_lines_consumed_projection_permanently_missing(self):
        # #37 (D2b=accepted, resident pin): a successful recovery CONSUMES the batch
        # (summary covers the lines, session ended) while progress/relations for that
        # batch stay missing forever -- the accepted summary-only residual risk.
        self._seed_dangling()
        recover_dangling_sessions(self.mem, _summarizer(_FakeLLM(GOOD)))
        self.assertEqual(self.mem.unsummarized_committed_story_lines("ABC"), [])
        self.assertEqual(self.mem.get_play_session("S1").state, "ended")
        self.assertIsNone(self.mem.get_progress_state("ABC"))
        self.assertEqual(self.mem.character_relations("ABC"), [])


if __name__ == "__main__":
    unittest.main()
