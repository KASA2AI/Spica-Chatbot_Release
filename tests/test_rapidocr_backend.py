import sys
from io import BytesIO
from types import SimpleNamespace

from PIL import Image

from agent_tools.function_tools.screen.backends import rapidocr
from agent_tools.function_tools.screen.schema import ScreenToolError


def install_fake_rapidocr(monkeypatch, *, fail=False):
    calls = []

    class FakeRapidOCR:
        def __call__(self, image):
            calls.append(image)
            if fail:
                raise RuntimeError("ocr boom")
            return (
                [
                    [[[1, 2], [20, 2], [20, 10], [1, 10]], "Hello", 0.98],
                    [[[2, 22], [30, 22], [30, 35], [2, 35]], "World", 0.87],
                ],
                {"elapsed": 0.1},
            )

    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", SimpleNamespace(RapidOCR=FakeRapidOCR))
    rapidocr.clear_rapidocr_engine()
    return calls


def test_rapidocr_backend_import_does_not_load_engine():
    rapidocr.clear_rapidocr_engine()
    assert rapidocr._ENGINE is None


def test_ocr_image_accepts_pil_image(monkeypatch):
    calls = install_fake_rapidocr(monkeypatch)

    result = rapidocr.ocr_image(Image.new("RGB", (64, 32), "white"))

    assert result["engine"] == "rapidocr"
    assert result["raw_text"] == "Hello\nWorld"
    assert result["blocks"][0]["text"] == "Hello"
    assert result["blocks"][0]["confidence"] == 0.98
    assert result["blocks"][0]["box"] == [[1.0, 2.0], [20.0, 2.0], [20.0, 10.0], [1.0, 10.0]]
    assert result["error"] is None
    assert calls


def test_ocr_image_accepts_png_bytes(monkeypatch):
    install_fake_rapidocr(monkeypatch)
    buffer = BytesIO()
    Image.new("RGB", (32, 16), "white").save(buffer, format="PNG")

    result = rapidocr.ocr_image(buffer.getvalue())

    assert result["raw_text"] == "Hello\nWorld"
    assert len(result["blocks"]) == 2
    assert result["error"] is None


def test_ocr_failure_returns_empty_result(monkeypatch):
    install_fake_rapidocr(monkeypatch, fail=True)

    result = rapidocr.ocr_image(Image.new("RGB", (64, 32), "white"))

    assert result["raw_text"] == ""
    assert result["blocks"] == []
    assert result["error"]["stage"] == "ocr"
    assert result["error"]["code"] == "SCREEN_OCR_FAILED"
    assert result["error"]["recoverable"] is True


def test_missing_rapidocr_returns_dependency_error(monkeypatch):
    rapidocr.clear_rapidocr_engine()

    def fail_load():
        raise ScreenToolError("SCREEN_OCR_DEPENDENCY_MISSING", "missing rapidocr")

    monkeypatch.setattr(rapidocr, "_load_rapidocr_class", fail_load)

    result = rapidocr.ocr_image(Image.new("RGB", (64, 32), "white"))

    assert result["raw_text"] == ""
    assert result["blocks"] == []
    assert result["error"]["code"] == "SCREEN_OCR_DEPENDENCY_MISSING"


# ---- cut 2 (D2): recognize_with_engine extraction -- ocr_image must stay a thin,
# byte-identical caller over the global engine + lock; the helper lets the TRT
# runtime reuse the SAME prepare/parse/error body with its OWN engine + lock. ----

class _RecordingLock:
    def __init__(self):
        self.entered = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc):
        return False


def test_recognize_with_engine_uses_given_engine_and_lock():
    lock = _RecordingLock()
    seen = []

    def fake_engine(prepared):
        seen.append(prepared)
        return ([[[[0, 0], [9, 0], [9, 9], [0, 9]], "Trt", 0.95]], {"elapse": 0.0})

    result = rapidocr.recognize_with_engine(
        lambda: fake_engine, Image.new("RGB", (16, 16), "white"), lock
    )

    assert lock.entered == 1  # inference serialized on the PROVIDED lock
    assert seen  # the PROVIDED engine ran
    assert result["engine"] == "rapidocr"
    assert result["raw_text"] == "Trt"
    assert result["error"] is None


def test_recognize_with_engine_is_best_effort_on_failure():
    lock = _RecordingLock()

    def boom(prepared):
        raise RuntimeError("trt inference boom")

    result = rapidocr.recognize_with_engine(lambda: boom, Image.new("RGB", (16, 16), "white"), lock)

    assert result["raw_text"] == ""
    assert result["blocks"] == []
    assert result["error"]["code"] == "SCREEN_OCR_FAILED"  # swallowed, never raised


def test_recognize_with_engine_catches_engine_load_failure():
    # The engine provider is resolved INSIDE the protected block, so a load failure
    # (e.g. missing dependency) becomes a best-effort error, not a raise -- matching
    # pre-extraction ocr_image (regression guard for the cut-2 extraction).
    def failing_provider():
        raise ScreenToolError("SCREEN_OCR_DEPENDENCY_MISSING", "missing rapidocr")

    result = rapidocr.recognize_with_engine(
        failing_provider, Image.new("RGB", (8, 8), "white"), _RecordingLock()
    )
    assert result["error"]["code"] == "SCREEN_OCR_DEPENDENCY_MISSING"


def test_ocr_image_delegates_to_recognize_with_engine(monkeypatch):
    # ocr_image MUST be a thin caller over recognize_with_engine bound to the global
    # engine provider (_get_engine) + _INFER_LOCK (so both OCR paths share one
    # prepare/parse/error body).
    captured = {}

    def spy(engine_provider, image, lock):
        captured["provider"] = engine_provider
        captured["lock"] = lock
        return {"engine": "rapidocr", "raw_text": "spied", "blocks": [], "error": None}

    monkeypatch.setattr(rapidocr, "recognize_with_engine", spy)
    result = rapidocr.ocr_image(Image.new("RGB", (8, 8), "white"))

    assert result["raw_text"] == "spied"
    assert captured["provider"] is rapidocr._get_engine  # the global engine provider
    assert captured["lock"] is rapidocr._INFER_LOCK  # the shared cross-path lock
