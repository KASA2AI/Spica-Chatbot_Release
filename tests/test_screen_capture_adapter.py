"""Phase 6: mss screen-capture adapter (grab seam mocked, never touches a screen)."""

import unittest

from PIL import Image

from spica.adapters.screen_capture.mss_visible_window import MssScreenCapture


class CaptureTest(unittest.TestCase):
    def test_capture_rect_uses_grabber_and_wraps(self):
        calls = []

        def grabber(monitor):
            calls.append(monitor)
            return Image.new("RGB", (monitor["width"], monitor["height"]), (1, 2, 3))

        out = MssScreenCapture(grabber=grabber).capture_rect(100, 200, 300, 150)
        self.assertEqual(calls[0], {"left": 100, "top": 200, "width": 300, "height": 150})
        self.assertEqual((out.width, out.height), (300, 150))
        self.assertTrue(out.to_png_bytes().startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
