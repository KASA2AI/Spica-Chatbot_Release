"""Install portable character folders and adapt them to existing runtime loaders."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from agent_tools.config_io import read_config_file, write_config_file

from spica.core.character import CharacterPackage, load_character_package
from spica.core.eye_rig import load_eye_rig
from spica.core.character_manifest import (
    CharacterManifest,
    ROLE_FILES,
    Scene,
    package_file,
)

_EMOTIONS = {
    "happy": ("002", "joy"),
    "angry": ("013", "anger"),
    "sad": ("010", "sad"),
    "surprised": ("009", "surprise"),
}
_ROOT = Path(__file__).resolve().parents[2]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _source_files(root: Path, manifest: CharacterManifest) -> list[str]:
    paths = manifest.files(root) | {"meta.json"}
    paths.update(
        name
        for name in (*ROLE_FILES, "README.md", "LICENSE", "sources.tsv")
        if (root / name).exists()
    )
    for value in paths:
        package_file(root, value)
        if value == ".installed.json" or value.startswith(".presentation/"):
            raise ValueError("角色源文件不能使用安装目录保留名称。")
    if len({value.casefold() for value in paths}) != len(paths):
        raise ValueError("角色文件路径在 Windows 上重名。")
    return sorted(paths)


def import_character_folder(
    source: str | Path, characters_root: Path
) -> CharacterPackage:
    """Stage a closed file set, then publish a content-addressed installation.

    Healthy installations and personal data are retained; damaged installations
    can be repaired from the same source. The source may be removed after success.
    No weight deserialization or renderer imports occur.
    """
    source = Path(source).resolve()
    package = load_character_package(source)
    manifest = package.manifest
    if manifest is None:
        raise ValueError("请选择含 pack_format: 1、2 或 3 的 meta.json 角色文件夹。")
    _visual_rules(source, manifest)
    if characters_root.is_dir() and any(
        child.name.casefold() == manifest.slug.casefold() and child.name != manifest.slug
        for child in characters_root.iterdir()
    ):
        raise ValueError("角色 ID 与已安装角色仅大小写不同，请使用一致的 ID。")
    weights = []
    if manifest.tts:
        weights.extend(((manifest.tts.gpt, ".ckpt"), (manifest.tts.sovits, ".pth")))
        import soundfile as sf

        for reference in [manifest.tts.reference, *manifest.tts.emotions.values()]:
            for audio in [reference.audio, *reference.additional_audio]:
                info = sf.info(str(package_file(source, audio)))
                if info.frames <= 0 or info.samplerate <= 0:
                    raise ValueError("角色参考音频为空。")
    if manifest.rvc:
        weights.append((manifest.rvc.model, ".pth"))
        if manifest.rvc.index:
            weights.append((manifest.rvc.index, ".index"))
    for value, suffix in weights:
        path = package_file(source, value)
        if path.suffix.lower() != suffix or path.stat().st_size == 0:
            raise ValueError(f"角色模型文件为空或格式不匹配：{value}")
    if manifest.memory_file:
        from spica.core.character_memory import read_character_save

        read_character_save(package_file(source, manifest.memory_file), manifest.slug)
    characters_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".import-", dir=characters_root
    ) as temporary:
        staging = Path(temporary) / "package"
        staging.mkdir()
        digest = hashlib.sha256(b"spica.character.v1\0")
        for relative in _source_files(source, manifest):
            original = package_file(source, relative)
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest.update(relative.encode("utf-8") + b"\0")
            file_digest = hashlib.sha256()
            with original.open("rb") as reader, destination.open("xb") as writer:
                while chunk := reader.read(1024 * 1024):
                    writer.write(chunk)
                    file_digest.update(chunk)
            digest.update(file_digest.digest())
        revision = digest.hexdigest()
        # Verify/derive images before either installation or selection changes.
        _build_presentation(staging, manifest, revision)
        _write_json(staging / ".installed.json", {"revision": revision})
        load_character_package(staging)
        target = characters_root / manifest.slug / revision
        if target.parent.is_symlink() or target.is_symlink():
            raise ValueError("角色安装目录不能是符号链接。")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            staging.rename(target)
        elif not _installation_matches(staging, target):
            # Keep the backup outside automatic staging cleanup so even a
            # failed rollback leaves the previous installation recoverable.
            previous = Path(temporary).with_name(Path(temporary).name + ".previous")
            target.rename(previous)
            try:
                staging.rename(target)
            except BaseException:
                previous.rename(target)
                raise
            shutil.rmtree(previous, ignore_errors=True)
        result = load_character_package(target)
        return result.model_copy(update={"revision": revision})



def _image(source: Path, destination: Path, *, sprite: bool) -> dict[str, Any]:
    from PIL import Image, ImageOps

    with Image.open(source) as image:
        if image.width * image.height > 32_000_000:
            raise ValueError(f"image is too large: {source.name}")
        rgba = image.convert("RGBA")
        fitted = ImageOps.contain(rgba, (1024, 1024) if sprite else (2048, 2048))
        if sprite:
            canvas = Image.new("RGBA", (1024, 1024))
            canvas.alpha_composite(
                fitted, ((1024 - fitted.width) // 2, 1024 - fitted.height)
            )
        else:
            canvas = fitted
        destination.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(destination, format="PNG")
    data = destination.read_bytes()
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def _build_presentation(root: Path, manifest: CharacterManifest, revision: str) -> None:
    # Eye animation uses native coordinates; the fitted PNG also remains
    # available as a static fallback in the local installation.
    from PIL import Image

    if manifest.visuals.floating:
        from spica.core.floating_character import validate_floating_images
        validate_floating_images(root, manifest.visuals.floating)
    for sprite_id, relative in manifest.visuals.eye_rigs.items():
        rig_path = package_file(root, relative)
        rig = load_eye_rig(rig_path)
        for texture in (rig.textures.open, rig.textures.closed):
            with Image.open(package_file(rig_path.parent, texture)) as image:
                if image.size != (rig.canvas.width, rig.canvas.height):
                    raise ValueError("眼部动画原图尺寸与标定不一致。")
                alpha = image.convert("RGBA").getchannel("A")
                for eye in rig.eyes:
                    x, y, w, h = eye.bounds
                    if alpha.crop((x, y, x + w, y + h)).getextrema() != (255, 255):
                        raise ValueError("眼部动画区域必须不透明，以保持桌面点击区域稳定。")
        # Keep the declared sprite ID as the filename stem for visual events,
        # even when the artist named the source differently.
        native = root / ".presentation" / "eye-sprites" / f"{sprite_id}.png"
        native.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(package_file(root, manifest.visuals.sprites[sprite_id]), native)
    public = root / ".presentation"
    sprites: dict[str, Any] = {}
    for sprite_id, relative in manifest.visuals.sprites.items():
        path = f"sprites/{sprite_id}.png"
        sprites[sprite_id] = {
            "path": path,
            **_image(package_file(root, relative), public / path, sprite=True),
        }
    backgrounds: dict[str, Any] = {}
    for key, relative in manifest.backgrounds.items():
        path = f"backgrounds/{key}.png"
        backgrounds[key] = {
            "path": path,
            "label": key,
            **_image(package_file(root, relative), public / path, sprite=False),
        }
    avatar = None
    if manifest.avatar:
        avatar = {
            "path": "avatar.png",
            **_image(
                package_file(root, manifest.avatar), public / "avatar.png", sprite=False
            ),
        }
    settings_background = None
    if manifest.settings_background:
        settings_background = {
            "path": "settings-background.png",
            **_image(
                package_file(root, manifest.settings_background),
                public / "settings-background.png",
                sprite=False,
            ),
        }
    _write_json(
        public / "manifest.json",
        {
            "format": 1,
            "character_id": manifest.slug,
            "name": manifest.char_name,
            "version": manifest.version,
            "revision": revision,
            "default_costume": manifest.visuals.default_costume,
            "costumes": [
                {
                    "id": costume.id,
                    "label": costume.label,
                    "idle_sprite_id": costume.default_sprite,
                    **({"sprite_ids": sorted({costume.default_sprite}
                        | {sprite for choices in costume.emotions.values() for sprite in choices}
                        | {sprite for expressions in costume.expression_sprites.values() for sprite in expressions.values()})}
                       if manifest.visuals.eye_rigs else {}),
                }
                for costume in manifest.visuals.costumes
            ],
            "sprites": sprites,
            "backgrounds": backgrounds,
            "avatar": avatar,
            "settings_background": settings_background,
            "scenes": {
                endpoint: manifest.scenes.get(endpoint, Scene()).model_dump(mode="json")
                for endpoint in ("mobile", "robot")
            },
        },
    )


def _visual_rules(root: Path, manifest: CharacterManifest) -> dict[str, Any]:
    if manifest.visuals.rules_file:
        rules = read_config_file(package_file(root, manifest.visuals.rules_file))
        if not isinstance(rules, dict) or not isinstance(rules.get("hand_poses"), dict):
            raise ValueError("角色差分规则缺少 hand_poses。")
        expressions = rules.get("expressions")
        if not isinstance(expressions, list) or not expressions or any(
            not isinstance(item, dict) or not isinstance(item.get("id"), str)
            for item in expressions
        ):
            raise ValueError("角色差分规则缺少有效的 expressions。")
        ids = {item["id"] for item in expressions}
        for costume in manifest.visuals.costumes:
            for pose, mapping in costume.expression_sprites.items():
                if pose not in rules["hand_poses"] or not set(mapping).issubset(ids):
                    raise ValueError("角色立绘引用了差分规则中不存在的姿势或表情。")
        return rules
    return {
        "hand_poses": {"normal": {"folder": "normal", "display_name": "自然"}},
        "expressions": [
            {
                "id": expression_id,
                "emotion_group": group,
                "intensity": 2,
                "recommended_hand_pose": "normal",
                "compatible_hand_poses": ["normal"],
            }
            for expression_id, group in [("000", "neutral"), *_EMOTIONS.values()]
        ],
    }


def prepare_character_package(
    package: CharacterPackage, *, data_root: Path | None = None
) -> CharacterPackage:
    """Materialize mutable runtime configs outside the installed character."""
    data_root = data_root or _ROOT / "data" / "runtime"
    state = data_root / "character_states" / package.character_id
    state.mkdir(parents=True, exist_ok=True)
    manifest = package.manifest
    if manifest is None:
        return package.model_copy(update={"state_dir": str(state)})
    root = Path(package.package_root or "")
    installed = root / ".installed.json"
    if (
        not installed.is_file()
        or not (root / ".presentation" / "manifest.json").is_file()
    ):
        raise ValueError("角色文件夹尚未导入，请先从桌宠设置导入。")
    revision = json.loads(installed.read_text(encoding="utf-8"))["revision"]
    selected = manifest.visuals.default_costume
    previous = state / "visual.yaml"
    if previous.is_file():
        saved = read_config_file(previous)
        candidate = saved.get("selected_costume")
        if candidate in {costume.id for costume in manifest.visuals.costumes}:
            selected = candidate
    elif manifest.memory_file:
        from spica.core.character_memory import read_character_save

        save = read_character_save(
            package_file(root, manifest.memory_file), manifest.slug
        )
        if save.selected_costume in {
            costume.id for costume in manifest.visuals.costumes
        }:
            selected = save.selected_costume
    sprite_paths = {
        key: str(root / ".presentation" / "sprites" / f"{key}.png")
        for key in manifest.visuals.sprites
    }
    eye_rigs = {}
    for sprite_id, relative in manifest.visuals.eye_rigs.items():
        sprite_paths[sprite_id] = str(package_file(root, f".presentation/eye-sprites/{sprite_id}.png"))
        eye_rigs[sprite_paths[sprite_id]] = str(package_file(root, relative))
    sprite_map: dict[str, Any] = {}
    for costume in manifest.visuals.costumes:
        expressions = {"000": [sprite_paths[costume.default_sprite]]}
        for emotion, (expression_id, _group) in _EMOTIONS.items():
            choices = costume.emotions.get(emotion) or [costume.default_sprite]
            expressions[expression_id] = [sprite_paths[key] for key in choices]
        for pose, mapping in costume.expression_sprites.items():
            for expression_id, sprite_id in mapping.items():
                expressions[f"{pose}:{expression_id}"] = [sprite_paths[sprite_id]]
        sprite_map[costume.id] = expressions
    rules_path = state / "visual-rules.json"
    _write_json(
        rules_path,
        _visual_rules(root, manifest),
    )
    write_config_file(
        previous,
        {
            "enabled": True,
            "rules_path": str(rules_path),
            "sprite_map": sprite_map,
            "eye_rigs": eye_rigs,
            "costume_labels": {c.id: c.label for c in manifest.visuals.costumes},
            **({"renderer": "cubism", "cubism": str(package_file(root, manifest.visuals.cubism)),
                "package_root": str(root)} if manifest.visuals.cubism else {}),
            "costume_mode": "fixed",
            "selected_costume": selected,
            "character": {
                "default_expression_id": "000",
                "default_hand_pose": "normal",
            },
            "dialog": {"speaker": manifest.char_name},
        },
    )
    tts_path = state / "tts.json"
    _write_json(tts_path, _tts_config(root, manifest))
    return package.model_copy(
        update={
            "visual_config_path": str(previous),
            "tts_config_path": str(tts_path),
            "state_dir": str(state),
            "revision": revision,
        }
    )


def _tts_config(root: Path, manifest: CharacterManifest) -> dict[str, Any]:
    voice = manifest.tts
    if voice is None:
        return {"provider": "text_only"}
    # Engine installation belongs to this machine; the folder can only select weights.
    from agent_tools.tts import load_tts_config

    base = load_tts_config()
    engine = Path(base["gptsovits_root"])
    if not engine.is_absolute():
        engine = Path(base["_config_path"]).parent / engine
    output = Path(base.get("output_dir") or _ROOT / "data" / "generated" / "voice")
    if not output.is_absolute():
        output = Path(base["_config_path"]).parent / output
    emotions = {}
    for emotion in _EMOTIONS:
        reference = voice.emotions.get(emotion, voice.reference)
        emotions[emotion] = {
            "ref_audio_path": str(package_file(root, reference.audio)),
            "prompt_text": reference.text,
            "ref_language": reference.language,
            "inp_refs": [
                str(package_file(root, path)) for path in reference.additional_audio
            ],
        }
    return {
        "provider": "gptsovits_current",
        "inference_profile": voice.inference_profile,
        "gptsovits_root": str(engine.resolve()),
        "gpt_model_path": str(package_file(root, voice.gpt)),
        "sovits_model_path": str(package_file(root, voice.sovits)),
        "ref_language": voice.reference.language,
        "target_language": voice.target_language,
        "default_emotion": "happy",
        "output_dir": str(output.resolve() / manifest.slug),
        "warmup_on_startup": True,
        "warmup_synthesize": False,
        "warmup_emotion": "happy",
        "emotions": emotions,
        "tts_params": {
            **voice.parameters.model_dump(),
            "sentence_chunking": True,
            "max_chunk_sentences": 1,
            "max_chunk_chars": 120,
        },
    }


def character_song_config(
    package: CharacterPackage, current: dict[str, Any]
) -> dict[str, Any]:
    if package.manifest is None:
        return current
    result = copy.deepcopy(current)
    voice = package.manifest.rvc
    if voice is None:
        result["enabled"] = False
        return result
    root = Path(package.package_root or "")
    rvc = result.setdefault("rvc", {})
    defaults = next(iter(rvc.get("voices", {}).values()), {})
    selected = {
        **defaults,
        "model_path": str(package_file(root, voice.model)),
        "index_path": str(package_file(root, voice.index)) if voice.index else "",
        "index_rate": voice.index_rate,
        "transpose": voice.transpose,
        "protect": voice.protect,
        "reference_audio_dir": None,
    }
    rvc.update(
        voice_model=package.character_id, voices={package.character_id: selected}
    )
    result["generated_root"] = str(
        Path(current.get("generated_root") or _ROOT / "data" / "generated" / "song")
        / package.character_id
    )
    return result


def export_character_folder(
    package: CharacterPackage, destination: Path, *, memory: dict | None = None
) -> None:
    """Export source resources, optionally adding a scoped personal snapshot."""
    if package.manifest is None:
        raise ValueError("内置角色请先整理为标准角色文件夹。")
    if destination.exists():
        raise ValueError("导出目标必须是尚不存在的文件夹。")
    source = Path(package.package_root or "")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".export-", dir=destination.parent
    ) as temporary:
        staging = Path(temporary) / "package"
        staging.mkdir()
        manifest = package.manifest
        for relative in _source_files(source, manifest):
            if relative == manifest.memory_file:
                continue
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(package_file(source, relative), target)
        meta = json.loads((staging / "meta.json").read_text(encoding="utf-8"))
        meta.pop("memory_file", None)
        if memory is not None:
            meta["memory_file"] = "memory/personal.json"
            _write_json(staging / "memory" / "personal.json", memory)
        _write_json(staging / "meta.json", meta)
        staging.rename(destination)


def _installation_matches(staging: Path, installed: Path) -> bool:
    """Compare source and derived assets without trusting the revision marker."""
    try:
        for expected in staging.rglob("*"):
            if not expected.is_file():
                continue
            actual = package_file(installed, expected.relative_to(staging).as_posix())
            if actual.stat().st_size != expected.stat().st_size:
                return False
            with expected.open("rb") as reference, actual.open("rb") as existing:
                while chunk := reference.read(1024 * 1024):
                    if existing.read(len(chunk)) != chunk:
                        return False
        return True
    except (OSError, ValueError):
        return False
