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
