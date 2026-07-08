"""Phase 6: GalgameOcrCalibrator -- set region stores ocr_profile (ratio+pixel+
window size); run_ocr_test emits preview + result; suspect_blank flagged."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.galgame.models import GameProfile, OCRProfile, utc_now_iso
from spica.galgame.ocr_calibration import GalgameOcrCalibrator
from spica.ports.ocr import OcrResult
from spica.ports.screen_capture import CaptureImage
from spica.ports.window_locator import WindowGeometry


class _FakeLocator:
    def __init__(self, geom):
        self.geom = geom

    def enumerate_windows(self):  # not used here
        raise NotImplementedError

    def get_window_geometry(self, window_id):
        return self.geom


class _FakeCapture:
    def __init__(self, image):
        self.image = image
        self.calls = []

    def capture_rect(self, left, top, width, height):
        self.calls.append((left, top, width, height))
        return CaptureImage(image=self.image, width=self.image.width, height=self.image.height)


class _FakeOcr:
    def __init__(self, text="認識テキスト"):
        self.text = text

    def recognize(self, image):
        return OcrResult(text=self.text, blocks=[])


class _Sink:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)

    def of(self, kind):
        return [e for e in self.events if e.kind == kind]


class CalibrationBase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mem = GameMemorySqliteAdapter(Path(self._tmp.name) / "g.sqlite3")
        now = utc_now_iso()
        self.mem.upsert_game_profile(GameProfile(game_id="ABC", display_name="X", created_at=now, updated_at=now))
        self.sink = _Sink()
        self.geom = WindowGeometry(x=100, y=200, width=1000, height=500)


class SetRegionTest(CalibrationBase):
    def test_set_dialog_region_stores_ratio_pixel_and_window_size(self):
        cal = GalgameOcrCalibrator(
            _FakeCapture(Image.new("RGB", (1, 1))), _FakeLocator(self.geom), _FakeOcr(), self.mem, emit=self.sink
        )
        self.assertTrue(cal.set_dialog_region("ABC", "0x1", (200, 300, 500, 250)))  # offset 100,100; size 500x250
        region = OCRProfile.from_dict(self.mem.get_game_profile("ABC").ocr_profile).dialog_text_region
        self.assertAlmostEqual(region["x_ratio"], 0.1)
        self.assertAlmostEqual(region["w_ratio"], 0.5)
        self.assertEqual(region["window_size_at_calibration"], [1000, 500])
        self.assertEqual(region["pixel_rect"], [100, 100, 500, 250])

    def test_missing_geometry_emits_error_and_returns_false(self):
        cal = GalgameOcrCalibrator(
            _FakeCapture(Image.new("RGB", (1, 1))), _FakeLocator(None), _FakeOcr(), self.mem, emit=self.sink
        )
        self.assertFalse(cal.set_dialog_region("ABC", "0x1", (0, 0, 10, 10)))
        self.assertEqual(self.sink.of("galgame_error")[0].code, "WINDOW_GEOMETRY_UNAVAILABLE")


class RunTestTest(CalibrationBase):
    def _calibrated_full_window(self, image):
        cal = GalgameOcrCalibrator(_FakeCapture(image), _FakeLocator(self.geom), _FakeOcr(), self.mem, emit=self.sink)
        cal.set_dialog_region("ABC", "0x1", (100, 200, 1000, 500))  # ratios (0,0,1,1) -> whole window
        self.sink.events.clear()
        return cal

    def test_run_ocr_test_emits_preview_and_result(self):
        img = Image.new("RGB", (1000, 500), (0, 0, 0))
        for x in range(200):
            for y in range(100):
                img.putpixel((x, y), (255, 255, 255))  # variety -> not blank
        self._calibrated_full_window(img).run_ocr_test("ABC", "0x1")
        preview = self.sink.of("galgame_ocr_preview_ready")[0]
        self.assertEqual(preview.region, "dialog")
        self.assertFalse(preview.suspect_blank)
        self.assertTrue(preview.image_png.startswith(b"\x89PNG"))
        self.assertEqual(self.sink.of("galgame_ocr_test_result")[0].dialog_text, "認識テキスト")

    def test_suspect_blank_flagged_on_black_capture(self):
        self._calibrated_full_window(Image.new("RGB", (1000, 500), (0, 0, 0))).run_ocr_test("ABC", "0x1")
        self.assertTrue(self.sink.of("galgame_ocr_preview_ready")[0].suspect_blank)


if __name__ == "__main__":
    unittest.main()
