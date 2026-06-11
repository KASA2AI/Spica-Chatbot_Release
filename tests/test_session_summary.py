"""Phase 8: session background summarization -- non-blocking + snapshot, failure
fold/retry, end 補總結, and §13.5 (player route authority over LLM proposals)."""

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.galgame.session import GalgameCompanionSession, GalgameState
from spica.galgame.summarizer import SummaryError, SummaryResult
from spica.runtime.jobs import InlineJobRunner, ThreadJobRunner


class _StubSummarizer:
    def __init__(self, *, result=None, sleep=0.0, fail_first=0):
        self.result = result or SummaryResult(summary_zh="S", characters=["麦"])
        self.sleep = sleep
        self.fail_first = fail_first
        self.calls = []

    def summarize(self, lines, *, recent_summaries=None, progress=None):
        self.calls.append([l.line_id for l in lines])
        if self.sleep:
            time.sleep(self.sleep)
        if self.fail_first > 0:
            self.fail_first -= 1
            raise SummaryError("boom")
        return self.result


class _Sink:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)

    def of(self, kind):
        return [e for e in self.events if e.kind == kind]


class SessionSummaryBase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mem = GameMemorySqliteAdapter(Path(self._tmp.name) / "g.sqlite3")
        self.sink = _Sink()

    def _session(self, *, jobs, summarizer, trigger):
        s = GalgameCompanionSession(self.mem, emit=self.sink, jobs=jobs, summarizer=summarizer, summary_trigger_chars=trigger)
        s.bind_game("ABC")
        s.start()
        return s

    def _feed(self, session, *texts):
        for text in texts:
            session.on_ocr_result(text)


class NonBlockingAndSnapshotTest(SessionSummaryBase):
    def test_summary_does_not_block_ocr_and_only_snapshot_batch_leaves_buffer(self):
        stub = _StubSummarizer(result=SummaryResult(summary_zh="S"), sleep=0.3)
        s = self._session(jobs=ThreadJobRunner(), summarizer=stub, trigger=2)
        # commit AA (chars=2 hits trigger) -> background summary starts (sleeping)
        self._feed(s, "AA", "AA", "BB", "BB")
        self.assertEqual(s.state, GalgameState.BACKGROUND_SUMMARIZING)
        before = len(s.unsummarized_line_ids)
        # OCR keeps collecting WHILE the LLM sleeps (must not block or raise)
        self._feed(s, "CC", "CC", "DD", "DD")
        self.assertGreater(len(s.unsummarized_line_ids), before)  # buffer advanced concurrently
        # wait for the background summary to finish + apply
        deadline = time.time() + 3.0
        while s.state == GalgameState.BACKGROUND_SUMMARIZING and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(s.state, GalgameState.PLAYING)
        summaries = self.mem.recent_summaries("ABC")
        self.assertEqual(len(summaries), 1)
        # snapshot was [AA] only -> lines collected during the LLM call are NOT in it
        self.assertEqual(len(summaries[0].source_line_ids), 1)
        committed = {l.text: l.line_id for l in self.mem.committed_story_lines("ABC")}
        self.assertEqual(summaries[0].source_line_ids, [committed["AA"]])


class FailureFoldTest(SessionSummaryBase):
    def test_failed_summary_keeps_lines_and_next_summary_folds_them(self):
        stub = _StubSummarizer(result=SummaryResult(summary_zh="S"), fail_first=1)  # fail 1st, succeed after
        s = self._session(jobs=InlineJobRunner(), summarizer=stub, trigger=2)
        self._feed(s, "AA", "AA", "BB", "BB")  # trigger -> inline summary FAILS
        self.assertEqual(self.mem.recent_summaries("ABC"), [])  # nothing persisted
        self.assertEqual(s.state, GalgameState.PLAYING)
        self.assertIsNone(self.sink.of("galgame_summary_done")[-1].summary_id)  # failure signal
        aa_id = self.mem.committed_story_lines("ABC")[0].line_id
        self.assertIn(aa_id, s.unsummarized_line_ids)  # AA still unsummarized

        self._feed(s, "CC", "CC")  # commit BB -> trigger again -> succeeds, folds AA+BB
        summaries = self.mem.recent_summaries("ABC")
        self.assertEqual(len(summaries), 1)
        ids = {l.text: l.line_id for l in self.mem.committed_story_lines("ABC")}
        self.assertEqual(set(summaries[0].source_line_ids), {ids["AA"], ids["BB"]})  # folded


class EndSummaryTest(SessionSummaryBase):
    def test_end_summarizes_remaining_and_marks_ended(self):
        stub = _StubSummarizer(result=SummaryResult(summary_zh="end summary", characters=["麦"]))
        s = self._session(jobs=InlineJobRunner(), summarizer=stub, trigger=100000)  # no auto-trigger
        sid = s.session_id
        self._feed(s, "L1", "L1", "L2", "L2")  # commit L1; L2 pending
        s.end()  # commits L2, summarizes [L1, L2], marks ended
        self.assertEqual(s.state, GalgameState.GAME_LAUNCHED)
        summaries = self.mem.recent_summaries("ABC")
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].summary_zh, "end summary")
        ids = {l.text: l.line_id for l in self.mem.committed_story_lines("ABC")}
        self.assertEqual(set(summaries[0].source_line_ids), {ids["L1"], ids["L2"]})
        self.assertEqual(self.mem.get_play_session(sid).state, "ended")


class RouteAuthorityTest(SessionSummaryBase):
    def test_player_declaration_not_overwritten_by_llm_proposal(self):
        s = self._session(jobs=InlineJobRunner(), summarizer=_StubSummarizer(), trigger=100000)
        s.declare_route("A线")  # player authority (§13.5)
        route = self.mem.get_progress_state("ABC").route
        self.assertTrue(route["confirmed"])
        self.assertEqual(route["name"], "A线")
        # an LLM proposal of a DIFFERENT route must NOT overwrite the confirmed one
        s._apply_progress_and_relations(SummaryResult(route_guess={"name": "B线", "confidence": 0.9, "evidence": ["x"]}))
        route = self.mem.get_progress_state("ABC").route
        self.assertTrue(route["confirmed"])
        self.assertEqual(route["name"], "A线")  # still the player's route

    def test_llm_route_is_a_guess_when_player_has_not_declared(self):
        s = self._session(jobs=InlineJobRunner(), summarizer=_StubSummarizer(), trigger=100000)
        s._apply_progress_and_relations(SummaryResult(route_guess={"name": "B线", "confidence": 0.5, "evidence": []}))
        route = self.mem.get_progress_state("ABC").route
        self.assertFalse(route["confirmed"])
        self.assertEqual(route["name"], "B线")
        self.assertEqual(route["source"], "llm_guess")


if __name__ == "__main__":
    unittest.main()
