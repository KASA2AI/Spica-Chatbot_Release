import json
from io import BytesIO

from PIL import Image

from agent_tools.function_tools.screen.analyzer import (
    analyze_screen_attachment,
    analyze_screen_image_local,
    analyze_screen_png_local,
    get_last_screen_analysis_metadata,
)
from agent_tools.function_tools.screen.capture import ScreenCaptureResult
from agent_tools.function_tools.screen.config import ScreenPipelineConfig, load_screen_config
from agent_tools.function_tools.screen.schema import ScreenToolError
from agent_tools.function_tools.screen.tool import inspect_screen


class FakeMoondreamManager:
    def __init__(self, text="画面にはブラウザが表示されています。"):
        self.text = text
        self.calls = []

    def query(self, image, question, reasoning=False):
        self.calls.append({"image": image, "question": question, "reasoning": reasoning})
        return self.text


class FailingMoondreamManager:
    def query(self, image, question, reasoning=False):
        raise ScreenToolError("SCREEN_CUDA_UNAVAILABLE", "CUDA 不可用")


def make_config() -> ScreenPipelineConfig:
    return ScreenPipelineConfig(
        enabled=True,
        provider="moondream_local",
        model_id="vikhyatk/moondream2",
        revision="2025-06-21",
        device="cuda",
        dtype="bfloat16",
        max_side=768,
        reasoning=False,
        preload=False,
        ocr_enabled=True,
        ocr_engine="rapidocr",
        capture_format="png",
        infer_timeout_sec=30.0,
        log_timing=True,
        debug_save_images=False,
    )


def test_analyze_screen_image_local_uses_local_moondream_backend(monkeypatch):
    manager = FakeMoondreamManager()
    monkeypatch.setattr("agent_tools.function_tools.screen.analyzer.get_moondream_manager", lambda config: manager)
    monkeypatch.setattr(
        "agent_tools.function_tools.screen.analyzer.run_ocr",
        lambda image: {
            "engine": "rapidocr",
            "raw_text": "OCR SHOULD NOT ENTER MOONDREAM PROMPT",
            "blocks": [{"text": "OCR SHOULD NOT ENTER MOONDREAM PROMPT", "confidence": 0.99, "box": []}],
            "error": None,
        },
    )

    observation = analyze_screen_image_local(
        Image.new("RGB", (64, 48), "white"),
        "full_screen",
        "这是什么？",
        config=make_config(),
        question_type="general_observation",
        capture={"mode": "full_screen", "width": 64, "height": 48, "image_format": "png"},
        performance={"capture_ms": 1.0},
    )

    assert observation["schema_version"] == "screen_observation.v1"
    assert observation["visual_summary"]["engine"] == "moondream"
    assert observation["visible_text"]["raw_text"] == "OCR SHOULD NOT ENTER MOONDREAM PROMPT"
    assert observation["answer"]["direct_answer"] == "画面にはブラウザが表示されています。"
    assert observation["performance"]["capture_ms"] == 1.0
    assert observation["performance"]["ocr_ms"] >= 0
    assert observation["performance"]["moondream_ms"] >= 0
    assert manager.calls
    assert manager.calls[0]["image"].size == (64, 48)
    assert "OCR SHOULD NOT ENTER MOONDREAM PROMPT" not in manager.calls[0]["question"]

    metadata = get_last_screen_analysis_metadata()
    assert metadata["screen_analysis_engine"] == "moondream_local"
    assert metadata["screen_analysis_local"] is True
    assert metadata["screen_analysis_ocr_ms"] >= 0


def test_analyze_screen_image_local_outputs_visual_summary_and_visible_text(monkeypatch):
    manager = FakeMoondreamManager(text="local visual summary")
    monkeypatch.setattr("agent_tools.function_tools.screen.analyzer.get_moondream_manager", lambda config: manager)
    monkeypatch.setattr(
        "agent_tools.function_tools.screen.analyzer.run_ocr",
        lambda image: {
            "engine": "rapidocr",
            "raw_text": "File Edit View",
            "blocks": [{"text": "File Edit View", "confidence": 0.97, "box": [[0, 0], [10, 0]]}],
            "error": None,
        },
    )

    observation = analyze_screen_image_local(
        Image.new("RGB", (120, 80), "white"),
        "full_screen",
        "帮我看看屏幕",
        config=make_config(),
        performance={"capture_ms": 2.5},
    )

    assert observation["schema"] == "screen_observation.v1"
    assert observation["capture"]["mode"] == "full_screen"
    assert observation["capture"]["width"] == 120
    assert observation["capture"]["height"] == 80
    assert observation["visual_summary"]["text"] == "local visual summary"
    assert observation["visible_text"]["raw_text"] == "File Edit View"
    assert observation["visible_text"]["blocks"][0]["confidence"] == 0.97
    assert observation["performance"]["capture_ms"] == 2.5
    assert observation["performance"]["ocr_ms"] >= 0
    assert observation["performance"]["moondream_ms"] >= 0
    assert observation["performance"]["total_ms"] >= 0
    assert observation["errors"] == []


def test_analyze_screen_png_local_decodes_png_bytes(monkeypatch):
    manager = FakeMoondreamManager(text="png visual summary")
    monkeypatch.setattr("agent_tools.function_tools.screen.analyzer.get_moondream_manager", lambda config: manager)
    monkeypatch.setattr(
        "agent_tools.function_tools.screen.analyzer.run_ocr",
        lambda image: {"engine": "rapidocr", "raw_text": "PNG TEXT", "blocks": [], "error": None},
    )
    buffer = BytesIO()
    Image.new("RGB", (32, 16), "white").save(buffer, format="PNG")

    observation = analyze_screen_png_local(
        buffer.getvalue(),
        "region",
        "describe",
        config=make_config(),
    )

    assert observation["capture"]["mode"] == "region"
    assert observation["capture"]["image_format"] == "png"
    assert observation["capture"]["width"] == 32
    assert observation["capture"]["height"] == 16
    assert observation["visual_summary"]["text"] == "png visual summary"
    assert observation["visible_text"]["raw_text"] == "PNG TEXT"


def test_analyze_screen_attachment_decodes_png_and_uses_region_mode(monkeypatch):
    manager = FakeMoondreamManager(text="region visual summary")
    monkeypatch.setattr("agent_tools.function_tools.screen.analyzer.get_moondream_manager", lambda config: manager)
    monkeypatch.setattr(
        "agent_tools.function_tools.screen.analyzer.run_ocr",
        lambda image: {"engine": "rapidocr", "raw_text": "Region OCR", "blocks": [], "error": None},
    )
    buffer = BytesIO()
    Image.new("RGB", (48, 24), "white").save(buffer, format="PNG")

    observation = analyze_screen_attachment(
        attachment={
            "kind": "screen_capture",
            "target": "selected_region",
            "mode": "region",
            "source": "manual_region_selection",
            "created_at": "2026-06-06T00:00:00+00:00",
            "captured_at": "2026-06-06T00:00:00+00:00",
            "image_bytes": buffer.getvalue(),
            "mime_type": "image/png",
            "width": 48,
            "height": 24,
            "original_resolution": {"width": 48, "height": 24},
            "sent_resolution": {"width": 48, "height": 24},
            "downscaled": False,
            "format": "png",
            "quality": None,
            "region": {
                "screen_name": "primary",
                "screen_index": 0,
                "logical": {"x": 1, "y": 2, "width": 48, "height": 24},
                "physical": {"x": 1, "y": 2, "width": 48, "height": 24},
                "device_pixel_ratio": 1.0,
            },
        },
        user_question="这是什么？",
    )

    assert observation["request"]["target"] == "region"
    assert observation["capture"]["mode"] == "region"
    assert observation["capture"]["width"] == 48
    assert observation["capture"]["height"] == 24
    assert observation["capture"]["created_at"] == "2026-06-06T00:00:00+00:00"
    assert observation["capture"]["mime_type"] == "image/png"
    assert observation["capture"]["image_format"] == "png"
    assert observation["visual_summary"]["text"] == "region visual summary"
    assert observation["visible_text"]["raw_text"] == "Region OCR"


def test_analyze_screen_image_local_records_moondream_failure_without_losing_ocr(monkeypatch):
    monkeypatch.setattr("agent_tools.function_tools.screen.analyzer.get_moondream_manager", lambda config: FailingMoondreamManager())
    monkeypatch.setattr(
        "agent_tools.function_tools.screen.analyzer.run_ocr",
        lambda image: {"engine": "rapidocr", "raw_text": "OCR survives", "blocks": [], "error": None},
    )

    observation = analyze_screen_image_local(
        Image.new("RGB", (64, 48), "white"),
        "full_screen",
        "帮我看看屏幕",
        config=make_config(),
    )

    assert observation["visual_summary"]["text"] == ""
    assert observation["visible_text"]["raw_text"] == "OCR survives"
    assert observation["answer"]["direct_answer"] == ""
    assert observation["errors"][0]["stage"] == "moondream"
    assert observation["errors"][0]["code"] == "SCREEN_CUDA_UNAVAILABLE"
    assert observation["errors"][0]["recoverable"] is True


def test_analyze_screen_image_local_keeps_moondream_result_when_ocr_fails(monkeypatch):
    manager = FakeMoondreamManager(text="visual answer")
    monkeypatch.setattr("agent_tools.function_tools.screen.analyzer.get_moondream_manager", lambda config: manager)
    monkeypatch.setattr(
        "agent_tools.function_tools.screen.analyzer.run_ocr",
        lambda image: {
            "engine": "rapidocr",
            "raw_text": "",
            "blocks": [],
            "error": {
                "stage": "ocr",
                "code": "SCREEN_OCR_FAILED",
                "message": "ocr boom",
                "recoverable": True,
            },
        },
    )

    observation = analyze_screen_image_local(
        Image.new("RGB", (64, 48), "white"),
        "full_screen",
        "帮我看看屏幕",
        config=make_config(),
        question_type="general_observation",
        capture={"mode": "full_screen", "width": 64, "height": 48, "image_format": "png"},
    )

    assert observation["answer"]["direct_answer"] == "visual answer"
    assert observation["visible_text"]["raw_text"] == ""
    assert observation["errors"][0]["stage"] == "ocr"
    assert observation["errors"][0]["code"] == "SCREEN_OCR_FAILED"
    assert observation["errors"][0]["recoverable"] is True


def test_load_screen_config_ignores_legacy_remote_env(monkeypatch, tmp_path):
    config_path = tmp_path / "screen_vision_config.json"
    config_path.write_text(json.dumps({"provider": "moondream_local", "revision": "2025-06-21"}), encoding="utf-8")

    config = load_screen_config(config_path)

    assert config.provider == "moondream_local"
    assert config.model_id == "vikhyatk/moondream2"
    assert config.revision == "2025-06-21"


def test_inspect_screen_captures_pil_and_calls_local_analyzer(monkeypatch):
    image = Image.new("RGB", (80, 60), "white")
    calls = []

    def fake_capture_full_screen():
        return ScreenCaptureResult(
            image=image,
            metadata={"captured_scope": "full_screen", "source": "automatic_screenshot", "region": None},
        )

    def fake_analyze_screen_image_local(received_image, mode, prompt, **kwargs):
        calls.append({"image": received_image, "mode": mode, "prompt": prompt, **kwargs})
        assert received_image is image
        assert kwargs["config"].provider == "moondream_local"
        return {
            "schema_version": "screen_observation.v1",
            "type": "screen_observation",
            "request": {"target": mode, "user_question": prompt},
            "capture": {"captured_scope": "full_screen", "source": "automatic_screenshot"},
            "answer": {"direct_answer": "local result", "confidence": 0.0},
            "followup": {"context_for_next_turn": "local result", "needs_followup_capture": False, "suggested_capture": None},
        }

    monkeypatch.setattr("agent_tools.function_tools.screen.tool.capture_full_screen", fake_capture_full_screen)
    monkeypatch.setattr("agent_tools.function_tools.screen.tool.analyze_screen_image_local", fake_analyze_screen_image_local)

    result = json.loads(inspect_screen(target="full_screen", question="帮我看看屏幕"))

    assert result["ok"] is True
    assert result["data"]["schema_version"] == "screen_observation.v1"
    assert result["data"]["answer"]["direct_answer"] == "local result"
    assert calls
    assert calls[0]["mode"] == "full_screen"
    assert calls[0]["prompt"] == "帮我看看屏幕"
    assert calls[0]["performance"]["capture_ms"] >= 0


def test_inspect_screen_returns_observation_errors_from_local_analyzer(monkeypatch):
    image = Image.new("RGB", (80, 60), "white")

    def fake_capture_full_screen():
        return ScreenCaptureResult(
            image=image,
            metadata={"captured_scope": "full_screen", "source": "automatic_screenshot", "region": None},
        )

    def fake_analyze_screen_image_local(received_image, mode, prompt, **kwargs):
        assert received_image is image
        return {
            "schema": "screen_observation.v1",
            "schema_version": "screen_observation.v1",
            "type": "screen_observation",
            "source": "screen",
            "request": {"target": mode, "user_question": prompt},
            "capture": {"captured_scope": "full_screen", "source": "automatic_screenshot"},
            "visual_summary": {"engine": "moondream", "text": ""},
            "visible_text": {"engine": "rapidocr", "raw_text": "OCR survives", "blocks": []},
            "errors": [
                {
                    "stage": "moondream",
                    "code": "SCREEN_CUDA_UNAVAILABLE",
                    "message": "CUDA 不可用",
                    "recoverable": True,
                }
            ],
            "answer": {"direct_answer": "", "confidence": 0.0},
            "followup": {"context_for_next_turn": "", "needs_followup_capture": False, "suggested_capture": None},
        }

    monkeypatch.setattr("agent_tools.function_tools.screen.tool.capture_full_screen", fake_capture_full_screen)
    monkeypatch.setattr("agent_tools.function_tools.screen.tool.analyze_screen_image_local", fake_analyze_screen_image_local)

    result = json.loads(inspect_screen(target="full_screen", question="帮我看看屏幕"))

    assert result["ok"] is True
    assert result["data"]["visible_text"]["raw_text"] == "OCR survives"
    assert result["data"]["errors"][0]["code"] == "SCREEN_CUDA_UNAVAILABLE"
