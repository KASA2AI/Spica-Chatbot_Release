"""Phase 6: pure OCR-region geometry + suspect-blank heuristic."""

import unittest

from PIL import Image

from spica.galgame.ocr_region import (
    crop_by_ratios,
    looks_blank,
    ratios_to_pixel_rect,
    screen_rect_to_ratios,
)
from spica.ports.window_locator import WindowGeometry


class RatioMathTest(unittest.TestCase):
    def test_screen_rect_to_ratios(self):
        geom = WindowGeometry(x=100, y=200, width=1000, height=500)
        ratios = screen_rect_to_ratios((200, 300, 500, 250), geom)  # offset 100,100; size 500x250
        self.assertAlmostEqual(ratios[0], 0.1)
        self.assertAlmostEqual(ratios[1], 0.2)
        self.assertAlmostEqual(ratios[2], 0.5)
        self.assertAlmostEqual(ratios[3], 0.5)

    def test_ratios_to_pixel_rect_scales_with_window_size(self):
        ratios = (0.1, 0.2, 0.5, 0.5)
        self.assertEqual(ratios_to_pixel_rect(ratios, (1000, 500)), (100, 100, 500, 250))
        # SAME ratios on a resized window -> scaled pixels (this is the adapt-on-resize)
        self.assertEqual(ratios_to_pixel_rect(ratios, (2000, 1000)), (200, 200, 1000, 500))

    def test_crop_by_ratios_adapts_to_image_size(self):
        crop = crop_by_ratios(Image.new("RGB", (1000, 500), (255, 255, 255)), (0.1, 0.2, 0.5, 0.5))
        self.assertEqual((crop.width, crop.height), (500, 250))
        crop2 = crop_by_ratios(Image.new("RGB", (2000, 1000), (255, 255, 255)), (0.1, 0.2, 0.5, 0.5))
        self.assertEqual((crop2.width, crop2.height), (1000, 500))

    def test_out_of_bounds_clamped(self):
        geom = WindowGeometry(x=0, y=0, width=100, height=100)
        ratios = screen_rect_to_ratios((-50, -50, 1000, 1000), geom)
        for value in ratios:
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)


class LooksBlankTest(unittest.TestCase):
    def test_all_black_is_blank(self):
        self.assertTrue(looks_blank(Image.new("RGB", (40, 20), (0, 0, 0))))

    def test_uniform_colour_is_blank(self):
        self.assertTrue(looks_blank(Image.new("RGB", (40, 20), (10, 20, 200))))

    def test_varied_image_is_not_blank(self):
        img = Image.new("RGB", (40, 20), (0, 0, 0))
        for x in range(20):
            for y in range(20):
                img.putpixel((x, y), (255, 255, 255))
        self.assertFalse(looks_blank(img))


if __name__ == "__main__":
    unittest.main()
