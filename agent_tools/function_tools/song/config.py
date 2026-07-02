from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parents[2]
DEFAULT_CONFIG_PATH = PACKAGE_DIR / "song_config.json"

logger = logging.getLogger(__name__)


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
        # cut 3 Phase 1A: RVC execution seam. Default in_process = byte-identical to
        # before (legacy in-process Applio load); subprocess isolates the Applio
        # import tree out of the caller process. worker_python None -> the current
        # interpreter (Phase 2 points it at an independent RVC env). These are
        # siblings of voices (NOT voice params), so _voice_config / _rvc_params are
        # untouched. Flipping the default to subprocess is Phase 1B.
        "execution_mode": "in_process",
        "worker_python": None,
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
    # B2 (P2): the "intent" section (thresholds / llm_fallback / decorative
    # safety switches) died with the pre-chat hijack stack -- singing is now the
    # main LLM's sing_song function call; only the control fast path remains.
}


def _compose_song_config(override: dict[str, Any], config_path_label: str) -> dict[str, Any]:
    """The ONE composition engine (P0b step 3): deep-merge an override dict over
    DEFAULT_CONFIG, then resolve paths. Shared by the legacy json loader and the
    app.yaml chain so the two carriers cannot drift in merge semantics."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    _deep_update(config, override)
    config["_config_path"] = config_path_label
    return _resolve_config_paths(config)


def load_song_config(config_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(config_path).expanduser() if config_path else DEFAULT_CONFIG_PATH
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    override: dict[str, Any] = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as file:
            override = json.load(file)
    return _compose_song_config(override, str(path))


def resolve_effective_song_config(
    config: Any | None = None,
    legacy_path: str | Path | None = None,
) -> dict[str, Any]:
    """P0b step 3 (D6): carrier switch for the song section -- one WHOLE chain
    by legacy-file existence (json present -> old chain + WARNING; absent ->
    app.yaml's ``song:`` override dict through the same composition engine).
    The override is deep-copied so composition can never mutate AppConfig state
    (path resolution rewrites nested voice dicts in place)."""
    path = Path(legacy_path).expanduser() if legacy_path is not None else DEFAULT_CONFIG_PATH
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    if path.exists():
        logger.warning(
            "song 配置仍由旧载体 %s 生效(整条旧链 json>defaults)；"
            "已迁移至 data/config/app.yaml 体系，请运行 scripts/migrate_config_p0b.py，"
            "下一版本停读旧 json",
            path,
        )
        return load_song_config(path)
    if config is None:
        from spica.config.manager import ConfigManager

        config = ConfigManager().load()
    return _compose_song_config(
        copy.deepcopy(config.song or {}), str(Path("data/config/app.yaml")) + "#song"
    )


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
