from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parents[2]
DEFAULT_CONFIG_PATH = PACKAGE_DIR / "song_config.json"


DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "generated_root": "static/generated_song",
    "applio_root": "agent_tools/function_tools/song/Applio",
    "search": {
        "limit": 20,
        "bitrate": 320000,
    },
    "download": {
        "timeout_sec": 60,
        "user_agent": "Mozilla/5.0 Spica-Chatbot SongTool",
    },
    "separator": {
        "model_filename": "UVR-MDX-NET-Inst_HQ_3.onnx",
        "output_format": "WAV",
        "swap_stems": True,
        "extra_kwargs": {},
    },
    "rvc": {
        "voice_model": "spica",
        "voices": {
            "spica": {
                "model_path": "agent_tools/function_tools/song/Applio/logs/spica/spica_200e_57000s.pth",
                "index_path": "agent_tools/function_tools/song/Applio/logs/spica/spica.index",
                "f0_method": "rmvpe",
                "transpose": 0,
                "index_rate": 0.75,
                "protect": 0.33,
                "device": "cuda",
                "volume_envelope": 1.0,
                "split_audio": False,
                "f0_autotune": False,
                "f0_autotune_strength": 1.0,
                "proposed_pitch": False,
                "proposed_pitch_threshold": 155.0,
                "clean_audio": False,
                "clean_strength": 0.5,
                "export_format": "WAV",
                "embedder_model": "contentvec",
                "embedder_model_custom": None,
                "reference_audio_dir": "spica_data/voice/happy/refs",
                "sid": 0,
            }
        },
    },
    "mix": {
        "instrumental_gain": 0.88,
        "vocal_gain": 1.0,
        "normalize_peak": 0.95,
        "output_subtype": "PCM_16",
    },
    "intent": {
        "enabled": True,
        "thresholds": {
            "direct_execute": 0.9,
            "confirm": 0.7,
            "llm_fallback_min": 0.45,
            "llm_fallback_max": 0.75,
        },
        "llm_fallback": {
            "enabled": True,
            "provider": "openai_compatible",
            "model": "gpt-4.1-nano",
            "api_key_env": "SONG_INTENT_OPENAI_API_KEY",
            "base_url_env": "SONG_INTENT_OPENAI_BASE_URL",
            "timeout_sec": 3,
            "max_tokens": 180,
            "temperature": 0,
        },
        "safety": {
            "never_execute_without_song_object": True,
            "llm_can_only_classify": True,
            "llm_can_start_pipeline": False,
        },
    },
}


def load_song_config(config_path: str | Path | None = None) -> dict[str, Any]:
    config = copy.deepcopy(DEFAULT_CONFIG)
    path = Path(config_path).expanduser() if config_path else DEFAULT_CONFIG_PATH
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    if path.exists():
        with path.open("r", encoding="utf-8") as file:
            _deep_update(config, json.load(file))
    config["_config_path"] = str(path)
    return _resolve_config_paths(config)


def ensure_song_dirs(config: dict[str, Any]) -> dict[str, Path]:
    root = Path(str(config["generated_root"]))
    cache = root / "cache"
    dirs = {
        "root": root,
        "cache": cache,
        "original": cache / "original",
        "separated": cache / "separated",
        "rvc": cache / "rvc",
        "final": cache / "final",
        "tmp": root / "tmp",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def _deep_update(base: dict[str, Any], override: dict[str, Any]) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value


def _resolve_config_paths(config: dict[str, Any]) -> dict[str, Any]:
    config["generated_root"] = str(_resolve_project_path(config.get("generated_root")))
    config["applio_root"] = str(_resolve_project_path(config.get("applio_root")))
    voices = config.get("rvc", {}).get("voices", {})
    if isinstance(voices, dict):
        for voice in voices.values():
            if not isinstance(voice, dict):
                continue
            for key in ("model_path", "index_path", "embedder_model_custom", "reference_audio_dir"):
                value = voice.get(key)
                if value:
                    voice[key] = str(_resolve_project_path(value))
    return config


def _resolve_project_path(value: Any) -> Path:
    path = Path(str(value or "")).expanduser()
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()
