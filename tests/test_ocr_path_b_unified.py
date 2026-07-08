"""Path-B OCR unification (LOCAL_RUNTIME_PLAN §11.3: the path-unification regression).

Proves inspect_screen / manual screen analysis ("path B") now route OCR through
the shared ``OCRPort`` seam (``run_ocr``) instead of the bare ``ocr_image`` it used
to call directly (§2.2) -- so a provider swap covers BOTH paths and they cannot
fork. Also pins the zero-diff default: with NO provider installed, ``run_ocr``
falls back to the legacy ``ocr_image`` byte-for-byte.
"""

import unittest
from unittest.mock import patch

from agent_tools.function_tools.screen import analyzer
from agent_tools.function_tools.screen.backends import ocr_runtime
from spica.ports.ocr import OcrResult


class _SentinelProvider:
    name = "sentinel_ort"

    def __init__(self):
        self.calls = []

    def recognize(self, image):
        self.calls.append(image)
        return OcrResult(text="哈囉世界", blocks=[{"text": "哈囉世界"}], error=None)


class OcrPathBUnifiedTest(unittest.TestCase):
    def tearDown(self):
        ocr_runtime.reset_active_ocr_provider()  # never leak global state across tests

    def test_analyzer_calls_the_seam_not_bare_ocr_image(self):
        # The unification: analyzer's OCR call IS the shared seam object, so
        # whatever the host installs governs inspect_screen too.
        self.assertIs(analyzer.run_ocr, ocr_runtime.run_ocr)

    def test_installed_provider_routes_path_b_through_port(self):
        sentinel = _SentinelProvider()
        ocr_runtime.set_active_ocr_provider(sentinel)

        result = ocr_runtime.run_ocr(b"fake-image-bytes")

        self.assertEqual(sentinel.calls, [b"fake-image-bytes"])  # provider was used
        self.assertEqual(result["engine"], "sentinel_ort")
        self.assertEqual(result["raw_text"], "哈囉世界")
        self.assertEqual(result["blocks"], [{"text": "哈囉世界"}])
        self.assertIsNone(result["error"])

    def test_no_provider_falls_back_to_legacy_ocr_image(self):
        ocr_runtime.reset_active_ocr_provider()
        legacy_payload = {"engine": "rapidocr", "raw_text": "legacy", "blocks": [], "error": None}
        with patch.object(ocr_runtime, "ocr_image", return_value=legacy_payload) as legacy:
            result = ocr_runtime.run_ocr(b"img")
        legacy.assert_called_once_with(b"img")
        self.assertEqual(result, legacy_payload)  # byte-identical default path

    def test_get_and_reset_active_provider(self):
        self.assertIsNone(ocr_runtime.get_active_ocr_provider())
        sentinel = _SentinelProvider()
        ocr_runtime.set_active_ocr_provider(sentinel)
        self.assertIs(ocr_runtime.get_active_ocr_provider(), sentinel)
        ocr_runtime.reset_active_ocr_provider()
        self.assertIsNone(ocr_runtime.get_active_ocr_provider())


if __name__ == "__main__":
    unittest.main()
