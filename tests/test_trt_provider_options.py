"""TRT EP provider options + EP list (LOCAL_RUNTIME_PLAN cut 2, CI-pure §6.5).

No onnxruntime / GPU / model -- pins the option dict + provider ordering that the
real session class hands to ORT.
"""

import unittest

from spica.local_runtime.ocr.trt_options import (
    CPU_EP,
    CUDA_EP,
    TRT_EP,
    build_ep_list,
    build_trt_provider_options,
)


class TrtProviderOptionsTest(unittest.TestCase):
    def test_fp32_default_and_cache_always_on(self):
        opts = build_trt_provider_options(
            fp16=False, engine_cache_dir="/abs/artifacts/trt", timing_cache=True
        )
        self.assertEqual(opts["trt_fp16_enable"], False)  # cut-2 default = fp32
        self.assertEqual(opts["trt_engine_cache_enable"], True)
        self.assertEqual(opts["trt_engine_cache_path"], "/abs/artifacts/trt")
        self.assertEqual(opts["trt_timing_cache_enable"], True)
        self.assertEqual(opts["trt_timing_cache_path"], "/abs/artifacts/trt")
        self.assertEqual(opts["device_id"], 0)

    def test_fp16_configurable(self):
        opts = build_trt_provider_options(
            fp16=True, engine_cache_dir="/c", timing_cache=False
        )
        self.assertEqual(opts["trt_fp16_enable"], True)
        self.assertEqual(opts["trt_timing_cache_enable"], False)

    def test_no_profiles_emitted_by_default(self):
        opts = build_trt_provider_options(fp16=False, engine_cache_dir="/c", timing_cache=True)
        self.assertNotIn("trt_profile_min_shapes", opts)
        self.assertNotIn("trt_profile_opt_shapes", opts)
        self.assertNotIn("trt_profile_max_shapes", opts)

    def test_profiles_emitted_when_supplied(self):
        opts = build_trt_provider_options(
            fp16=False,
            engine_cache_dir="/c",
            timing_cache=True,
            profiles={
                "min": "x:1x3x32x32",
                "opt": "x:1x3x48x320",
                "max": "x:1x3x48x1280",
            },
        )
        self.assertEqual(opts["trt_profile_min_shapes"], "x:1x3x32x32")
        self.assertEqual(opts["trt_profile_opt_shapes"], "x:1x3x48x320")
        self.assertEqual(opts["trt_profile_max_shapes"], "x:1x3x48x1280")

    def test_ep_list_priority_trt_cuda_cpu(self):
        opts = build_trt_provider_options(fp16=False, engine_cache_dir="/c", timing_cache=True)
        ep_list = build_ep_list(opts)
        names = [name for name, _ in ep_list]
        self.assertEqual(names, [TRT_EP, CUDA_EP, CPU_EP])  # TRT first, CUDA fallback, CPU last
        self.assertEqual(ep_list[0][1]["trt_engine_cache_enable"], True)
        self.assertIn("cudnn_conv_algo_search", ep_list[1][1])  # CUDA opts present

    def test_ep_list_accepts_custom_provider_options(self):
        ep_list = build_ep_list(
            {"k": "v"}, cuda_options={"device_id": 1}, cpu_options={"x": "y"}
        )
        self.assertEqual(ep_list[1][1], {"device_id": 1})
        self.assertEqual(ep_list[2][1], {"x": "y"})


if __name__ == "__main__":
    unittest.main()
