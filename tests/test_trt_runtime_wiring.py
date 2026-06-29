"""TRT runtime/adapter wiring (LOCAL_RUNTIME_PLAN cut 2, CI -- no real TRT).

Three CI-safe concerns (NO engine build, NO GPU):
- reverse-drift pin: the three rapidocr submodules the scoped patch swaps still
  expose ``OrtInferSession`` (a rapidocr upgrade that renames them fails CI loudly,
  not silently in production).
- the TRT session subclass's ``_get_ep_list`` yields TRT -> CUDA -> CPU.
- the adapter is LAZY (construction builds no engine -> CI/factory safe), wears
  ``OCRPort``, maps the runtime dict to ``OcrResult``, and degrades a build failure
  to a best-effort error instead of raising.
"""

import importlib
import unittest

import pytest

from spica.adapters.ocr import RapidOcrTrtEpAdapter
from spica.local_runtime.errors import LOCAL_RUNTIME_INFERENCE_FAILED
from spica.ports.ocr import OCRPort, OcrResult


class _FakeRuntime:
    used_providers = "trt"

    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def recognize(self, image):
        self.calls.append(image)
        return self._payload


class TrtAdapterLazyContractTest(unittest.TestCase):
    def test_construction_is_lazy_and_builds_no_runtime(self):
        adapter = RapidOcrTrtEpAdapter()  # must NOT touch GPU / build an engine
        self.assertIsNone(adapter._runtime)
        self.assertIsInstance(adapter, OCRPort)
        self.assertEqual(adapter.name, "rapidocr_trt_ep")

    def test_recognize_maps_runtime_dict_to_ocr_result(self):
        runtime = _FakeRuntime(
            {"engine": "rapidocr", "raw_text": "識別", "blocks": [{"text": "識別"}], "error": None}
        )
        adapter = RapidOcrTrtEpAdapter(runtime=runtime)
        result = adapter.recognize(b"img")
        self.assertIsInstance(result, OcrResult)
        self.assertEqual(result.text, "識別")
        self.assertEqual(result.blocks, [{"text": "識別"}])
        self.assertEqual(runtime.calls, [b"img"])

    def test_build_failure_degrades_to_best_effort_error(self):
        adapter = RapidOcrTrtEpAdapter()

        def boom():
            raise RuntimeError("trt build boom")

        adapter._ensure_runtime = boom  # simulate a total build failure
        result = adapter.recognize(b"img")
        self.assertEqual(result.text, "")
        self.assertEqual(result.error["code"], LOCAL_RUNTIME_INFERENCE_FAILED)
        self.assertTrue(result.error["recoverable"])  # never raises into a turn

    def test_warmup_returns_used_providers(self):
        adapter = RapidOcrTrtEpAdapter(runtime=_FakeRuntime({}))
        self.assertEqual(adapter.warmup(), "trt")


class _FakeSession:
    def __init__(self, providers):
        self._providers = providers

    def get_providers(self):
        return list(self._providers)


def _fake_engine(providers):
    from types import SimpleNamespace

    return SimpleNamespace(text_det=SimpleNamespace(infer=SimpleNamespace(session=_FakeSession(providers))))


class DetectActiveProviderTest(unittest.TestCase):
    # Honest reporting: ORT silently falls back to CUDA INSIDE a "successful" TRT
    # build when libnvinfer is missing, so we must read the session's real provider
    # list, not trust that the TRT factory didn't raise (regression from the
    # real-machine smoke where used_providers wrongly said "trt").
    def test_detects_trt_cuda_cpu_from_session(self):
        from spica.local_runtime.ocr.rapidocr_trt_runtime import detect_active_provider

        self.assertEqual(
            detect_active_provider(_fake_engine(["TensorrtExecutionProvider", "CUDAExecutionProvider"])),
            "trt",
        )
        # TRT libs missing -> ORT fell back; must report cuda, NOT trt.
        self.assertEqual(
            detect_active_provider(_fake_engine(["CUDAExecutionProvider", "CPUExecutionProvider"])),
            "cuda",
        )
        self.assertEqual(detect_active_provider(_fake_engine(["CPUExecutionProvider"])), "cpu")

    def test_unknown_when_session_unreadable(self):
        from spica.local_runtime.ocr.rapidocr_trt_runtime import detect_active_provider

        self.assertEqual(detect_active_provider(object()), "unknown")


class TrtSessionPatchTest(unittest.TestCase):
    def test_patch_targets_still_exist(self):
        # Reverse-drift: rapidocr must still expose OrtInferSession where we patch.
        pytest.importorskip("rapidocr_onnxruntime")
        from spica.local_runtime.ocr.rapidocr_trt_runtime import _PATCH_TARGETS

        for module_name, attr in _PATCH_TARGETS:
            module = importlib.import_module(module_name)
            self.assertTrue(
                hasattr(module, attr), f"{module_name}.{attr} missing -- rapidocr API drift"
            )

    def test_trt_session_class_get_ep_list_orders_trt_cuda_cpu(self):
        pytest.importorskip("rapidocr_onnxruntime")
        from spica.local_runtime.ocr.rapidocr_trt_runtime import make_trt_session_class
        from spica.local_runtime.ocr.trt_options import CPU_EP, CUDA_EP, TRT_EP

        cls = make_trt_session_class(
            {"trt_engine_cache_enable": True}, {"device_id": 0}, {"x": "y"}
        )
        inst = cls.__new__(cls)  # bypass __init__ -> no real InferenceSession built
        inst.cfg_use_cuda = False  # _check_cuda short-circuits without a GPU
        ep_list = inst._get_ep_list()
        self.assertEqual([name for name, _ in ep_list], [TRT_EP, CUDA_EP, CPU_EP])
        self.assertFalse(inst.use_directml)


if __name__ == "__main__":
    unittest.main()
