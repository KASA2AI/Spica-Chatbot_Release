"""rapidocr_ort adapter satisfies OCRPort (LOCAL_RUNTIME_PLAN §11.3).

The new local-runtime adapter wears the EXISTING ``OCRPort`` (no second port,
§3.1) and shape-maps the runtime dict into ``OcrResult`` exactly like the legacy
adapter -- so the two are drop-in interchangeable behind the factory. A fake
runtime keeps this CI test off any real model / GPU (§6.5).
"""

import unittest

from spica.adapters.ocr import RapidOcrAdapter, RapidOcrOrtAdapter
from spica.ports.ocr import OCRPort, OcrResult


class _FakeRuntime:
    def __init__(self, payload):
        self._payload = payload

    def recognize(self, image):
        return self._payload


class LocalRuntimeOcrContractTest(unittest.TestCase):
    def test_satisfies_ocr_port_protocol(self):
        # runtime_checkable Protocol: both adapters expose recognize().
        self.assertIsInstance(RapidOcrOrtAdapter(_FakeRuntime({})), OCRPort)
        self.assertIsInstance(RapidOcrAdapter(), OCRPort)

    def test_name_is_experimental_provider(self):
        self.assertEqual(RapidOcrOrtAdapter(_FakeRuntime({})).name, "rapidocr_ort")

    def test_maps_runtime_dict_to_ocr_result(self):
        payload = {
            "engine": "rapidocr",
            "raw_text": "識別文字",
            "blocks": [{"text": "識別文字", "confidence": 0.9}],
            "error": None,
        }
        adapter = RapidOcrOrtAdapter(_FakeRuntime(payload))
        result = adapter.recognize(b"img")
        self.assertIsInstance(result, OcrResult)
        self.assertEqual(result.text, "識別文字")
        self.assertEqual(result.blocks, [{"text": "識別文字", "confidence": 0.9}])
        self.assertIsNone(result.error)

    def test_tolerates_missing_and_malformed_fields(self):
        # Best-effort contract: empty/odd payloads never crash, just degrade.
        adapter = RapidOcrOrtAdapter(_FakeRuntime({}))
        result = adapter.recognize(b"img")
        self.assertEqual(result.text, "")
        self.assertEqual(result.blocks, [])
        self.assertIsNone(result.error)

        err_payload = {"raw_text": "", "blocks": "not-a-list", "error": {"code": "X"}}
        result2 = RapidOcrOrtAdapter(_FakeRuntime(err_payload)).recognize(b"img")
        self.assertEqual(result2.blocks, [])  # non-list coerced to []
        self.assertEqual(result2.error, {"code": "X"})


if __name__ == "__main__":
    unittest.main()
