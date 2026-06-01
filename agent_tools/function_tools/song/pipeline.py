from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from agent_tools.function_tools.song.config import ensure_song_dirs, load_song_config
from agent_tools.function_tools.song.mixer import mix_vocal_with_instrumental, trim_audio_file
from agent_tools.function_tools.song.models import CancellationToken, SongJobCancelled, SongJobResult, SongRequest
from agent_tools.function_tools.song.netease import download_audio, extension_from_url, get_audio_url, search_best_song
from agent_tools.function_tools.song.rvc import infer_spica_vocal
from agent_tools.function_tools.song.separator import separate_vocals


class SongPipeline:
    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config = load_song_config(config_path)
        self.dirs = ensure_song_dirs(self.config)

    def run(self, request: SongRequest, cancellation: CancellationToken | None = None) -> SongJobResult:
        cancellation = cancellation or CancellationToken()
        try:
            return self._run(request, cancellation)
        except SongJobCancelled:
            return SongJobResult(ok=False, message="已取消唱歌。", error="cancelled")
        except Exception as exc:
            return SongJobResult(ok=False, message=f"唱歌失败：{exc}", error=str(exc))

    def _run(self, request: SongRequest, cancellation: CancellationToken) -> SongJobResult:
        if not bool(self.config.get("enabled", True)):
            raise RuntimeError("唱歌功能已在 song_config.json 中关闭。")
        cancellation.throw_if_cancelled()

        search_config = self.config.get("search", {})
        song = search_best_song(request, limit=int(search_config.get("limit", 20)))
        cancellation.throw_if_cancelled()

        url = get_audio_url(song.song_id, bitrate=int(search_config.get("bitrate", 320000)))
        original_path = self._original_path(song.song_id, extension_from_url(url))
        if not request.prefer_cache or not original_path.exists():
            download_config = self.config.get("download", {})
            download_audio(
                url,
                original_path,
                timeout_sec=int(download_config.get("timeout_sec", 60)),
                user_agent=str(download_config.get("user_agent") or ""),
            )
        cancellation.throw_if_cancelled()

        separator_config = self.config.get("separator", {})
        separator_params = self._separator_params(separator_config)
        separator_model = str(separator_params["model_filename"])
        separated_key = _stable_hash({"song_id": song.song_id, "separator": separator_params})
        separated_dir = self.dirs["separated"] / separated_key
        vocal_path = separated_dir / "vocals.wav"
        instrumental_path = separated_dir / "instrumental.wav"
        if not request.prefer_cache or not vocal_path.exists() or not instrumental_path.exists():
            if separated_dir.exists() and not request.prefer_cache:
                shutil.rmtree(separated_dir)
            tmp_separated = self.dirs["tmp"] / f"{uuid.uuid4().hex}_separated"
            tmp_separated.mkdir(parents=True, exist_ok=True)
            try:
                tmp_vocal, tmp_instrumental = separate_vocals(
                    original_path,
                    tmp_separated,
                    model_filename=separator_model,
                    output_format=str(separator_config.get("output_format") or "WAV"),
                    extra_kwargs=separator_config.get("extra_kwargs") if isinstance(separator_config.get("extra_kwargs"), dict) else {},
                )
                if bool(separator_params.get("swap_stems")):
                    tmp_vocal, tmp_instrumental = tmp_instrumental, tmp_vocal
                separated_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(tmp_vocal, vocal_path)
                shutil.copyfile(tmp_instrumental, instrumental_path)
            finally:
                shutil.rmtree(tmp_separated, ignore_errors=True)
        cancellation.throw_if_cancelled()

        rvc_config = self._voice_config(request.voice_model)
        model_path = Path(str(rvc_config["model_path"]))
        index_path = str(rvc_config.get("index_path") or "") or None
        if not model_path.exists():
            raise RuntimeError(f"RVC 模型不存在：{model_path}")
        if index_path and not Path(index_path).exists():
            index_path = None
        rvc_model_hash = _file_hash(model_path)
        rvc_params = self._rvc_params(rvc_config, request.max_duration_sec)
        rvc_key = _stable_hash(
            {
                "song_id": song.song_id,
                "separator": separator_params,
                "rvc_model_hash": rvc_model_hash,
                "rvc_params": rvc_params,
            }
        )
        rvc_path = self.dirs["rvc"] / f"{rvc_key}.wav"
        if not request.prefer_cache or not rvc_path.exists():
            tmp_vocal = self.dirs["tmp"] / f"{uuid.uuid4().hex}_vocal.wav"
            prepared_vocal = trim_audio_file(vocal_path, tmp_vocal, request.max_duration_sec)
            try:
                infer_spica_vocal(
                    input_vocal_path=str(prepared_vocal),
                    output_vocal_path=str(rvc_path),
                    model_path=str(model_path),
                    index_path=index_path,
                    applio_root=str(self.config["applio_root"]),
                    **rvc_params,
                )
            finally:
                if prepared_vocal == tmp_vocal:
                    tmp_vocal.unlink(missing_ok=True)
        cancellation.throw_if_cancelled()

        mix_params = dict(self.config.get("mix", {}))
        final_key = _stable_hash(
            {
                "song_id": song.song_id,
                "separator": separator_params,
                "rvc_model_hash": rvc_model_hash,
                "rvc_params": rvc_params,
                "mix_params": mix_params,
            }
        )
        final_path = self.dirs["final"] / f"{final_key}.wav"
        metadata = {
            "song_id": song.song_id,
            "title": song.title,
            "artist": song.artist_text,
            "album": song.album,
            "original_path": str(original_path),
            "vocal_path": str(vocal_path),
            "instrumental_path": str(instrumental_path),
            "rvc_vocal_path": str(rvc_path),
            "final_audio_path": str(final_path),
            "separator_model": separator_model,
            "separator_params": separator_params,
            "rvc_model_hash": rvc_model_hash,
            "rvc_params": rvc_params,
            "mix_params": mix_params,
            "search_score": song.score,
        }
        if not request.prefer_cache or not final_path.exists():
            mix_vocal_with_instrumental(
                rvc_path,
                instrumental_path,
                final_path,
                mix_params,
                max_duration_sec=request.max_duration_sec,
            )
            _write_json(final_path.with_suffix(".json"), metadata)

        return SongJobResult(
            ok=True,
            final_audio_path=str(final_path),
            song_id=song.song_id,
            title=song.title,
            artist=song.artist_text,
            message=f"唱歌中：{song.display_name()}",
            metadata=metadata,
        )

    def _original_path(self, song_id: str, extension: str) -> Path:
        return self.dirs["original"] / f"{song_id}{extension}"

    def _voice_config(self, voice_model: str) -> dict[str, Any]:
        rvc = self.config.get("rvc", {})
        voices = rvc.get("voices") if isinstance(rvc.get("voices"), dict) else {}
        name = voice_model or str(rvc.get("voice_model") or "spica")
        config = voices.get(name) or voices.get(str(rvc.get("voice_model") or "spica"))
        if not isinstance(config, dict):
            raise RuntimeError(f"找不到 RVC 声线配置：{name}")
        return config

    def _separator_params(self, separator_config: dict[str, Any]) -> dict[str, Any]:
        extra_kwargs = separator_config.get("extra_kwargs")
        if not isinstance(extra_kwargs, dict):
            extra_kwargs = {}
        return {
            "model_filename": str(separator_config.get("model_filename") or ""),
            "output_format": str(separator_config.get("output_format") or "WAV"),
            "swap_stems": bool(separator_config.get("swap_stems", False)),
            "extra_kwargs": extra_kwargs,
        }

    def _rvc_params(self, rvc_config: dict[str, Any], max_duration_sec: int) -> dict[str, Any]:
        allowed = {
            "f0_method",
            "transpose",
            "index_rate",
            "protect",
            "device",
            "volume_envelope",
            "split_audio",
            "f0_autotune",
            "f0_autotune_strength",
            "proposed_pitch",
            "proposed_pitch_threshold",
            "clean_audio",
            "clean_strength",
            "export_format",
            "embedder_model",
            "embedder_model_custom",
            "reference_audio_dir",
            "sid",
        }
        params = {key: rvc_config[key] for key in allowed if key in rvc_config}
        params["max_duration_sec"] = max_duration_sec
        return params


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
