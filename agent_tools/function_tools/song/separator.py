from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


def separate_vocals(
    input_path: str | Path,
    output_dir: str | Path,
    model_filename: str,
    output_format: str = "WAV",
    extra_kwargs: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    input_path = Path(input_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    before = {path.resolve() for path in output_dir.glob("*") if path.is_file()}
    separator = _build_separator(output_dir, output_format, extra_kwargs or {})
    separator.load_model(model_filename)
    produced = separator.separate(str(input_path))
    candidates = _candidate_paths(output_dir, produced, before)
    vocal_path, instrumental_path = _select_stems(candidates)
    return _copy_stem(vocal_path, output_dir / "vocals.wav"), _copy_stem(instrumental_path, output_dir / "instrumental.wav")


def _build_separator(output_dir: Path, output_format: str, extra_kwargs: dict[str, Any]) -> Any:
    try:
        from audio_separator.separator import Separator
    except Exception as exc:  # pragma: no cover - depends on local runtime deps
        raise RuntimeError("缺少 audio-separator，请先在运行环境安装 audio-separator。") from exc

    kwargs = {
        "output_dir": str(output_dir),
        "output_format": output_format,
    }
    kwargs.update(extra_kwargs)
    try:
        return Separator(**kwargs)
    except TypeError:
        kwargs.pop("output_format", None)
        return Separator(**kwargs)


def _candidate_paths(output_dir: Path, produced: Any, before: set[Path]) -> list[Path]:
    candidates: list[Path] = []
    if isinstance(produced, (list, tuple)):
        for item in produced:
            path = Path(str(item))
            if not path.is_absolute():
                path = output_dir / path
            if path.exists() and path.is_file():
                candidates.append(path)
    for path in output_dir.glob("*"):
        if path.is_file() and path.resolve() not in before and path not in candidates:
            candidates.append(path)
    return candidates


def _select_stems(paths: list[Path]) -> tuple[Path, Path]:
    if len(paths) < 2:
        raise RuntimeError("audio-separator 没有输出可用的人声/伴奏文件。")

    vocal_keywords = ("vocal", "vocals", "voice", "sing", "人声")
    instrumental_keywords = ("instrumental", "inst", "accompaniment", "karaoke", "no_vocals", "伴奏")

    instrumental = _find_by_keywords(paths, instrumental_keywords)
    vocal_candidates = [path for path in paths if path != instrumental]
    vocal = _find_by_keywords(vocal_candidates, vocal_keywords)
    if vocal is None:
        vocal = vocal_candidates[0] if vocal_candidates else None
    if instrumental is None:
        instrumental = next((path for path in paths if path != vocal), None)
    if vocal is None or instrumental is None:
        raise RuntimeError("无法识别 audio-separator 输出的人声/伴奏文件。")
    return vocal, instrumental


def _find_by_keywords(paths: list[Path], keywords: tuple[str, ...]) -> Path | None:
    for path in paths:
        name = path.name.lower()
        if any(keyword in name for keyword in keywords):
            return path
    return None


def _copy_stem(source: Path, target: Path) -> Path:
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)
    return target
