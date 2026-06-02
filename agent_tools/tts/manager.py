from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_tools.tts.adapters import CurrentGPTSoVITSAdapter, DummyTTSAdapter
from agent_tools.tts.base import TTSAdapter


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = BASE_DIR / "config" / "tts_config.json"
CURRENT_GPTSOVITS_PROVIDERS = {"gptsovits_current", "gptsovits", "current"}


def load_tts_config(config_path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    path = Path(config_path).resolve()
    with path.open("r", encoding="utf-8") as file:
        config = json.load(file)
    config.setdefault("_config_path", str(path))
    return config


def build_tts_adapter(config: dict[str, Any] | None = None, service: Any | None = None) -> TTSAdapter:
    config = config or {}
    provider = str(config.get("provider") or config.get("tts_provider") or "gptsovits_current")

    if provider in CURRENT_GPTSOVITS_PROVIDERS:
        config_path = config.get("_config_path") or config.get("config_path")
        return CurrentGPTSoVITSAdapter(service=service, config_path=str(config_path) if config_path else None)

    if provider == "dummy":
        audio_path = config.get("audio_path") or config.get("dummy_audio_path")
        if audio_path:
            audio_path = str(Path(audio_path).expanduser())
        return DummyTTSAdapter(audio_path=audio_path, audio_url=config.get("audio_url"))

    raise ValueError(f"Unsupported TTS provider: {provider}")
