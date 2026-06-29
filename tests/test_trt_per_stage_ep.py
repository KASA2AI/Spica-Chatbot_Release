"""Per-stage EP routing + TRT load verification (LOCAL_RUNTIME_PLAN cut 2.1, CI-pure).

Diagnosis (real-machine): det/rec build clean TRT engines; cls fails TRT engine
build even single-shape (a cls graph problem) -> cls routed to CUDA. These pure
functions encode that routing + the "is TRT genuinely loaded?" check. No ORT/GPU.
"""

import unittest

from spica.local_runtime.ocr.trt_options import (
    CPU_EP,
    CUDA_EP,
    TRT_EP,
    classify_load_status,
    classify_stage,
    ep_list_for_stage,
)

# The ACTUAL bundled rapidocr_onnxruntime 1.4.4 model filenames -- a reverse-drift
# pin: if a rapidocr upgrade renames these, classify_stage breaks and CI goes red
# (instead of silently routing the wrong stage to TRT in production).
DET_MODEL = "/x/rapidocr_onnxruntime/models/ch_PP-OCRv4_det_infer.onnx"
CLS_MODEL = "/x/rapidocr_onnxruntime/models/ch_ppocr_mobile_v2.0_cls_infer.onnx"
REC_MODEL = "/x/rapidocr_onnxruntime/models/ch_PP-OCRv4_rec_infer.onnx"

_OPTS = dict(trt_options={"trt": 1}, cuda_options={"cuda": 1}, cpu_options={"cpu": 1})


class ClassifyStageTest(unittest.TestCase):
    def test_real_rapidocr_filenames(self):
        self.assertEqual(classify_stage(DET_MODEL), "det")
        self.assertEqual(classify_stage(CLS_MODEL), "cls")
        self.assertEqual(classify_stage(REC_MODEL), "rec")

    def test_unknown_is_default(self):
        self.assertEqual(classify_stage("/x/models/mystery.onnx"), "unknown")
        self.assertEqual(classify_stage(""), "unknown")

    def test_basename_only_no_parent_dir_match(self):
        # a parent dir containing "det" must NOT classify a cls model as det.
        self.assertEqual(classify_stage("/detection_models/some_cls_infer.onnx"), "cls")

    def test_windows_path_separators(self):
        self.assertEqual(classify_stage(r"C:\\models\\ch_PP-OCRv4_rec_infer.onnx"), "rec")


class EpListForStageTest(unittest.TestCase):
    def test_det_and_rec_get_trt_first(self):
        for model in (DET_MODEL, REC_MODEL):
            ep = ep_list_for_stage(model, **_OPTS)
            self.assertEqual([n for n, _ in ep], [TRT_EP, CUDA_EP, CPU_EP])
            self.assertEqual(ep[0][1], {"trt": 1})

    def test_cls_has_no_trt(self):
        ep = ep_list_for_stage(CLS_MODEL, **_OPTS)
        self.assertEqual([n for n, _ in ep], [CUDA_EP, CPU_EP])
        self.assertNotIn(TRT_EP, [n for n, _ in ep])

    def test_unknown_has_no_trt(self):
        # conservative: a renamed/unrecognized model never silently lands on TRT.
        ep = ep_list_for_stage("/x/models/mystery.onnx", **_OPTS)
        self.assertEqual([n for n, _ in ep], [CUDA_EP, CPU_EP])


class ClassifyLoadStatusTest(unittest.TestCase):
    def test_ok_when_det_rec_trt_cls_cuda(self):
        ok, diag = classify_load_status({"det": "trt", "rec": "trt", "cls": "cuda"})
        self.assertTrue(ok)
        self.assertIsNone(diag)

    def test_cls_on_trt_is_also_ok(self):
        # cls landing on trt is unexpected but not a FAILURE (only det/rec are gated).
        ok, _ = classify_load_status({"det": "trt", "rec": "trt", "cls": "trt"})
        self.assertTrue(ok)

    def test_fail_when_det_fell_back_to_cuda(self):
        ok, diag = classify_load_status({"det": "cuda", "rec": "trt", "cls": "cuda"})
        self.assertFalse(ok)
        self.assertIn("det", diag)
        self.assertIn("libnvinfer", diag)

    def test_fail_when_rec_fell_back(self):
        ok, diag = classify_load_status({"det": "trt", "rec": "cuda", "cls": "cuda"})
        self.assertFalse(ok)
        self.assertIn("rec", diag)

    def test_fail_lists_all_bad_stages(self):
        ok, diag = classify_load_status({"det": "cuda", "rec": "cpu", "cls": "cuda"})
        self.assertFalse(ok)
        self.assertIn("det", diag)
        self.assertIn("rec", diag)


if __name__ == "__main__":
    unittest.main()
