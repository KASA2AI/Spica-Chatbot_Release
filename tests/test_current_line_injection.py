"""B1 acceptance pins for [CURRENT_LINE] injection (the pending_current half of
CLAUDE.md §4's "问答读 committed 历史快照 + 由 owner 原子读一次当前 pending_current").

The line currently on screen is a PENDING_CURRENT row, excluded from the COMMITTED
buffer; B1 reads it from the DB (NOT session memory) and injects it as [CURRENT_LINE]
on active companion turns. Three pins:

  ① timing      -- the read is consistent at every point of the OCR thread's
                   NEW_STABLE write sequence (事务① commit-old, 事务② write-new);
                   status partitions the line into exactly one of the two readers.
  ② session     -- a crash-residue PENDING_CURRENT row from an already-ended session
                   (dangling recovery does NOT reconcile pending rows) is NEVER
                   returned to a new live session -- the read is session-scoped.
  ③ injection   -- the gated stage injects [CURRENT_LINE] on an ACTIVE turn and
                   only then (offline has no live session); crash-residue isolation
                   holds end-to-end through the node too.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.config.schema import AppConfig, CharacterConfig
from spica.galgame.manual import ManualGameMemory
from spica.galgame.models import StoryLine, StoryLineStatus
from spica.runtime.context import GameContextRequest, PromptBundle, TurnContext, TurnRequest
from spica.runtime.deps import TurnDeps
from spica.runtime.observer import DefaultTurnObserver
from spica.runtime.stages import retrieve_game_context_node
from spica.runtime.tools import RegistryToolSet

BASE_PROMPT = "[CURRENT_USER_INPUT]\n现在这句什么意思"


def _line(
    line_id: str,
    session_id: str,
    text: str,
    status: StoryLineStatus,
    *,
    game_id: str = "ABC",
    playthrough_id: str = "default",
    speaker: str = "朱比華",
    ts: str = "2026-06-10T10:00:00",
) -> StoryLine:
    return StoryLine(
        line_id=line_id,
        session_id=session_id,
        game_id=game_id,
        text=text,
        timestamp=ts,
        playthrough_id=playthrough_id,
        speaker=speaker,
        source="ocr",
        confidence=0.0,
        raw_hash="",
        status=status,
    )


def _ctx(request: TurnRequest, prompt: str = BASE_PROMPT) -> TurnContext:
    ctx = TurnContext(request)
    ctx.prompt = PromptBundle(prompt_input=prompt)
    return ctx


def _deps(ctx, *, game_memory=None, character_id="spica", user_id="麦"):
    return TurnDeps(
        config=AppConfig(character=CharacterConfig(character_id=character_id, interlocutor_name=user_id)),
        llm=None,
        tts=None,
        visual=None,
        memory=None,
        tools=RegistryToolSet.from_function_table([], {}),
        game_memory=game_memory,
        observer=DefaultTurnObserver(ctx.timing),
    )


class CurrentLineTimingPinTest(unittest.TestCase):
    """① The two readers the stage uses -- current_pending_story_line (PENDING_CURRENT)
    and unsummarized_committed_story_lines (COMMITTED) -- partition lines by status:
    a line is in EXACTLY one of them, never both (no double-injection), never lost."""

    def test_three_states_each_consistent(self):
        with TemporaryDirectory() as tmp:
            gm = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
            S = "S1"

            def pending():
                return gm.current_pending_story_line("ABC", "default", S)

            def buffer_ids():
                return {l.line_id for l in gm.unsummarized_committed_story_lines("ABC", "default")}

            # --- State A (事务①之前): old line L1 still PENDING_CURRENT (on screen now).
            gm.add_story_line(_line("L1", S, "第一句", StoryLineStatus.PENDING_CURRENT, ts="2026-06-10T10:00:01"))
            self.assertIsNotNone(pending())
            self.assertEqual(pending().line_id, "L1")  # CURRENT_LINE = L1
            self.assertEqual(pending().text, "第一句")
            self.assertNotIn("L1", buffer_ids())  # pending NOT in the committed buffer

            # --- State B (事务①后/②前, the zero-row gap): L1 committed, no pending yet.
            gm.update_story_line_status("L1", StoryLineStatus.COMMITTED)
            self.assertIsNone(pending())  # 0 PENDING_CURRENT rows -> [CURRENT_LINE] omitted
            self.assertIn("L1", buffer_ids())  # ...but L1 is now in the buffer -> NO loss

            # --- State C (事务②后): new line L2 PENDING_CURRENT; L1 stays committed.
            gm.add_story_line(_line("L2", S, "第二句", StoryLineStatus.PENDING_CURRENT, ts="2026-06-10T10:00:02"))
            self.assertEqual(pending().line_id, "L2")  # CURRENT_LINE = L2
            self.assertEqual(pending().text, "第二句")
            self.assertEqual(buffer_ids(), {"L1"})  # buffer still exactly L1
            self.assertNotIn("L2", buffer_ids())  # no double-injection: L2 not in buffer


class CurrentLineSessionScopePinTest(unittest.TestCase):
    """② Crash-residue isolation: the read is scoped by session_id, NOT just
    (game_id, playthrough_id), so an orphaned PENDING_CURRENT row left by a crashed
    (now-ended) session can never be mistaken for the new session's current line."""

    def test_crash_residue_pending_never_matches_new_session(self):
        with TemporaryDirectory() as tmp:
            gm = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
            # A crashed session left this PENDING_CURRENT row in the DB forever
            # (dangling recovery summarizes committed lines but never touches it).
            gm.add_story_line(_line("OLD", "S_crashed", "崩溃残留的旧行", StoryLineStatus.PENDING_CURRENT))

            # The new live session cannot see it.
            self.assertIsNone(gm.current_pending_story_line("ABC", "default", "S_new"))
            # The manual/debug path (session_id is None) reads nothing either.
            self.assertIsNone(gm.current_pending_story_line("ABC", "default", None))
            # Proof it is session-SCOPING, not the row vanishing: its OWN session sees it.
            self.assertEqual(
                gm.current_pending_story_line("ABC", "default", "S_crashed").line_id, "OLD"
            )

            # The new session writes its own pending -> returns ITS line, never OLD.
            gm.add_story_line(_line("NEW", "S_new", "新会话的当前句", StoryLineStatus.PENDING_CURRENT))
            self.assertEqual(gm.current_pending_story_line("ABC", "default", "S_new").line_id, "NEW")


class CurrentLineInjectionPinTest(unittest.TestCase):
    """③ The gated stage injects [CURRENT_LINE] on an ACTIVE companion turn and only
    then; offline never injects it; crash-residue never leaks through the node."""

    def test_active_injects_current_line(self):
        with TemporaryDirectory() as tmp:
            gm = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
            gm.add_story_line(
                _line("L1", "S1", "現在画面に出てるこの台詞", StoryLineStatus.PENDING_CURRENT)
            )
            req = TurnRequest(
                user_input="现在这句什么意思",
                interaction_mode="galgame",
                conversation_id="default",
                game_context_request=GameContextRequest(mode="active", game_id="ABC", session_id="S1"),
            )
            ctx = _ctx(req)
            retrieve_game_context_node(ctx, None, _deps(ctx, game_memory=gm))
            prompt = ctx.prompt.prompt_input
            self.assertIn("[CURRENT_LINE]", prompt)
            self.assertIn("現在画面に出てるこの台詞", prompt)

    def test_offline_never_injects_current_line(self):
        with TemporaryDirectory() as tmp:
            gm = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
            # Progress data so the offline branch DOES inject something (proves the
            # node ran the offline path), while [CURRENT_LINE] is deliberately skipped.
            ManualGameMemory(gm, character_id="spica", user_id="麦").manual_set_progress_state(
                "ABC", current_scene_summary="教室"
            )
            gm.add_story_line(_line("L1", "S1", "ペンディング行", StoryLineStatus.PENDING_CURRENT))
            req = TurnRequest(
                user_input="昨天玩到哪了",
                conversation_id="default",
                command_intent="ask_last_progress",
                # even with a session_id present, offline must not inject CURRENT_LINE
                game_context_request=GameContextRequest(mode="offline", game_id="ABC", session_id="S1"),
            )
            ctx = _ctx(req)
            retrieve_game_context_node(ctx, None, _deps(ctx, game_memory=gm))
            prompt = ctx.prompt.prompt_input
            self.assertIn("[GAME_PROGRESS]", prompt)  # offline branch ran
            self.assertNotIn("[CURRENT_LINE]", prompt)  # ...but no live current line
            self.assertNotIn("ペンディング行", prompt)

    def test_active_with_only_crash_residue_injects_nothing(self):
        # Active turn for the new session while ONLY a crash-residue pending row
        # exists -> [CURRENT_LINE] must not leak the stale line end-to-end.
        with TemporaryDirectory() as tmp:
            gm = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
            gm.add_story_line(_line("OLD", "S_crashed", "崩溃残留", StoryLineStatus.PENDING_CURRENT))
            req = TurnRequest(
                user_input="现在这句什么意思",
                interaction_mode="galgame",
                conversation_id="default",
                game_context_request=GameContextRequest(mode="active", game_id="ABC", session_id="S_new"),
            )
            ctx = _ctx(req)
            retrieve_game_context_node(ctx, None, _deps(ctx, game_memory=gm))
            prompt = ctx.prompt.prompt_input
            self.assertNotIn("[CURRENT_LINE]", prompt)
            self.assertNotIn("崩溃残留", prompt)


if __name__ == "__main__":
    unittest.main()
