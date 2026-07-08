"""Phase 7: the RapidOCR inference lock serializes ALL OCR paths.

_INFER_LOCK lives inside ocr_image, and BOTH the galgame loop (RapidOcrAdapter)
and inspect_screen (analyzer) call ocr_image -- so two inferences never run
concurrently on the shared _ENGINE, on either path.
"""

import threading
import time
import unittest

from PIL import Image

from agent_tools.function_tools.screen.backends import rapidocr as backend


class _SerialProbe:
    """Stands in for the RapidOCR engine; records peak concurrency on __call__."""

    def __init__(self) -> None:
        self.calls = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self._lock = threading.Lock()

    def __call__(self, prepared):
        with self._lock:
            self.calls += 1
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        time.sleep(0.02)  # widen the overlap window
        with self._lock:
            self.concurrent -= 1
        return ([],)  # rapidocr-shaped: unwrap -> [] -> no blocks


class RapidOcrLockTest(unittest.TestCase):
    def test_inference_serialized_across_concurrent_calls(self):
        probe = _SerialProbe()
        backend._ENGINE = probe  # inject instrumented engine (skip real model load)
        try:
            threads = [
                threading.Thread(target=lambda: backend.ocr_image(Image.new("RGB", (8, 8))))
                for _ in range(6)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            backend.clear_rapidocr_engine()
        self.assertEqual(probe.calls, 6)
        self.assertEqual(probe.max_concurrent, 1)  # _INFER_LOCK held -> no overlap

    def test_inspect_screen_path_uses_the_same_locked_ocr_image(self):
        from agent_tools.function_tools.screen import analyzer
        from agent_tools.function_tools.screen.backends import ocr_runtime

        # cut 1: analyzer (inspect_screen) now routes OCR through the run_ocr seam
        # (path-B unification). With no provider installed (the rapidocr default),
        # run_ocr falls back to the SAME backend.ocr_image whose body holds
        # _INFER_LOCK -> both OCR paths still serialize on the shared _ENGINE.
        self.assertIs(analyzer.run_ocr, ocr_runtime.run_ocr)
        self.assertIs(ocr_runtime.ocr_image, backend.ocr_image)


if __name__ == "__main__":
    unittest.main()
