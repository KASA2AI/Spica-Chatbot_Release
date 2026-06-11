"""Phase 6: RapidOCR adapter is a thin bridge over the global ocr_image -- it must
delegate (single shared engine) and NEVER instantiate a second RapidOCR."""

import unittest
from unittest.mock import patch

from PIL import Image

from spica.adapters.ocr.rapidocr import RapidOcrAdapter


class OcrAdapterTest(unittest.TestCase):
    def test_delegates_to_ocr_image_and_holds_no_engine(self):
        fake = {
            "engine": "rapidocr",
            "raw_text": "こんにちは",
            "blocks": [{"text": "こんにちは", "confidence": 0.9, "box": []}],
            "error": None,
        }
        with patch("spica.adapters.ocr.rapidocr.ocr_image", return_value=fake) as mocked:
            adapter = RapidOcrAdapter()
            result = adapter.recognize(Image.new("RGB", (10, 10)))
        mocked.assert_called_once()  # only the shared ocr_image is used
        self.assertEqual(result.text, "こんにちは")
        self.assertEqual(len(result.blocks), 1)
        # The adapter holds no engine/model of its own (no second load).
        self.assertEqual(vars(adapter), {})

    def test_error_payload_is_carried(self):
        fake = {"engine": "rapidocr", "raw_text": "", "blocks": [], "error": {"code": "SCREEN_OCR_FAILED"}}
        with patch("spica.adapters.ocr.rapidocr.ocr_image", return_value=fake):
            result = RapidOcrAdapter().recognize(b"png")
        self.assertEqual(result.text, "")
        self.assertEqual(result.error, {"code": "SCREEN_OCR_FAILED"})


if __name__ == "__main__":
    unittest.main()
