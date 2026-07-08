"""Phase 4: GalgameCompanionSession FSM + projection + event channel.

Locks the two red lines: (1) the session is the single state owner -- one
_transition entry, illegal edges raise GalgameStateError, no external state setter;
(2) the event channel is per-turn-INDEPENDENT (events flow with no run_turn/turn
context at all). Plus FSM->PlaySession projection (incl. error->crashed,
end->ended+ended_at), best-effort projection self-heal, and no-op sink default.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.galgame.models import ChoiceEvent
from spica.galgame.session import (
    ALLOWED_TRANSITIONS,
    GalgameCompanionSession,
    GalgameState,
    GalgameStateError,
)


class _CollectingSink:
    def __init__(self) -> None:
        self.events = []

    def __call__(self, event) -> None:
        self.events.append(event)

    @property
    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def of(self, kind: str) -> list:
        return [e for e in self.events if e.kind == kind]


class _FlakyGameMemory:
    """Delegates to a real adapter but can be toggled to fail update_play_session."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.fail_update = False

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def update_play_session(self, session_id, **fields):
        if self.fail_update:
            raise RuntimeError("db down")
        return self._inner.update_play_session(session_id, **fields)


class SessionTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mem = GameMemorySqliteAdapter(Path(self._tmp.name) / "g.sqlite3")
        self.sink = _CollectingSink()

    def _session(self, mem=None, emit="default"):
        return GalgameCompanionSession(
            mem or self.mem,
            emit=self.sink if emit == "default" else emit,
            character_id="spica",
            user_id="麦",
        )


class FsmTransitionTest(SessionTestBase):
    def test_full_legal_path_touches_every_state(self):
        s = self._session()
        self.assertEqual(s.state, GalgameState.IDLE)
        s.bind_game("ABC")
        self.assertEqual(s.state, GalgameState.GAME_LAUNCHED)
        s.start(needs_calibration=True)
        self.assertEqual(s.state, GalgameState.CALIBRATING)
        s.finish_calibration()
        self.assertEqual(s.state, GalgameState.PLAYING)
        s.pause()
        self.assertEqual(s.state, GalgameState.PAUSED)
        s.resume()
        self.assertEqual(s.state, GalgameState.PLAYING)
        s.on_window_lost("occluded")
        self.assertEqual(s.state, GalgameState.WINDOW_LOST)
        s.on_window_recovered()
        self.assertEqual(s.state, GalgameState.PLAYING)
        s.begin_choice_check()
        self.assertEqual(s.state, GalgameState.CHOICE_CHECKING)
        s.on_choice_detected(ChoiceEvent(choice_id="C1", game_id="ABC", options=[{"index": 1, "text": "a"}]))
        self.assertEqual(s.state, GalgameState.PLAYING)
        s.begin_background_summary()
        self.assertEqual(s.state, GalgameState.BACKGROUND_SUMMARIZING)
        s.on_summary_finished("SM1")
        self.assertEqual(s.state, GalgameState.PLAYING)
        s.end()  # playing -> summarizing -> ending -> game_launched
        self.assertEqual(s.state, GalgameState.GAME_LAUNCHED)

    def test_illegal_transitions_raise(self):
        s = self._session()
        for call in (s.pause, s.resume, s.end):  # nothing started yet (idle)
            with self.subTest(call=call.__name__):
                with self.assertRaises(GalgameStateError):
                    call()
        s.bind_game("ABC")
        s.start()  # playing
        with self.assertRaises(GalgameStateError):
            s.start()  # playing -> playing illegal
        with self.assertRaises(GalgameStateError):
            s.on_window_recovered()  # not window_lost
        with self.assertRaises(GalgameStateError):
            s.on_summary_finished()  # not background_summarizing
        with self.assertRaises(GalgameStateError):
            s.on_choice_detected(ChoiceEvent(choice_id="C", game_id="ABC"))  # not choice_checking

    def test_illegal_transition_does_not_mutate_state(self):
        s = self._session()
        with self.assertRaises(GalgameStateError):
            s.pause()
        self.assertEqual(s.state, GalgameState.IDLE)  # untouched
        self.assertEqual(self.sink.events, [])  # no event emitted on a rejected call

    def test_transition_table_only_legal_edges(self):
        # The table is the single source of truth for legality.
        self.assertNotIn(GalgameState.PLAYING, ALLOWED_TRANSITIONS[GalgameState.IDLE])
        self.assertIn(GalgameState.GAME_LAUNCHED, ALLOWED_TRANSITIONS[GalgameState.IDLE])
        self.assertIn(GalgameState.ENDING, ALLOWED_TRANSITIONS[GalgameState.SUMMARIZING])


class NoBypassTest(SessionTestBase):
    def test_state_is_read_only_no_setter(self):
        s = self._session()
        with self.assertRaises(AttributeError):
            s.state = GalgameState.PLAYING  # type: ignore[misc]


class PlaySessionProjectionTest(SessionTestBase):
    def test_mapping_each_state_persisted(self):
        s = self._session()
        s.bind_game("ABC")
        sid = s.start()  # playing -> active
        self.assertEqual(self.mem.get_play_session(sid).state, "active")
        s.pause()
        self.assertEqual(self.mem.get_play_session(sid).state, "paused")
        s.resume()
        self.assertEqual(self.mem.get_play_session(sid).state, "active")
        s.on_window_lost()
        self.assertEqual(self.mem.get_play_session(sid).state, "paused")  # window_lost -> paused
        s.on_window_recovered()
        s.begin_choice_check()
        self.assertEqual(self.mem.get_play_session(sid).state, "active")  # choice_checking -> active
        s.on_choice_detected(ChoiceEvent(choice_id="C1", game_id="ABC"))
        s.begin_background_summary()
        self.assertEqual(self.mem.get_play_session(sid).state, "active")
        s.on_summary_finished()
        s.end()
        ps = self.mem.get_play_session(sid)
        self.assertEqual(ps.state, "ended")
        self.assertTrue(ps.ended_at)

    def test_error_maps_to_crashed(self):
        s = self._session()
        s.bind_game("ABC")
        sid = s.start()
        s.mark_error("boom")
        self.assertEqual(s.state, GalgameState.ERROR)
        self.assertEqual(self.mem.get_play_session(sid).state, "crashed")


class EventChannelTest(SessionTestBase):
    def test_events_delivered_with_no_turn_lifecycle(self):
        # RED LINE: there is NO TurnContext / run_turn / ChatWorker anywhere in this
        # test. The session emits purely from its own background-style lifecycle.
        s = self._session()
        s.bind_game("ABC")
        s.start()
        self.sink.events.clear()
        s.on_window_lost("occluded")  # pure background trigger, zero turn context
        self.assertIn("galgame_window_lost", self.sink.kinds)
        self.assertIn("galgame_status_changed", self.sink.kinds)
        lost = self.sink.of("galgame_window_lost")[0]
        self.assertEqual(lost.reason, "occluded")
        status = self.sink.of("galgame_status_changed")[-1]
        self.assertEqual(status.state, "window_lost")
        self.assertEqual(status.previous, "playing")


class NoopSinkTest(SessionTestBase):
    def test_runs_headless_without_a_sink(self):
        s = GalgameCompanionSession(self.mem)  # no emit -> noop default, no UI
        s.bind_game("ABC")
        sid = s.start()
        s.pause()
        s.resume()
        s.begin_choice_check()
        s.on_choice_detected(ChoiceEvent(choice_id="C", game_id="ABC"))
        s.end()
        self.assertEqual(s.state, GalgameState.GAME_LAUNCHED)
        self.assertEqual(self.mem.get_play_session(sid).state, "ended")


class ProjectionSelfHealTest(SessionTestBase):
    def test_projection_failure_dirty_with_context_then_self_heals(self):
        flaky = _FlakyGameMemory(self.mem)
        s = self._session(mem=flaky)
        s.bind_game("ABC")
        sid = s.start()  # add_play_session succeeds -> active
        self.assertFalse(s.is_projection_dirty)

        # projection fails on pause: FSM still advances; DB drifts (stays active).
        flaky.fail_update = True
        self.sink.events.clear()
        s.pause()
        self.assertEqual(s.state, GalgameState.PAUSED)  # owner truth advanced anyway
        self.assertTrue(s.is_projection_dirty)
        self.assertEqual(flaky.get_play_session(sid).state, "active")  # DB behind (drifted)
        errors = self.sink.of("galgame_error")
        self.assertTrue(errors)
        self.assertEqual(errors[0].session_id, sid)  # context: session_id
        self.assertEqual(errors[0].target_state, "paused")  # context: target state
        self.assertIn("projection failed", errors[0].message)  # context: reason

        # recovery: writes work again; end() must drive the DB to ended -- NOT leave
        # it stuck at "active" (which would look like crashed dangling on restart).
        flaky.fail_update = False
        s.end()
        self.assertFalse(s.is_projection_dirty)
        ps = flaky.get_play_session(sid)
        self.assertEqual(ps.state, "ended")  # self-healed to current truth
        self.assertTrue(ps.ended_at)


if __name__ == "__main__":
    unittest.main()
