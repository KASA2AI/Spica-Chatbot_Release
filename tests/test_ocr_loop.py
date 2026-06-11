"""Phase 7: OcrStreamRunner -- safety gate (pause/recover with readable reason),
overlay-covers pause, and serial loop (single OCR at a time + slow-cycle warning)."""

import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.galgame.ocr_loop import OcrStreamRunner
from spica.galgame.session import GalgameCompanionSession, GalgameState
from spica.ports.ocr import OcrResult
from spica.ports.screen_capture import CaptureImage
from spica.ports.window_locator import WindowGeometry, WindowSafetyResult


class _Locator:
    def __init__(self):
        self.safety = WindowSafetyResult(ok=True)
        self.geom = WindowGeometry(0, 0, 100, 100)

    def enumerate_windows(self):
        raise NotImplementedError

    def get_window_geometry(self, window_id):
        return self.geom

    def check_safety(self, window_id, rule, overlay_window_id=None):
        return self.safety


class _Capture:
    def capture_rect(self, left, top, width, height):
        img = Image.new("RGB", (max(1, width), max(1, height)), (123, 50, 50))
        return CaptureImage(image=img, width=img.width, height=img.height)


class _Ocr:
    def __init__(self, text="台詞"):
        self.text = text

    def recognize(self, image):
        return OcrResult(text=self.text)


class _Sink:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)

    def of(self, kind):
        return [e for e in self.events if e.kind == kind]


class SafetyGateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mem = GameMemorySqliteAdapter(Path(self._tmp.name) / "g.sqlite3")
        self.sink = _Sink()
        self.session = GalgameCompanionSession(self.mem, emit=self.sink)
        self.session.bind_game("ABC")
        self.session.start()  # playing
        self.locator = _Locator()
        self.runner = OcrStreamRunner(self.session, _Capture(), self.locator, _Ocr(), interval_seconds=0.0)
        self.runner.configure("0x1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))

    def test_unsafe_pauses_with_readable_reason(self):
        self.locator.safety = WindowSafetyResult(ok=False, reason_code="WINDOW_NOT_FOCUSED", reason="…")
        self.runner.run_once()
        self.assertEqual(self.session.state, GalgameState.WINDOW_LOST)
        lost = self.sink.of("galgame_window_lost")
        self.assertEqual(lost[-1].reason, "WINDOW_NOT_FOCUSED")  # reason distinguishes pause from bug

    def test_recover_when_safe_again(self):
        self.locator.safety = WindowSafetyResult(ok=False, reason_code="WINDOW_MINIMIZED")
        self.runner.run_once()
        self.assertEqual(self.session.state, GalgameState.WINDOW_LOST)
        self.locator.safety = WindowSafetyResult(ok=True)
        self.runner.run_once()
        self.assertEqual(self.session.state, GalgameState.PLAYING)
        self.assertTrue(self.sink.of("galgame_window_recovered"))

    def test_all_four_unsafe_reasons_pause(self):
        for code in ("WINDOW_GONE", "WINDOW_MINIMIZED", "WINDOW_NOT_FOCUSED", "SAFETY_PROBE_FAILED"):
            with self.subTest(code=code):
                self.session = GalgameCompanionSession(self.mem, emit=self.sink)
                self.session.bind_game("ABC")
                self.session.start()
                runner = OcrStreamRunner(self.session, _Capture(), self.locator, _Ocr(), interval_seconds=0.0)
                runner.configure("0x1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
                self.locator.safety = WindowSafetyResult(ok=False, reason_code=code)
                runner.run_once()
                self.assertEqual(self.session.state, GalgameState.WINDOW_LOST)

    def test_overlay_covering_region_pauses(self):
        self.locator.safety = WindowSafetyResult(ok=True)  # focus etc. fine...
        self.runner.set_overlay_rect((0, 0, 100, 100))  # ...but overlay covers the whole OCR region
        self.runner.run_once()
        self.assertEqual(self.session.state, GalgameState.WINDOW_LOST)
        self.assertEqual(self.sink.of("galgame_window_lost")[-1].reason, "OVERLAY_COVERS")

    def test_safe_feeds_ocr_into_session(self):
        self.runner.run_once()
        self.runner.run_once()  # 2x same text -> stable -> pending line written
        self.assertIsNotNone(self.session.pending_current_line)


class SerialLoopTest(unittest.TestCase):
    def test_slow_cycle_no_overlap_and_warns(self):
        with TemporaryDirectory() as tmp:
            mem = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
            session = GalgameCompanionSession(mem)
            session.bind_game("ABC")
            session.start()
            locator = _Locator()

            class _SlowOcr:
                def __init__(self):
                    self.calls = 0
                    self.concurrent = 0
                    self.max_concurrent = 0
                    self._lock = threading.Lock()
                    self.stop_event = None

                def recognize(self, image):
                    with self._lock:
                        self.calls += 1
                        self.concurrent += 1
                        self.max_concurrent = max(self.max_concurrent, self.concurrent)
                    time.sleep(0.02)  # cycle > interval(0) -> warning
                    with self._lock:
                        self.concurrent -= 1
                    if self.calls >= 3 and self.stop_event is not None:
                        self.stop_event.set()
                    return OcrResult(text=f"line{self.calls}")

            ocr = _SlowOcr()
            runner = OcrStreamRunner(session, _Capture(), locator, ocr, interval_seconds=0.0)
            ocr.stop_event = runner._stop  # stop the loop after 3 cycles
            with self.assertLogs("spica.galgame.ocr_loop", level="WARNING") as logs:
                runner.start("0x1", dialog_ratios=(0.0, 0.0, 1.0, 1.0))
                time.sleep(0.4)
                runner.stop()
            self.assertGreaterEqual(ocr.calls, 3)
            self.assertEqual(ocr.max_concurrent, 1)  # single-thread loop never overlaps cycles
            self.assertTrue(any("ocr_cycle_ms" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
