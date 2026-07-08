"""Path B stage 1: GalgameCompanionController -- assembles the verified parts and
persists through the INJECTED game-memory adapter; stop is safe + idempotent;
start failure leaves no dangling, controller re-startable.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.galgame.companion_controller import (
    GalgameCompanionController,
    GalgameCompanionError,
    guess_game_id_from_title,
)
from spica.galgame.models import GameProfile, OCRProfile, OCRRegion, utc_now_iso
from spica.galgame.ocr_loop import OcrStreamRunner
from spica.galgame.session import GalgameState
from spica.galgame.summarizer import SummaryResult
from spica.ports.ocr import OcrResult
from spica.ports.screen_capture import CaptureImage
from spica.ports.window_locator import WindowGeometry, WindowSafetyResult


class _Locator:
    def get_window_geometry(self, window_id):
        return WindowGeometry(0, 0, 100, 100)

    def check_safety(self, window_id, rule, overlay_window_id=None):
        return WindowSafetyResult(ok=True)


class _Capture:
    def capture_rect(self, left, top, width, height):
        img = Image.new("RGB", (max(1, width), max(1, height)), (30, 30, 30))
        return CaptureImage(image=img, width=img.width, height=img.height)


class _InertOcr:
    """Returns empty text, so the background OCR loop never commits on its own --
    the test drives committed lines explicitly via controller.session."""

    def recognize(self, image):
        return OcrResult(text="")


class _StubSummarizer:
    def summarize(self, lines, *, recent_summaries=None, progress=None):
        return SummaryResult(summary_zh="stub summary", characters=["麦"])


class _Sink:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)


class GuessGameIdTest(unittest.TestCase):
    def test_guesses_leading_word_lowercased(self):
        self.assertEqual(guess_game_id_from_title("LimeLight Lemonade Jam"), "limelight")
        self.assertEqual(guess_game_id_from_title("anemoi gemini-3.1-pro 機翻"), "anemoi")
        self.assertEqual(guess_game_id_from_title("Game.exe foo"), "game")

    def test_empty_returns_blank(self):
        self.assertEqual(guess_game_id_from_title(""), "")
        self.assertEqual(guess_game_id_from_title(None), "")

    def test_cjk_title_uses_leading_name_segment(self):
        # Chinese/Japanese fan-translated galgames: the game name leads and the
        # translator/model/group tags trail. The id is the leading name segment up
        # to the first metadata boundary (latin letter / bracket / |/@), keeping
        # only CJK+kana+digits -- so in-title symbols (★☆！～) do NOT truncate it.
        self.assertEqual(
            guess_game_id_from_title("创作彼女的恋爱方程式-Galgamer@台北办公室/Awaken"),
            "创作彼女的恋爱方程式")
        self.assertEqual(
            guess_game_id_from_title(
                "次元错位恋人!!【claude-4.6-opus v1.4】Made by julixian|花咲夜机翻组"),
            "次元错位恋人")
        # in-title ★ is KEPT (belongs to the name; also keeps the id a substring
        # of the live title -- the focus keyword); only the edge ！ is trimmed.
        self.assertEqual(
            guess_game_id_from_title("绽放★青春全力向前冲！"), "绽放★青春全力向前冲")
        self.assertEqual(guess_game_id_from_title("日本語タイトル"), "日本語タイトル")
        self.assertEqual(guess_game_id_from_title("アイランド"), "アイランド")

    def test_cjk_id_stays_substring_of_title_for_focus(self):
        # The id doubles as the window-focus keyword (companion start ->
        # title_keywords=[id]; ocr_loop verifies focus by `id in live_title`).
        # Regression guard for the "失焦" bug: a CJK id MUST stay a CONTIGUOUS
        # substring of its title, else the OCR loop reports WINDOW_NOT_FOCUSED.
        for title in ("绽放★青春全力向前冲！",
                      "创作彼女的恋爱方程式-Galgamer@台北办公室/Awaken",
                      "次元错位恋人!!【claude-4.6-opus v1.4】Made by julixian",
                      "日本語タイトル", "アイランド"):
            gid = guess_game_id_from_title(title)
            self.assertTrue(gid and gid in title,
                            f"{gid!r} must be a substring of {title!r} (focus keyword)")

    def test_leading_bracket_tag_still_blank(self):
        # minimal fix boundary: a title starting with a bracket tag (neither latin
        # nor CJK) still yields "" -- the caller then supplies an explicit game_id.
        self.assertEqual(guess_game_id_from_title("【汉化】游戏名"), "")


class ControllerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mem = GameMemorySqliteAdapter(Path(self._tmp.name) / "galgame.sqlite3")
        self.sink = _Sink()

    def _controller(self):
        return GalgameCompanionController(
            self.mem, _Capture(), _Locator(), _InertOcr(),
            summarizer=_StubSummarizer(), emit=self.sink,
            summary_trigger_chars=100000,  # no auto-trigger; end() does the summary
            interval_seconds=60.0,  # loop barely cycles (stop interrupts the wait)
        )


class IntervalForwardingTest(ControllerTestBase):
    """The interval-drop regression: controller must forward the sampling interval to
    the OcrStreamRunner it builds (the bug was it silently used the 1.0 default)."""

    def _controller_with(self, interval):
        return GalgameCompanionController(
            self.mem, _Capture(), _Locator(), _InertOcr(),
            summarizer=_StubSummarizer(), emit=self.sink, interval_seconds=interval,
        )

    def test_start_interval_override_reaches_runner(self):
        c = self._controller_with(5.0)  # distinct construct default
        c.start("0x1", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0), interval_seconds=0.2)
        self.assertEqual(c._runner._interval, 0.2)  # override won (not 5.0, not the old 1.0)
        c.stop()

    def test_start_without_interval_uses_controller_default(self):
        c = self._controller_with(0.3)
        c.start("0x1", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))  # no interval_seconds
        self.assertEqual(c._runner._interval, 0.3)  # falls back to the construct default
        c.stop()

    def test_start_overlay_window_id_reaches_runner(self):
        # window_lost fix (env 2): start() must forward overlay_window_id to the OCR
        # runner (-> ocr_loop -> check_safety's focus exemption). This is the spica/
        # half of the wiring (the ui/ half is in test_companion_bridge).
        c = self._controller_with(0.3)
        c.start("0x1", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0), overlay_window_id="0x5a00005")
        self.assertEqual(c._runner._overlay_window_id, "0x5a00005")  # reached runner/ocr_loop
        c.stop()

    def test_start_without_overlay_window_id_leaves_runner_none(self):
        c = self._controller_with(0.3)
        c.start("0x1", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))  # no overlay id
        self.assertIsNone(c._runner._overlay_window_id)  # default off -> exemption stays off
        c.stop()

    def test_controller_construct_default_is_point_three(self):
        # No interval_seconds at construction -> 0.3 (the new default, NOT 1.0).
        c = GalgameCompanionController(
            self.mem, _Capture(), _Locator(), _InertOcr(), summarizer=_StubSummarizer(), emit=self.sink
        )
        self.assertEqual(c._interval_seconds, 0.3)


class GalgameConfigDefaultTest(unittest.TestCase):
    def test_ocr_interval_seconds_default_is_point_three(self):
        from spica.config.schema import AppConfig, GalgameConfig

        self.assertEqual(GalgameConfig().ocr_interval_seconds, 0.3)
        self.assertEqual(AppConfig().galgame.ocr_interval_seconds, 0.3)


class RecordHistoryCallbackTest(ControllerTestBase):
    """B 方案 (FINDINGS #15): stop() hands the play-history card to the injected
    recorder AFTER a normal end(); a failing recorder never blocks stop; no play
    -> no call. The controller only produces text -- write authority is the
    injected closure's (铁律 #8)."""

    def _controller_with_recorder(self, recorder):
        return GalgameCompanionController(
            self.mem, _Capture(), _Locator(), _InertOcr(),
            summarizer=_StubSummarizer(), emit=self.sink,
            record_history=recorder,
            summary_trigger_chars=100000, interval_seconds=60.0,
        )

    def test_stop_invokes_recorder_with_card(self):
        records = []
        controller = self._controller_with_recorder(lambda game_id, card: records.append((game_id, card)))
        controller.start("0x1", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
        for text in ["L1", "L1", "L2", "L2"]:
            controller.session.on_ocr_result(text)
        controller.stop()  # end() writes the final summary -> material exists
        self.assertEqual(len(records), 1)
        game_id, card = records[0]
        self.assertEqual(game_id, "g1")
        self.assertIn("一起玩了游戏《g1》", card)  # "游戏" framing
        self.assertIn("最近剧情：", card)

    def test_recorder_failure_does_not_block_stop(self):
        def _explode(game_id, card):
            raise RuntimeError("memory store down")

        controller = self._controller_with_recorder(_explode)
        controller.start("0x1", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
        for text in ["L1", "L1", "L2", "L2"]:
            controller.session.on_ocr_result(text)
        session_id = controller.session.session_id
        controller.stop()  # must NOT raise
        self.assertIsNone(controller.session)  # stop completed normally
        self.assertEqual(self.mem.get_play_session(session_id).state, "ended")  # end() landed

    def test_stop_without_start_makes_no_call(self):
        records = []
        controller = self._controller_with_recorder(lambda game_id, card: records.append((game_id, card)))
        controller.stop()  # never started
        self.assertEqual(records, [])


class ProfileRatiosTest(ControllerTestBase):
    """Stage 3 (debt #8): start() omitted ratios -> read the persisted
    GameProfile.ocr_profile; explicit args always win; uncalibrated fails EARLY
    (before any session exists -> no dangling) and stays restartable."""

    DIALOG = (0.1, 0.7, 0.8, 0.2)
    SPEAKER = (0.1, 0.6, 0.3, 0.08)

    def _calibrate(self, game_id, *, speaker=True):
        now = utc_now_iso()
        ocr = OCRProfile(dialog_text_region=OCRRegion(*self.DIALOG).to_dict())
        if speaker:
            ocr.speaker_name_region = OCRRegion(*self.SPEAKER).to_dict()
        self.mem.upsert_game_profile(GameProfile(
            game_id=game_id, display_name=game_id, created_at=now, updated_at=now,
            ocr_profile=ocr.to_dict(),
        ))

    def test_start_without_ratios_reads_profile(self):
        self._calibrate("g1")
        controller = self._controller()
        controller.start("0x1", game_id="g1")  # NO dialog_ratios passed
        self.assertEqual(controller._runner._dialog_ratios, self.DIALOG)
        self.assertEqual(controller._runner._speaker_ratios, self.SPEAKER)
        controller.stop()

    def test_explicit_ratios_win_over_profile(self):
        self._calibrate("g1")
        controller = self._controller()
        controller.start("0x1", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
        self.assertEqual(controller._runner._dialog_ratios, (0.0, 0.0, 1.0, 1.0))
        self.assertIsNone(controller._runner._speaker_ratios)  # explicit path: no profile fill
        controller.stop()

    def test_uncalibrated_raises_early_no_dangling_and_restartable(self):
        controller = self._controller()
        with self.assertRaises(GalgameCompanionError):
            controller.start("0x1", game_id="g1")  # no profile, no ratios
        self.assertIsNone(controller.session)  # failed BEFORE building a session
        self.assertEqual(self.mem.dangling_play_sessions(), [])  # nothing to clean
        # explicit ratios still start fine afterwards
        controller.start("0x1", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
        self.assertEqual(controller.session.state, GalgameState.PLAYING)
        controller.stop()


class HasCalibratedDialogRegionTest(ControllerTestBase):
    def test_false_without_profile_and_with_degenerate_region(self):
        controller = self._controller()
        self.assertFalse(controller.has_calibrated_dialog_region("nope"))
        # zero-size region = never actually calibrated
        now = utc_now_iso()
        self.mem.upsert_game_profile(GameProfile(
            game_id="g0", display_name="g0", created_at=now, updated_at=now,
            ocr_profile=OCRProfile(dialog_text_region=OCRRegion().to_dict()).to_dict(),
        ))
        self.assertFalse(controller.has_calibrated_dialog_region("g0"))

    def test_true_with_calibrated_region(self):
        now = utc_now_iso()
        self.mem.upsert_game_profile(GameProfile(
            game_id="g1", display_name="g1", created_at=now, updated_at=now,
            ocr_profile=OCRProfile(dialog_text_region=OCRRegion(0.1, 0.7, 0.8, 0.2).to_dict()).to_dict(),
        ))
        self.assertTrue(self._controller().has_calibrated_dialog_region("g1"))


class SwitchSnapshotTest(ControllerTestBase):
    """M2 (stage 4): the switch IS stop(A)->start(B) on the same controller --
    binding swaps cleanly through None (the plain-chat window), A's full teardown
    (summary + ended, no dangling) lands before B exists."""

    def test_stop_start_sequence_swaps_binding_and_finishes_a(self):
        controller = self._controller()
        controller.start("0x1", game_id="a1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
        for text in ["L1", "L1", "L2", "L2"]:
            controller.session.on_ocr_result(text)
        a_session = controller.session.session_id
        self.assertEqual(controller.current_game_context().game_context_request.game_id, "a1")

        controller.stop()  # A's synchronous full teardown
        self.assertIsNone(controller.current_game_context())  # the switch window: plain chat

        controller.start("0x2", game_id="b1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
        binding = controller.current_game_context()
        self.assertEqual(binding.conversation_id, "galgame::b1::playthrough::default")

        # A finished completely: final summary written, session ended. (B is live
        # right now, so it legitimately shows in the dangling query -- assert the
        # clean slate only after B also stops.)
        self.assertEqual(len(self.mem.recent_summaries("a1")), 1)
        self.assertEqual(self.mem.get_play_session(a_session).state, "ended")
        b_session = controller.session.session_id
        controller.stop()
        self.assertEqual(self.mem.get_play_session(b_session).state, "ended")
        self.assertEqual(self.mem.dangling_play_sessions(), [])  # nothing dirty left


class BindingPublishTest(ControllerTestBase):
    """Stage 2: start() publishes the GameTurnBinding snapshot LAST; stop() clears
    it FIRST; a failed start publishes nothing. current_game_context() is the
    lock-free read the ChatEngine provider goes through."""

    def test_start_publishes_binding(self):
        controller = self._controller()
        controller.start("0x1", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
        binding = controller.current_game_context()
        self.assertIsNotNone(binding)
        self.assertEqual(binding.conversation_id, "galgame::g1::playthrough::default")
        gcr = binding.game_context_request
        self.assertEqual((gcr.mode, gcr.game_id, gcr.playthrough_id), ("active", "g1", "default"))
        controller.stop()

    def test_stop_clears_binding(self):
        controller = self._controller()
        controller.start("0x1", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
        controller.stop()
        self.assertIsNone(controller.current_game_context())
        controller.stop()  # idempotent stop keeps it None
        self.assertIsNone(controller.current_game_context())

    def test_failed_start_publishes_nothing(self):
        controller = self._controller()
        with patch.object(OcrStreamRunner, "start", side_effect=RuntimeError("window gone")):
            with self.assertRaises(RuntimeError):
                controller.start("0x1", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
        self.assertIsNone(controller.current_game_context())

    def test_before_start_returns_none(self):
        self.assertIsNone(self._controller().current_game_context())


class WatchTargetTest(ControllerTestBase):
    """Phase 9: the published (game_id, window_id) watch target follows the same
    snapshot discipline as the binding -- published on start, cleared FIRST on
    stop, None before start / after a failed start."""

    def test_start_publishes_watch_target(self):
        controller = self._controller()
        controller.start("0x42", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
        self.assertEqual(controller.current_watch_target(), ("g1", "0x42"))
        controller.stop()

    def test_stop_clears_watch_target(self):
        controller = self._controller()
        controller.start("0x42", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
        controller.stop()
        self.assertIsNone(controller.current_watch_target())

    def test_before_start_and_failed_start_none(self):
        controller = self._controller()
        self.assertIsNone(controller.current_watch_target())
        with patch.object(OcrStreamRunner, "start", side_effect=RuntimeError("window gone")):
            with self.assertRaises(RuntimeError):
                controller.start("0x42", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
        self.assertIsNone(controller.current_watch_target())


class StartFeedStopTest(ControllerTestBase):
    def test_start_feed_stop_persists_to_injected_game_memory(self):
        controller = self._controller()
        game_id = controller.start("0x1", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
        self.assertEqual(game_id, "g1")
        self.assertEqual(controller.session.state, GalgameState.PLAYING)
        session_id = controller.session.session_id

        # Feed simulated stable lines (the OCR loop is inert: empty OCR text).
        for text in ["L1", "L1", "L2", "L2"]:
            controller.session.on_ocr_result(text)
        # L1 committed (L2 still pending) -> persisted to the injected (test) DB.
        self.assertEqual([l.text for l in self.mem.committed_story_lines("g1")], ["L1"])

        controller.stop()
        # end() committed L2 + wrote the final summary, all in the injected DB.
        self.assertEqual({l.text for l in self.mem.committed_story_lines("g1")}, {"L1", "L2"})
        self.assertEqual(len(self.mem.recent_summaries("g1")), 1)
        self.assertEqual(self.mem.get_play_session(session_id).state, "ended")
        self.assertEqual(self.mem.dangling_play_sessions(), [])  # no dangling
        self.assertIsNone(controller.session)  # stopped
        self.assertEqual(controller.game_id, "g1")  # kept for later stages

    def test_stop_during_choice_checking_ends_the_session(self):
        """Review #2: stopping mid-choice must end() normally -- previously
        CHOICE_CHECKING was outside _ENDABLE (stage-1 scoping), so stop() left
        an active PlaySession for dangling recovery to mop up at next start."""
        controller = self._controller()
        controller.start("0x1", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
        session_id = controller.session.session_id
        controller.session.on_ocr_result("L1")
        controller.session.on_ocr_result("L1")  # L1 stable (pending_current)
        controller.session.begin_choice_check()
        self.assertEqual(controller.session.state, GalgameState.CHOICE_CHECKING)

        controller.stop()

        self.assertEqual(self.mem.get_play_session(session_id).state, "ended")
        self.assertEqual(self.mem.dangling_play_sessions(), [])  # not crash residue
        # end() committed the pending line + wrote the final summary as usual
        self.assertEqual({l.text for l in self.mem.committed_story_lines("g1")}, {"L1"})
        self.assertEqual(len(self.mem.recent_summaries("g1")), 1)
        self.assertIsNone(controller.session)

    def test_start_guesses_game_id_from_title(self):
        controller = self._controller()
        game_id = controller.start("0x1", window_title="LimeLight Lemonade Jam", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
        self.assertEqual(game_id, "limelight")
        controller.stop()


class StopSafetyTest(ControllerTestBase):
    def test_stop_before_start_is_noop(self):
        controller = self._controller()
        controller.stop()  # never started
        controller.stop()  # twice
        self.assertIsNone(controller.session)

    def test_stop_twice_after_start(self):
        controller = self._controller()
        controller.start("0x1", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
        controller.stop()
        controller.stop()  # idempotent, no crash
        self.assertIsNone(controller.session)


class StartFailureCleanupTest(ControllerTestBase):
    def test_runner_start_failure_finalizes_session_and_is_restartable(self):
        controller = self._controller()
        with patch.object(OcrStreamRunner, "start", side_effect=RuntimeError("window gone")):
            with self.assertRaises(RuntimeError):
                controller.start("0x1", game_id="g1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
        self.assertIsNone(controller.session)  # not half-started
        self.assertEqual(self.mem.dangling_play_sessions(), [])  # the started PlaySession was finalized
        # re-startable after a failed start
        controller.start("0x1", game_id="g2", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
        self.assertEqual(controller.session.state, GalgameState.PLAYING)
        controller.stop()

    def test_start_without_game_id_raises_and_leaves_nothing(self):
        from spica.galgame.companion_controller import GalgameCompanionError

        controller = self._controller()
        with self.assertRaises(GalgameCompanionError):
            controller.start("0x1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))  # no game_id, no title
        self.assertIsNone(controller.session)
        self.assertEqual(self.mem.dangling_play_sessions(), [])


if __name__ == "__main__":
    unittest.main()
