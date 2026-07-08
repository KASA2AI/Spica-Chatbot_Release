"""Phase 5: pure title scoring (unique/multiple/none + no-keyword force-pick +
aux-signals-only-tiebreak) and the locator's structured degradation."""

import unittest

from spica.adapters.window_locator.linux_x11 import LinuxX11WindowLocator, _WmctrlError
from spica.galgame.models import WindowMatchRule
from spica.galgame.window_match import WindowMatchOutcome, classify, score_candidates
from spica.ports.window_locator import WindowCandidate


def _cand(window_id, title, **kw):
    return WindowCandidate(window_id=window_id, title=title, **kw)


class ScoreTest(unittest.TestCase):
    def test_unique(self):
        scored = score_candidates(
            [_cand("0x1", "シナリオ - MyGame"), _cand("0x2", "Firefox")],
            WindowMatchRule(title_keywords=["MyGame"]),
        )
        self.assertEqual([s.candidate.window_id for s in scored], ["0x1"])
        self.assertEqual(classify(scored), WindowMatchOutcome.UNIQUE)

    def test_multiple(self):
        scored = score_candidates(
            [_cand("0x1", "MyGame ch1"), _cand("0x2", "MyGame config"), _cand("0x3", "Editor")],
            WindowMatchRule(title_keywords=["MyGame"]),
        )
        self.assertEqual({s.candidate.window_id for s in scored}, {"0x1", "0x2"})
        self.assertEqual(classify(scored), WindowMatchOutcome.MULTIPLE)

    def test_none(self):
        scored = score_candidates(
            [_cand("0x1", "Firefox"), _cand("0x2", "Editor")],
            WindowMatchRule(title_keywords=["MyGame"]),
        )
        self.assertEqual(scored, [])
        self.assertEqual(classify(scored), WindowMatchOutcome.NONE)

    def test_no_keywords_forces_explicit_pick(self):
        scored = score_candidates(
            [_cand("0x1", "A"), _cand("0x2", "B")], WindowMatchRule(title_keywords=[])
        )
        self.assertEqual(len(scored), 2)  # everything qualifies
        self.assertEqual(classify(scored), WindowMatchOutcome.MULTIPLE)  # -> forced pick

    def test_aux_signals_only_tiebreak_never_promote(self):
        rule = WindowMatchRule(title_keywords=["MyGame"], app_id="mygame.exe")
        scored = score_candidates(
            [_cand("0x1", "MyGame"), _cand("0x2", "MyGame", app_id="mygame.exe")], rule
        )
        self.assertEqual(scored[0].candidate.window_id, "0x2")  # app_id breaks the tie
        self.assertEqual(len(scored), 2)
        # a window matching app_id but NOT a title keyword is still excluded
        self.assertEqual(score_candidates([_cand("0x3", "Firefox", app_id="mygame.exe")], rule), [])


WMCTRL_OUT = (
    "0x03000007  0 1234   Navigator.firefox   host Mozilla Firefox\n"
    "0x05000003  0 5678   mygame.exe.mygame   host シナリオ - My Game\n"
)


class LocatorEnumerationTest(unittest.TestCase):
    def test_parse_wmctrl_output(self):
        enum = LinuxX11WindowLocator(runner=lambda cmd: WMCTRL_OUT).enumerate_windows()
        self.assertTrue(enum.available)
        self.assertEqual([w.window_id for w in enum.windows], ["0x03000007", "0x05000003"])
        self.assertEqual(enum.windows[1].title, "シナリオ - My Game")
        self.assertEqual(enum.windows[1].app_id, "mygame.exe.mygame")
        self.assertEqual(enum.windows[1].pid, 5678)

    def test_wmctrl_missing(self):
        def runner(cmd):
            raise FileNotFoundError("wmctrl")

        enum = LinuxX11WindowLocator(runner=runner).enumerate_windows()
        self.assertFalse(enum.available)
        self.assertEqual(enum.reason_code, "WMCTRL_MISSING")
        self.assertIn("wmctrl", enum.reason)
        self.assertEqual(enum.windows, [])  # empty list, NOT a raised exception

    def test_wayland_unsupported(self):
        def runner(cmd):
            raise _WmctrlError("Cannot open display")

        enum = LinuxX11WindowLocator(runner=runner, wayland_probe=lambda: True).enumerate_windows()
        self.assertFalse(enum.available)
        self.assertEqual(enum.reason_code, "WAYLAND_UNSUPPORTED")
        self.assertIn("Wayland", enum.reason)

    def test_enumeration_failed_non_wayland(self):
        def runner(cmd):
            raise _WmctrlError("boom")

        enum = LinuxX11WindowLocator(runner=runner, wayland_probe=lambda: False).enumerate_windows()
        self.assertFalse(enum.available)
        self.assertEqual(enum.reason_code, "ENUMERATION_FAILED")
        self.assertIn("boom", enum.reason)


WMCTRL_G_OUT = (
    "0x03000007  0 0 0 800 600 host Firefox\n"
    "0x05000003  0 100 200 1280 720 host My Game\n"
)


class GeometryTest(unittest.TestCase):
    def test_get_window_geometry_parsed(self):
        geom = LinuxX11WindowLocator(runner=lambda cmd: WMCTRL_G_OUT).get_window_geometry("0x05000003")
        self.assertEqual((geom.x, geom.y, geom.width, geom.height), (100, 200, 1280, 720))

    def test_get_window_geometry_unknown_window(self):
        self.assertIsNone(LinuxX11WindowLocator(runner=lambda cmd: WMCTRL_G_OUT).get_window_geometry("0xdead"))

    def test_get_window_geometry_wmctrl_missing(self):
        def runner(cmd):
            raise FileNotFoundError()

        self.assertIsNone(LinuxX11WindowLocator(runner=runner).get_window_geometry("0x1"))


_GEOM_LINE = "0x0c200001  0 0 0 1280 720 host anemoi\n"
_RULE = WindowMatchRule(title_keywords=["anemoi"])


class CheckSafetyTest(unittest.TestCase):
    """Focus is judged by the ACTIVE window's TITLE vs keywords (§17.3), never by
    window-id equality."""

    def _loc(self, *, geom=_GEOM_LINE, active="0x0c200001", title="anemoi Day 1", minimized=False):
        return LinuxX11WindowLocator(
            runner=lambda cmd: geom,
            active_window_probe=lambda: active,
            window_title_probe=lambda window_id: title,
            minimized_probe=lambda window_id: minimized,
        )

    def test_window_gone(self):
        self.assertEqual(self._loc(geom="").check_safety("0x0c200001", _RULE).reason_code, "WINDOW_GONE")

    def test_window_minimized(self):
        self.assertEqual(self._loc(minimized=True).check_safety("0x0c200001", _RULE).reason_code, "WINDOW_MINIMIZED")

    def test_safety_probe_failed_when_active_unknown(self):
        self.assertEqual(self._loc(active=None).check_safety("0x0c200001", _RULE).reason_code, "SAFETY_PROBE_FAILED")

    def test_inner_render_window_focused_is_safe_REGRESSION(self):
        # The real anemoi bug: bound to the OUTER window id, but the focused window is
        # the INNER render window with a DIFFERENT id -- yet its title still contains
        # the keyword, so it must read SAFE (not WINDOW_NOT_FOCUSED).
        loc = self._loc(active="0x0fa00001", title="anemoi gemini-3.1-pro 机翻 by jyxjyx1234 Ｄａｙ　１")
        result = loc.check_safety("0x0c200001", _RULE)  # bound id != active id
        self.assertTrue(result.ok)

    def test_other_app_focused_is_not_focused(self):
        # A genuinely different app (title misses the keyword) still pauses ("绝不误截").
        loc = self._loc(active="0x0fa00001", title="Mozilla Firefox")
        self.assertEqual(loc.check_safety("0x0c200001", _RULE).reason_code, "WINDOW_NOT_FOCUSED")

    def test_overlay_focus_is_exempt(self):
        # focus on the Spica overlay (own window, id reliable) must NOT pause (§7.4)
        loc = self._loc(active="0x9", title="Spica")
        self.assertTrue(loc.check_safety("0x0c200001", _RULE, overlay_window_id="0x9").ok)

    def test_no_keywords_is_conservatively_not_focused(self):
        # Without keywords focus can't be verified -> stay paused (never relax §7.1).
        loc = self._loc(title="anemoi Day 1")
        self.assertEqual(
            loc.check_safety("0x0c200001", WindowMatchRule(title_keywords=[])).reason_code, "WINDOW_NOT_FOCUSED"
        )


if __name__ == "__main__":
    unittest.main()
