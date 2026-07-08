"""TRT->CUDA build/warmup fallback orchestration (LOCAL_RUNTIME_PLAN cut 2, CI-pure).

The hard constraint: TRT EP init/build failure MUST degrade to CUDA, never crash a
turn. Drives ``build_engine_with_fallback`` with injected fake factories -- no ORT,
no GPU. Covers: TRT ok, TRT-construct fails, TRT-warmup fails (engine build surfaces
at first inference), and that warmup runs on whichever engine is used.
"""

import unittest

from spica.local_runtime.ocr.trt_options import build_engine_with_fallback


class TrtEngineFallbackTest(unittest.TestCase):
    def test_primary_success_uses_trt(self):
        warmed = []
        result = build_engine_with_fallback(
            primary_factory=lambda: "trt_engine",
            fallback_factory=lambda: "cuda_engine",
            warmup=warmed.append,
        )
        self.assertEqual(result.used, "trt")
        self.assertEqual(result.engine, "trt_engine")
        self.assertIsNone(result.error)
        self.assertEqual(warmed, ["trt_engine"])  # warmed the TRT engine

    def test_primary_construction_failure_falls_back_to_cuda(self):
        fell_back_with = []

        def boom():
            raise RuntimeError("TRT EP not available (libnvinfer missing)")

        result = build_engine_with_fallback(
            primary_factory=boom,
            fallback_factory=lambda: "cuda_engine",
            on_fallback=fell_back_with.append,
        )
        self.assertEqual(result.used, "cuda")
        self.assertEqual(result.engine, "cuda_engine")
        self.assertIsInstance(result.error, RuntimeError)
        self.assertEqual(len(fell_back_with), 1)

    def test_warmup_failure_surfaces_build_error_and_falls_back(self):
        # Engine *builds* lazily at first inference -> a TRT build failure shows up
        # in warmup, not construction. Must still fall back to CUDA.
        warmed = []

        def warmup(engine):
            if engine == "trt_engine":
                raise RuntimeError("TRT engine build failed (unsupported op)")
            warmed.append(engine)

        result = build_engine_with_fallback(
            primary_factory=lambda: "trt_engine",
            fallback_factory=lambda: "cuda_engine",
            warmup=warmup,
        )
        self.assertEqual(result.used, "cuda")
        self.assertEqual(result.engine, "cuda_engine")
        self.assertEqual(warmed, ["cuda_engine"])  # CUDA engine warmed after fallback

    def test_no_warmup_is_allowed(self):
        result = build_engine_with_fallback(
            primary_factory=lambda: "trt_engine",
            fallback_factory=lambda: "cuda_engine",
        )
        self.assertEqual(result.used, "trt")


if __name__ == "__main__":
    unittest.main()
