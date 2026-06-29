"""build_ocr_adapter factory (LOCAL_RUNTIME_PLAN §11.3: test_build_ocr_adapter).

The single OCR-provider selection point. Pins: default ``rapidocr`` is the
unchanged adapter (zero-diff); ``rapidocr_ort`` is selectable (experimental);
reserved-not-live (``rapidocr_trt_ep``) and unknown names degrade to the fallback
instead of crashing startup.
"""

import unittest

from spica.adapters.ocr import RapidOcrAdapter, RapidOcrOrtAdapter, RapidOcrTrtEpAdapter
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

    def test_rapidocr_trt_ep_builds_lazy_adapter(self):
        # cut 2: rapidocr_trt_ep now builds the (LAZY) TRT-EP adapter -- no engine
        # built here, so this is CI-safe (no GPU / TRT).
        adapter = build_ocr_adapter("rapidocr_trt_ep")
        self.assertIsInstance(adapter, RapidOcrTrtEpAdapter)
        self.assertEqual(adapter.name, "rapidocr_trt_ep")
        self.assertIsNone(adapter._runtime)  # lazy: nothing built

    def test_rapidocr_trt_ep_resolves_cache_dir_to_absolute(self):
        from spica.config.schema import TrtOcrConfig

        adapter = build_ocr_adapter(
            "rapidocr_trt_ep", trt_config=TrtOcrConfig(engine_cache_dir="artifacts/trt")
        )
        # repo-relative -> absolute (no env / no cwd dependence, §3.3).
        self.assertTrue(adapter._cfg["engine_cache_dir"].endswith("artifacts/trt"))
        self.assertTrue(adapter._cfg["engine_cache_dir"].startswith("/"))
        self.assertFalse(adapter._cfg["fp16"])  # D4: fp32 default

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
