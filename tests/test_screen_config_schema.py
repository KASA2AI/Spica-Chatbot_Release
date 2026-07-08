import json

from agent_tools.function_tools.screen.config import load_screen_config
from agent_tools.function_tools.screen.schema import (
    build_screen_observation,
    compact_screen_observation_for_prompt,
    screen_observation_context_for_next_turn,
)


def test_load_screen_config_uses_local_fields_and_ignores_remote_keys(tmp_path, monkeypatch):
    config_path = tmp_path / "screen_vision_config.json"
    config_path.write_text(
        json.dumps(
            {
                "provider": "moondream_local",
                "model_id": "vikhyatk/moondream2",
                "revision": "2025-06-21",
                "device": "cuda",
                "dtype": "bfloat16",
                "max_side": 1024,
                "ocr_enabled": True,
                "capture_format": "png",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SPICA_SCREEN_MODEL_ID", "local/test-model")
    monkeypatch.delenv("SPICA_SCREEN_MAX_SIDE", raising=False)

    config = load_screen_config(config_path)

    assert config.provider == "moondream_local"
    assert config.model_id == "local/test-model"
    assert config.revision == "2025-06-21"
    assert config.device == "cuda"
    assert config.dtype == "bfloat16"
    assert config.max_side == 1024
    assert config.capture_format == "png"

def test_build_screen_observation_keeps_legacy_schema_and_adds_local_fields():
    observation = build_screen_observation(
        user_question="看一下屏幕",
        question_type="general_observation",
        target="full_screen",
        capture={
            "mode": "full_screen",
            "width": 1920,
            "height": 1080,
            "created_at": "2026-06-06T00:00:00+00:00",
            "image_format": "png",
        },
        visual_summary={"text": "A browser is visible."},
        visible_text={"raw_text": "FULL OCR SHOULD STAY OUT OF COMPACT PROMPT"},
        performance={"capture_ms": 1.2, "ocr_ms": 2.3, "moondream_ms": 3.4, "total_ms": 6.9},
        errors=[{"stage": "ocr", "code": "OCR_FAILED", "message": "ocr unavailable", "recoverable": True}],
    )

    assert observation["schema"] == "screen_observation.v1"
    assert observation["schema_version"] == "screen_observation.v1"
    assert observation["source"] == "screen"
    assert observation["capture"]["mode"] == "full_screen"
    assert observation["capture"]["image_format"] == "png"
    assert observation["visual_summary"]["engine"] == "moondream"
    assert observation["visible_text"]["engine"] == "rapidocr"
    assert observation["performance"]["total_ms"] == 6.9
    assert observation["errors"][0]["recoverable"] is True
    assert observation["answer"]["direct_answer"] == "A browser is visible."

    compact = compact_screen_observation_for_prompt(observation)
    assert compact["schema_version"] == "screen_observation.v1"
    assert compact["visual_summary"]["text"] == "A browser is visible."
    assert "FULL OCR SHOULD STAY OUT OF COMPACT PROMPT" not in json.dumps(compact, ensure_ascii=False)
    assert screen_observation_context_for_next_turn(observation).endswith("A browser is visible.")
