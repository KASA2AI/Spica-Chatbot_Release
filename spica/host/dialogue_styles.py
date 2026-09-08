"""Validate and install small, immutable dialogue style folders."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
import tempfile

from PIL import Image

from spica.core.character_manifest import package_file
from spica.core.dialogue_style import DialogueStyle, LoadedDialogueStyle

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024




def load_dialogue_style(root: str | Path) -> LoadedDialogueStyle:
    root = Path(root).resolve()
    manifest = package_file(root, "style.json")
    if manifest.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("样式配置过大。")
    style = DialogueStyle.model_validate_json(manifest.read_bytes())
    assets = {}
    for key, relative in style.images.items():
        path = package_file(root, relative)
        size = path.stat().st_size
        if not 0 < size <= MAX_IMAGE_BYTES:
            raise ValueError("样式 PNG 不能超过 8 MiB。")
        try:
            with Image.open(path) as im:
                if im.format != "PNG" or max(im.size) > 4096 or im.width * im.height > 8_000_000:
                    raise ValueError("样式图片必须为尺寸不超过 4096、总像素不超过 800 万的 PNG。")
                if getattr(im, "n_frames", 1) != 1:
                    raise ValueError("句尾动画请使用 PNG 图集。")
                im.load()
                if key == "tail" and (im.width % style.tail.columns or im.height % style.tail.rows):
                    raise ValueError("句尾动画图集尺寸与行列数不符。")
        except (Image.DecompressionBombError, SyntaxError) as exc:
            raise ValueError("样式 PNG 无效或像素过多。") from exc
        assets[key] = {"path": relative, "bytes": size,
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    canonical = json.dumps({"style": style.model_dump(mode="json"), "assets": assets},
                           sort_keys=True, separators=(",", ":")).encode()
    return LoadedDialogueStyle(root, style, hashlib.sha256(canonical).hexdigest(), assets)


def import_dialogue_style(source: str | Path, installed_root: Path) -> LoadedDialogueStyle:
    loaded = load_dialogue_style(source)
    destination = installed_root / loaded.style.style_id / loaded.revision
    if destination.is_symlink():
        raise ValueError("样式安装目录不能是符号链接。")
    if destination.exists():
        try:
            existing = load_dialogue_style(destination)
            if existing.revision == loaded.revision:
                return existing
        except (OSError, ValueError):
            pass  # Rebuild a damaged installation from the validated source.
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".import-", dir=destination.parent) as temp:
        root = Path(temp) / "package"
        (root / "images").mkdir(parents=True)
        (root / "style.json").write_text(loaded.style.model_dump_json(indent=2), encoding="utf-8")
        for relative in loaded.style.images.values():
            (root / relative).write_bytes(package_file(loaded.root, relative).read_bytes())
        verified = load_dialogue_style(root)
        if verified.revision != loaded.revision:
            raise ValueError("导入过程中样式文件发生变化，请重试。")
        # Keep the backup outside TemporaryDirectory: if restoration itself
        # fails, automatic cleanup must not delete the recoverable old copy.
        previous = Path(temp).with_name(Path(temp).name + ".previous")
        if destination.exists():
            destination.rename(previous)
        try:
            root.rename(destination)
        except BaseException:
            if previous.exists():
                previous.rename(destination)
            raise
        if previous.exists():
            shutil.rmtree(previous, ignore_errors=True)
    return load_dialogue_style(destination)


def selected_dialogue_style(package_dir: str | None, installed_root: Path) -> LoadedDialogueStyle | None:
    if package_dir is None:
        return None
    root = Path(package_dir).resolve()
    loaded = load_dialogue_style(root)
    expected = (installed_root / loaded.style.style_id / loaded.revision).resolve()
    if root != expected:
        raise ValueError("请先通过设置导入对话框样式文件夹。")
    return loaded
