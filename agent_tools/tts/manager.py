from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_tools.config_io import read_config_file
from agent_tools.tts.adapters import CurrentGPTSoVITSAdapter, DummyTTSAdapter, TextOnlyTTSAdapter
from agent_tools.tts.base import TTSAdapter


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = BASE_DIR / "data" / "config" / "tts.yaml"
CURRENT_GPTSOVITS_PROVIDERS = {"gptsovits_current", "gptsovits", "current"}


def load_tts_config(config_path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    path = Path(config_path).resolve()
    config = read_config_file(path)
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

    if provider == "text_only":
        # tts.enabled=false assembly: ok results with no audio, zero VRAM.
        return TextOnlyTTSAdapter()

    raise ValueError(f"Unsupported TTS provider: {provider}")
