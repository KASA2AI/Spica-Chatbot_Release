"""Phase 7: session.on_ocr_result text stream -- pending -> committed -> persisted,
same line not rewritten, end() commits the final pending line."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.galgame.session import GalgameCompanionSession, GalgameState, GalgameStateError


class _Sink:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)

    def of(self, kind):
        return [e for e in self.events if e.kind == kind]


class SessionOcrStreamTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mem = GameMemorySqliteAdapter(Path(self._tmp.name) / "g.sqlite3")
        self.sink = _Sink()
        self.session = GalgameCompanionSession(self.mem, emit=self.sink)
        self.session.bind_game("ABC")
        self.session.start()  # -> playing, creates the play session + tracker

    def test_pending_then_commit_on_change_persisted(self):
        s = self.session
        s.on_ocr_result("台詞A")
        s.on_ocr_result("台詞A")  # 2nd identical -> NEW_STABLE -> pending A written
        self.assertEqual(s.pending_current_line.text, "台詞A")
        self.assertEqual(self.mem.committed_story_lines("ABC"), [])  # A is pending, not committed

        s.on_ocr_result("台詞B")
        s.on_ocr_result("台詞B")  # B settles -> commit A, write pending B
        committed = self.mem.committed_story_lines("ABC")
        self.assertEqual([l.text for l in committed], ["台詞A"])  # A committed + persisted
        self.assertEqual(s.pending_current_line.text, "台詞B")
        self.assertEqual(list(s.unsummarized_line_ids), [committed[0].line_id])
        self.assertEqual([e.text for e in self.sink.of("galgame_stable_line_committed")], ["台詞A"])

    def test_same_line_not_rewritten(self):
        s = self.session
        s.on_ocr_result("X")
        s.on_ocr_result("X")  # pending X
        pending_id = s.pending_current_line.line_id
        s.on_ocr_result("X")  # SAME -> must NOT create a new line
        self.assertEqual(s.pending_current_line.line_id, pending_id)

    def test_end_commits_final_pending(self):
        s = self.session
        s.on_ocr_result("最後の台詞")
        s.on_ocr_result("最後の台詞")  # pending
        s.end()  # §16.4: pending -> committed
        self.assertEqual([l.text for l in self.mem.committed_story_lines("ABC")], ["最後の台詞"])
        self.assertEqual(s.state, GalgameState.GAME_LAUNCHED)

    def test_on_ocr_result_requires_playing(self):
        self.session.pause()
        with self.assertRaises(GalgameStateError):
            self.session.on_ocr_result("x")


if __name__ == "__main__":
    unittest.main()
