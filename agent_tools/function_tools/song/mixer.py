from __future__ import annotations

from pathlib import Path
from typing import Any


def trim_audio_file(input_path: str | Path, output_path: str | Path, max_duration_sec: int | None) -> Path:
    input_path = Path(input_path)
    output_path = Path(output_path)
    if not max_duration_sec or max_duration_sec <= 0:
        return input_path

    sf = _soundfile()
    info = sf.info(str(input_path))
    max_frames = int(info.samplerate * max_duration_sec)
    if info.frames <= max_frames:
        return input_path

    output_path.parent.mkdir(parents=True, exist_ok=True)
    data, sample_rate = sf.read(str(input_path), frames=max_frames, always_2d=True)
    sf.write(str(output_path), data, sample_rate)
    return output_path


def mix_vocal_with_instrumental(
    vocal_path: str | Path,
    instrumental_path: str | Path,
    output_path: str | Path,
    mix_params: dict[str, Any],
    max_duration_sec: int | None = None,
) -> Path:
    np = _numpy()
    sf = _soundfile()
    vocal, vocal_sr = sf.read(str(vocal_path), always_2d=True)
    instrumental, instrumental_sr = sf.read(str(instrumental_path), always_2d=True)

    target_sr = instrumental_sr
    if vocal_sr != target_sr:
        vocal = _resample(vocal, vocal_sr, target_sr, np)

    channels = max(vocal.shape[1], instrumental.shape[1])
    vocal = _match_channels(vocal, channels, np)
    instrumental = _match_channels(instrumental, channels, np)

    max_len = max(len(vocal), len(instrumental))
    if max_duration_sec and max_duration_sec > 0:
        max_len = min(max_len, int(target_sr * max_duration_sec))
    vocal = _pad_or_trim(vocal, max_len, np)
    instrumental = _pad_or_trim(instrumental, max_len, np)

    mixed = (
        instrumental * float(mix_params.get("instrumental_gain", 0.88))
        + vocal * float(mix_params.get("vocal_gain", 1.0))
    )
    normalize_peak = float(mix_params.get("normalize_peak", 0.95))
    peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
    if peak > normalize_peak > 0:
        mixed = mixed / peak * normalize_peak

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(output_path), mixed, target_sr, subtype=str(mix_params.get("output_subtype") or "PCM_16"))
    return output_path


def _match_channels(audio: Any, channels: int, np: Any) -> Any:
    if audio.shape[1] == channels:
        return audio
    if audio.shape[1] == 1:
        return np.repeat(audio, channels, axis=1)
    return audio[:, :channels]


def _pad_or_trim(audio: Any, length: int, np: Any) -> Any:
    if len(audio) == length:
        return audio
    if len(audio) > length:
        return audio[:length]
    pad = np.zeros((length - len(audio), audio.shape[1]), dtype=audio.dtype)
    return np.concatenate([audio, pad], axis=0)


def _resample(audio: Any, source_sr: int, target_sr: int, np: Any) -> Any:
    if source_sr == target_sr or len(audio) == 0:
        return audio
    source_x = np.arange(len(audio), dtype=np.float64)
    target_len = max(1, round(len(audio) * target_sr / source_sr))
    target_x = np.linspace(0, max(0, len(audio) - 1), target_len, dtype=np.float64)
    channels = [np.interp(target_x, source_x, audio[:, channel]) for channel in range(audio.shape[1])]
    return np.stack(channels, axis=1).astype(audio.dtype, copy=False)


def _numpy() -> Any:
    try:
        import numpy as np
    except Exception as exc:  # pragma: no cover - depends on local runtime deps
        raise RuntimeError("缺少 numpy，请先在运行环境安装 numpy。") from exc
    return np


def _soundfile() -> Any:
    try:
        import soundfile as sf
    except Exception as exc:  # pragma: no cover - depends on local runtime deps
        raise RuntimeError("缺少 soundfile，请先在运行环境安装 soundfile。") from exc
    return sf
