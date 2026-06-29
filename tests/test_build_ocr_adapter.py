"""build_ocr_adapter factory (LOCAL_RUNTIME_PLAN §11.3: test_build_ocr_adapter).

The single OCR-provider selection point. Pins: default ``rapidocr`` is the
unchanged adapter (zero-diff); ``rapidocr_ort`` is selectable (experimental);
reserved-not-live (``rapidocr_trt_ep``) and unknown names degrade to the fallback
instead of crashing startup.
"""

import unittest

from spica.adapters.ocr import RapidOcrAdapter, RapidOcrOrtAdapter
from spica.host.agent_assembly import build_ocr_adapter


class BuildOcrAdapterTest(unittest.TestCase):
    def test_default_is_rapidocr_unchanged(self):
        adapter = build_ocr_adapter()
        self.assertIsInstance(adapter, RapidOcrAdapter)
        self.assertEqual(adapter.name, "rapidocr")

    def test_explicit_rapidocr(self):
        adapter = build_ocr_adapter("rapidocr")
        self.assertIsInstance(adapter, RapidOcrAdapter)

    def test_rapidocr_ort_experimental_selectable(self):
        adapter = build_ocr_adapter("rapidocr_ort")
        self.assertIsInstance(adapter, RapidOcrOrtAdapter)
        self.assertEqual(adapter.name, "rapidocr_ort")

    def test_reserved_trt_ep_falls_back_to_rapidocr(self):
        # rapidocr_trt_ep is reserved for step 2 (not live) -> graceful fallback.
        adapter = build_ocr_adapter("rapidocr_trt_ep", fallback_provider="rapidocr")
        self.assertIsInstance(adapter, RapidOcrAdapter)

    def test_unknown_provider_falls_back(self):
        adapter = build_ocr_adapter("totally_unknown", fallback_provider="rapidocr")
        self.assertIsInstance(adapter, RapidOcrAdapter)

    def test_unknown_with_no_fallback_still_yields_rapidocr(self):
        adapter = build_ocr_adapter("totally_unknown", fallback_provider=None)
        self.assertIsInstance(adapter, RapidOcrAdapter)

    def test_blank_provider_defaults_to_rapidocr(self):
        self.assertIsInstance(build_ocr_adapter("  "), RapidOcrAdapter)


if __name__ == "__main__":
    unittest.main()
