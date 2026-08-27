"""Offline builder for content-addressed body/face sprite bundles."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import re
import shutil
import tempfile
from pathlib import Path

from PIL import Image, ImageChops

from agent_tools.visual.layered_assets import BUNDLE_FORMAT, load_layered_manifest


_EXPRESSION_STEM = re.compile(r"^(?P<prefix>.+)_(?P<expression>[0-9]{3})$")
SOURCE_COMMIT = "f376caa0b2bbd1550e5f26e6f34ed918170145ab"


@dataclass(frozen=True, slots=True)
class _SourceSprite:
    path: Path
    sprite_id: str
    costume: str
    hand_pose: str
    prefix: str
    expression_id: int

    @property
    def group_key(self) -> tuple[str, str, str]:
        return self.costume, self.hand_pose, self.prefix


def build_layered_bundle(source_root: Path, output_root: Path, *, px: int) -> Path:
    """Build and atomically publish one immutable bundle, returning its path."""

    if type(px) is not int or px <= 0:
        raise ValueError("target size must be a positive integer")
    source_root = Path(source_root)
    output_root = Path(output_root)
    sprites = _collect_sources(source_root)
    if not sprites:
        raise ValueError(f"no sprite PNGs found under {source_root}")
    output_root.mkdir(parents=True, exist_ok=True)
    stage: Path | None = Path(tempfile.mkdtemp(prefix=".staging-", dir=output_root))
    try:
        bodies, faces, recipes, costumes = _build_assets(stage, sprites, px)
        canonical = {
            "canvas": {"width": px, "height": px},
            "bodies": bodies,
            "faces": faces,
            "sprites": recipes,
            "costumes": costumes,
        }
        content_hash = hashlib.sha256(
            json.dumps(
                canonical,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        bundle_id = f"sprites-{px}-{content_hash[:12]}"
        manifest = {
            "bundle_format": BUNDLE_FORMAT,
            "bundle_id": bundle_id,
            "source": {
                "upstream_commit": SOURCE_COMMIT,
                "count": len(recipes),
            },
            "target_px": px,
            **canonical,
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        _verify_bundle(stage, manifest)
        destination = output_root / bundle_id
        if destination.exists():
            _verify_bundle(destination, manifest)
            return destination
        try:
            stage.rename(destination)
            stage = None
        except OSError:
            _verify_bundle(destination, manifest)
        return destination
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)


def _collect_sources(source_root: Path) -> list[_SourceSprite]:
    if source_root.is_symlink() or not source_root.is_dir():
        raise ValueError("source root must be a regular directory")
    sprites: list[_SourceSprite] = []
    seen_ids: set[str] = set()
    expressions_by_group: dict[tuple[str, str, str], set[int]] = {}
    for path in sorted(source_root.rglob("*.png")):
        relative = path.relative_to(source_root)
        if relative.parts and relative.parts[0] == "ui":
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"sprite source must be a regular file: {relative.as_posix()}")
        if len(relative.parts) != 3 or any(
            part in {"", ".", ".."} or part != part.strip() for part in relative.parts
        ):
            raise ValueError(
                f"sprite source must be costume/hand_pose/file.png: {relative.as_posix()}"
            )
        match = _EXPRESSION_STEM.fullmatch(path.stem)
        if match is None:
            raise ValueError(f"sprite stem lacks a three-digit expression id: {path.stem}")
        sprite_id = path.stem
        if sprite_id in seen_ids:
            raise ValueError(f"duplicate sprite id: {sprite_id}")
        seen_ids.add(sprite_id)
        sprite = _SourceSprite(
            path=path,
            sprite_id=sprite_id,
            costume=relative.parts[0],
            hand_pose=relative.parts[1],
            prefix=match.group("prefix"),
            expression_id=int(match.group("expression")),
        )
        group_expressions = expressions_by_group.setdefault(sprite.group_key, set())
        if sprite.expression_id in group_expressions:
            raise ValueError(f"duplicate expression in group: {sprite.group_key!r}")
        group_expressions.add(sprite.expression_id)
        sprites.append(sprite)
    for group_key, expression_ids in expressions_by_group.items():
        if 0 not in expression_ids:
            raise ValueError(f"sprite group has no expression 000: {group_key!r}")
    return sprites


def _build_assets(stage: Path, sprites: list[_SourceSprite], px: int):
    groups: dict[tuple[str, str, str], list[_SourceSprite]] = {}
    for sprite in sprites:
        groups.setdefault(sprite.group_key, []).append(sprite)
    bodies: dict[str, dict] = {}
    faces: dict[str, dict] = {}
    recipes: dict[str, dict] = {}
    idle_by_costume: dict[str, tuple[_SourceSprite, Image.Image]] = {}
    for group_sprites in groups.values():
        ordered = sorted(group_sprites, key=lambda item: item.expression_id)
        idle = next(item for item in ordered if item.expression_id == 0)
        base = _load_rgba(idle.path, px)
        union_mask = Image.new("L", (px, px), 0)
        for sprite in ordered:
            union_mask = ImageChops.lighter(
                union_mask,
                _difference_mask(base, _load_rgba(sprite.path, px)),
            )
        body = base.copy()
        body.paste((0, 0, 0, 0), mask=union_mask)
        body_id, body_meta = _store_png(stage, "bodies", body)
        bodies.setdefault(body_id, body_meta)
        bbox = union_mask.getbbox()
        x, y = bbox[:2] if bbox is not None else (0, 0)
        patch_mask = union_mask.crop(bbox) if bbox is not None else None
        for sprite in ordered:
            current = _load_rgba(sprite.path, px)
            if bbox is None:
                face = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
            else:
                crop = current.crop(bbox)
                face = Image.new("RGBA", crop.size, (0, 0, 0, 0))
                face.paste(crop, mask=patch_mask)
            face_id, face_meta = _store_png(stage, "faces", face)
            faces.setdefault(face_id, face_meta)
            recipes[sprite.sprite_id] = {
                "body": body_id,
                "face": face_id,
                "x": x,
                "y": y,
                "costume": sprite.costume,
                "hand_pose": sprite.hand_pose,
                "expression_id": sprite.expression_id,
            }
            if sprite.hand_pose == "普通动作" and sprite.expression_id == 0:
                if sprite.costume in idle_by_costume:
                    raise ValueError(f"costume has multiple idle sprites: {sprite.costume}")
                idle_by_costume[sprite.costume] = (sprite, current)
    all_costumes = {sprite.costume for sprite in sprites}
    if set(idle_by_costume) != all_costumes:
        missing = sorted(all_costumes - set(idle_by_costume))
        raise ValueError(f"costume has no 普通动作 expression 000: {missing[0]}")
    costumes: dict[str, dict] = {}
    preview_px = min(px, 256)
    for costume, (idle, image) in sorted(idle_by_costume.items()):
        preview = image.resize((preview_px, preview_px), Image.Resampling.LANCZOS)
        preview_id, preview_meta = _store_png(stage, "previews", preview)
        costumes[costume] = {
            "idle_sprite_id": idle.sprite_id,
            "preview": {"id": preview_id, **preview_meta},
        }
    return bodies, faces, recipes, costumes


def _load_rgba(path: Path, px: int) -> Image.Image:
    with Image.open(path) as source:
        image = source.convert("RGBA")
    if image.size != (px, px):
        image = image.resize((px, px), Image.Resampling.LANCZOS)
    red, green, blue, alpha = image.split()
    visible = alpha.point(lambda value: 255 if value else 0)
    return Image.merge(
        "RGBA",
        (
            ImageChops.multiply(red, visible),
            ImageChops.multiply(green, visible),
            ImageChops.multiply(blue, visible),
            alpha,
        ),
    )


def _difference_mask(left: Image.Image, right: Image.Image) -> Image.Image:
    channels = ImageChops.difference(left, right).split()
    difference = channels[0]
    for channel in channels[1:]:
        difference = ImageChops.lighter(difference, channel)
    return difference.point(lambda value: 255 if value else 0)


def _store_png(stage: Path, kind: str, image: Image.Image) -> tuple[str, dict]:
    buffer = BytesIO()
    image.save(buffer, "PNG", optimize=True)
    data = buffer.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    relative = f"{kind}/{digest}.png"
    destination = stage / relative
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    return digest, {
        "path": relative,
        "sha256": digest,
        "size": len(data),
        "width": image.width,
        "height": image.height,
    }


def _verify_bundle(bundle: Path, expected_manifest: dict) -> None:
    try:
        actual_manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("published manifest is unavailable") from error
    if actual_manifest != expected_manifest:
        raise ValueError("published manifest does not match this build")
    validated = load_layered_manifest(bundle)
    actual_files = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    if actual_files != set(validated.files):
        raise ValueError("published bundle file set does not match its manifest")
    for relative, metadata in validated.files.items():
        data = (bundle / relative).read_bytes()
        if len(data) != metadata.size or hashlib.sha256(data).hexdigest() != metadata.sha256:
            raise ValueError(f"published asset digest mismatch: {relative}")
