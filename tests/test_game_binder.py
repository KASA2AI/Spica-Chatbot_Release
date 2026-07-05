"""Phase 5: GameBinder flow -- launch/manual, candidate cases, launch-fail branches,
enumeration-unavailable, bind success (stores WindowMatchRule + -> game_launched),
and bind_game rejection surfaced (not crashed)."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.galgame.binding import GameBinder
from spica.galgame.models import GameProfile, LaunchProfile, WindowMatchRule, utc_now_iso
from spica.galgame.session import GalgameCompanionSession, GalgameState
from spica.ports.game_launcher import LaunchResult
from spica.ports.window_locator import WindowCandidate, WindowEnumeration


class _FakeLauncher:
    def __init__(self, result=None):
        self.result = result or LaunchResult(ok=True, pid=1)
        self.launched = []

    def scan_desktop_entries(self):
        return []

    def launch(self, profile):
        self.launched.append(profile)
        return self.result


class _FakeLocator:
    def __init__(self, enumeration):
        self.enumeration = enumeration

    def enumerate_windows(self):
        return self.enumeration


class _Sink:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)

    def last(self, kind):
        matches = [e for e in self.events if e.kind == kind]
        return matches[-1] if matches else None


def _enum(*candidates):
    return WindowEnumeration(windows=list(candidates), available=True)


def _cand(window_id, title, **kw):
    return WindowCandidate(window_id=window_id, title=title, **kw)


class BinderTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mem = GameMemorySqliteAdapter(Path(self._tmp.name) / "g.sqlite3")
        self.sink = _Sink()
        self.session = GalgameCompanionSession(self.mem, emit=self.sink)
        now = utc_now_iso()
        self.mem.upsert_game_profile(
            GameProfile(
                game_id="ABC", display_name="My Game", created_at=now, updated_at=now,
                window_match=WindowMatchRule(title_keywords=["My Game"]).to_dict(),
            )
        )

    def _binder(self, *, launcher=None, locator):
        return GameBinder(launcher or _FakeLauncher(), locator, self.mem, self.session, emit=self.sink)


class BindSuccessTest(BinderTestBase):
    def test_unique_confirm_then_bind_stores_rule_and_game_launched(self):
        binder = self._binder(locator=_FakeLocator(_enum(_cand("0x1", "シナリオ - My Game", app_id="mygame"))))
        binder.begin_bind("ABC", LaunchProfile(launch_type="command", command="run"))
        candidates_ev = self.sink.last("galgame_window_candidates")
        self.assertEqual(candidates_ev.mode, "confirm")  # unique still confirms
        self.assertEqual([c["window_id"] for c in candidates_ev.candidates], ["0x1"])

        binder.resolve_selection("0x1")
        self.assertEqual(self.session.state, GalgameState.GAME_LAUNCHED)
        self.assertEqual(self.session.game_id, "ABC")
        self.assertEqual(self.sink.last("galgame_game_bound").window_id, "0x1")

        rule = WindowMatchRule.from_dict(self.mem.get_game_profile("ABC").window_match)
        self.assertEqual(rule.last_full_title, "シナリオ - My Game")  # historical only
        self.assertEqual(rule.title_keywords, ["My Game"])  # match key unchanged by title
        self.assertTrue(rule.confirmed_once)
        self.assertIn("active", self.mem.get_game_profile("ABC").launch_profiles)

    def test_multiple_pick(self):
        binder = self._binder(locator=_FakeLocator(_enum(_cand("0x1", "My Game ch1"), _cand("0x2", "My Game cfg"))))
        binder.begin_bind("ABC", LaunchProfile(launch_type="command", command="run"))
        self.assertEqual(self.sink.last("galgame_window_candidates").mode, "pick")
        binder.resolve_selection("0x2")
        self.assertEqual(self.session.state, GalgameState.GAME_LAUNCHED)

    def test_manual_bind_skips_launch(self):
        launcher = _FakeLauncher()
        binder = GameBinder(launcher, _FakeLocator(_enum(_cand("0x1", "My Game"))), self.mem, self.session, emit=self.sink)
        binder.begin_bind("ABC", manual=True)
        self.assertEqual(launcher.launched, [])  # nothing launched
        self.assertEqual(self.sink.last("galgame_window_candidates").mode, "confirm")
        binder.resolve_selection("0x1")
        self.assertEqual(self.session.state, GalgameState.GAME_LAUNCHED)


class SelectionOnlyModeTest(BinderTestBase):
    """Stage 3: session=None = selection/persistence-only mode (the companion
    controller's own start() binds afterwards), and game_id_override supports the
    first-time flow where the game_id can only be guessed FROM the picked title."""

    def test_session_none_persists_and_emits_without_bind(self):
        binder = GameBinder(
            _FakeLauncher(), _FakeLocator(_enum(_cand("0x1", "シナリオ - My Game"))),
            self.mem, session=None, emit=self.sink,
        )
        binder.begin_bind("ABC", manual=True)
        binder.resolve_selection("0x1")
        bound = self.sink.last("galgame_game_bound")
        self.assertEqual((bound.game_id, bound.window_id), ("ABC", "0x1"))  # event still emitted
        rule = WindowMatchRule.from_dict(self.mem.get_game_profile("ABC").window_match)
        self.assertTrue(rule.confirmed_once)  # binding persisted
        self.assertEqual(rule.last_full_title, "シナリオ - My Game")
        self.assertEqual(self.session.state, GalgameState.IDLE)  # session UNTOUCHED (skip bind_game)

    def test_game_id_override_first_time_flow(self):
        binder = GameBinder(
            _FakeLauncher(),
            _FakeLocator(_enum(_cand("0x1", "LimeLight Lemonade Jam"), _cand("0x2", "Firefox"))),
            self.mem, session=None, emit=self.sink,
        )
        binder.begin_bind("", manual=True)  # unknown game: empty rule -> ALL qualify
        candidates_ev = self.sink.last("galgame_window_candidates")
        self.assertEqual(candidates_ev.mode, "pick")  # forced pick, never auto-guess (§17.3)
        self.assertEqual(len(candidates_ev.candidates), 2)
        binder.resolve_selection("0x1", game_id_override="limelight")
        bound = self.sink.last("galgame_game_bound")
        self.assertEqual(bound.game_id, "limelight")  # the override won
        self.assertIsNotNone(self.mem.get_game_profile("limelight"))  # persisted under it

    def test_missing_game_id_fails_readably(self):
        binder = GameBinder(
            _FakeLauncher(), _FakeLocator(_enum(_cand("0x1", "Some Window"))),
            self.mem, session=None, emit=self.sink,
        )
        binder.begin_bind("", manual=True)
        binder.resolve_selection("0x1")  # no override, begin had no game_id either
        self.assertEqual(self.sink.last("galgame_bind_failed").code, "NO_GAME_ID")


class BindFailureTest(BinderTestBase):
    def test_no_window(self):
        binder = self._binder(locator=_FakeLocator(_enum(_cand("0x1", "Firefox"))))  # no keyword hit
        binder.begin_bind("ABC", LaunchProfile(launch_type="command", command="run"))
        ev = self.sink.last("galgame_bind_failed")
        self.assertEqual(ev.code, "NO_WINDOW")
        self.assertIn("manual_bind", ev.options)
        self.assertEqual(self.session.state, GalgameState.IDLE)  # not bound

    def test_launch_failure_offers_three_branches(self):
        launcher = _FakeLauncher(LaunchResult(ok=False, error="bottles missing"))
        binder = self._binder(launcher=launcher, locator=_FakeLocator(_enum()))
        binder.begin_bind("ABC", LaunchProfile(launch_type="command", command="run"))
        ev = self.sink.last("galgame_bind_failed")
        self.assertEqual(ev.code, "LAUNCH_FAILED")
        self.assertEqual(ev.options, ["rechoose_launch", "manual_bind", "cancel"])

        # branch (manual_bind after failure): a fresh binder, manual bind, works
        binder2 = GameBinder(launcher, _FakeLocator(_enum(_cand("0x1", "My Game"))), self.mem, self.session, emit=self.sink)
        binder2.begin_bind("ABC", manual=True)
        self.assertEqual(self.sink.last("galgame_window_candidates").mode, "confirm")
        # branch (cancel): cancel clears pending -> resolve then is a no-op failure
        binder2.cancel_bind()
        binder2.resolve_selection("0x1")
        self.assertEqual(self.sink.last("galgame_bind_failed").code, "NO_PENDING_BIND")

    def test_wmctrl_missing_unavailable_readable(self):
        loc = _FakeLocator(WindowEnumeration(windows=[], available=False, reason_code="WMCTRL_MISSING", reason="请安装 wmctrl"))
        binder = self._binder(locator=loc)
        binder.begin_bind("ABC", LaunchProfile(launch_type="command", command="run"))
        ev = self.sink.last("galgame_bind_failed")
        self.assertEqual(ev.code, "WMCTRL_MISSING")
        self.assertEqual(ev.options, ["install_wmctrl", "cancel"])
        self.assertIn("wmctrl", ev.reason)

    def test_wayland_unavailable_readable(self):
        loc = _FakeLocator(WindowEnumeration(windows=[], available=False, reason_code="WAYLAND_UNSUPPORTED", reason="Wayland 暂不支持"))
        binder = self._binder(locator=loc)
        binder.begin_bind("ABC", LaunchProfile(launch_type="command", command="run"))
        ev = self.sink.last("galgame_bind_failed")
        self.assertEqual(ev.code, "WAYLAND_UNSUPPORTED")
        self.assertEqual(ev.options, ["cancel"])

    def test_win32_unavailable_readable(self):
        # W2: the windows lane without user32 (e.g. mis-selected on a non-Windows
        # host) -- no enumeration means no manual-bind either, so cancel-only.
        loc = _FakeLocator(WindowEnumeration(windows=[], available=False, reason_code="WIN32_UNAVAILABLE", reason="Win32 窗口 API 不可用"))
        binder = self._binder(locator=loc)
        binder.begin_bind("ABC", LaunchProfile(launch_type="command", command="run"))
        ev = self.sink.last("galgame_bind_failed")
        self.assertEqual(ev.code, "WIN32_UNAVAILABLE")
        self.assertEqual(ev.options, ["cancel"])

    def test_bind_game_rejection_surfaces_not_crashes(self):
        # G3: session already past idle -> bind_game raises GalgameStateError -> caught.
        self.session.bind_game("XYZ")  # session now game_launched
        binder = self._binder(locator=_FakeLocator(_enum(_cand("0x1", "My Game"))))
        binder.begin_bind("ABC", LaunchProfile(launch_type="command", command="run"))
        binder.resolve_selection("0x1")  # must NOT raise
        self.assertEqual(self.sink.last("galgame_bind_failed").code, "SESSION_NOT_BINDABLE")
        self.assertEqual(self.session.state, GalgameState.GAME_LAUNCHED)  # unchanged, no crash


if __name__ == "__main__":
    unittest.main()
