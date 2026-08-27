"""Qt-free reader for the endpoint-local layered sprite bundle format."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


BUNDLE_FORMAT = 3
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_HEX = frozenset("0123456789abcdef")


class LayeredAssetError(ValueError):
    """The active local asset bundle is unavailable or unsafe."""


@dataclass(frozen=True, slots=True)
class AssetFile:
    path: str
    sha256: str
    size: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class LayeredSpriteRecipe:
    body_path: str
    face_path: str
    x: int
    y: int


@dataclass(frozen=True, slots=True)
class LayeredAssetManifest:
    root: Path
    bundle_id: str
    recipes: dict[str, LayeredSpriteRecipe]
    files: dict[str, AssetFile]

    def resolve(self, sprite_id: str) -> LayeredSpriteRecipe | None:
        """Resolve an exact logical id; selection and fallback stay upstream."""

        return self.recipes.get(sprite_id)


def load_layered_manifest(bundle_root: Path) -> LayeredAssetManifest:
    root = _resolve_bundle_root(Path(bundle_root))
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise LayeredAssetError("layered manifest must be a regular file")
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise LayeredAssetError("layered manifest is too large")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise LayeredAssetError("layered manifest is unreadable") from error
    if not isinstance(manifest, dict) or manifest.get("bundle_format") != BUNDLE_FORMAT:
        raise LayeredAssetError(f"bundle_format must be {BUNDLE_FORMAT}")
    bundle_id = manifest.get("bundle_id")
    if not _safe_id(bundle_id):
        raise LayeredAssetError("bundle_id is invalid")
    canvas = manifest.get("canvas")
    if not isinstance(canvas, dict):
        raise LayeredAssetError("canvas is invalid")
    width = canvas.get("width")
    height = canvas.get("height")
    if not (_positive_int(width) and _positive_int(height)):
        raise LayeredAssetError("canvas dimensions are invalid")

    files: dict[str, AssetFile] = {}
    bodies = _parse_asset_section(manifest.get("bodies"), "bodies", files)
    faces = _parse_asset_section(manifest.get("faces"), "faces", files)
    if any((asset.width, asset.height) != (width, height) for asset in bodies.values()):
        raise LayeredAssetError("body dimensions do not match the canvas")

    raw_sprites = manifest.get("sprites")
    if not isinstance(raw_sprites, dict) or not raw_sprites:
        raise LayeredAssetError("sprites must be a non-empty object")
    recipes: dict[str, LayeredSpriteRecipe] = {}
    used_bodies: set[str] = set()
    used_faces: set[str] = set()
    for sprite_id, raw in raw_sprites.items():
        if not _safe_id(sprite_id) or not isinstance(raw, dict):
            raise LayeredAssetError("sprite recipe is invalid")
        body_id = raw.get("body")
        face_id = raw.get("face")
        x = raw.get("x")
        y = raw.get("y")
        if body_id not in bodies or face_id not in faces:
            raise LayeredAssetError(f"sprite {sprite_id!r} references an unknown asset")
        if not (_nonnegative_int(x) and _nonnegative_int(y)):
            raise LayeredAssetError(f"sprite {sprite_id!r} has an invalid placement")
        face = faces[face_id]
        if x + face.width > width or y + face.height > height:
            raise LayeredAssetError(f"sprite {sprite_id!r} escapes the canvas")
        recipes[sprite_id] = LayeredSpriteRecipe(
            body_path=bodies[body_id].path,
            face_path=face.path,
            x=x,
            y=y,
        )
        used_bodies.add(body_id)
        used_faces.add(face_id)
    if used_bodies != set(bodies) or used_faces != set(faces):
        raise LayeredAssetError("body and face assets must all be referenced")

    _parse_costume_previews(manifest.get("costumes"), recipes, files)
    for relative_path in files:
        asset_path = root / relative_path
        if asset_path.is_symlink() or not asset_path.is_file():
            raise LayeredAssetError(f"asset is missing or linked: {relative_path!r}")
    return LayeredAssetManifest(
        root=root,
        bundle_id=bundle_id,
        recipes=recipes,
        files=files,
    )


def _resolve_bundle_root(root: Path) -> Path:
    if not root.is_symlink():
        if not root.is_dir():
            raise LayeredAssetError("layered bundle root is unavailable")
        return root
    try:
        assets_root = root.parent.resolve(strict=True)
        resolved = root.resolve(strict=True)
    except OSError as error:
        raise LayeredAssetError("layered bundle link is unavailable") from error
    if resolved.parent != assets_root or not resolved.is_dir():
        raise LayeredAssetError("current bundle must target a sibling directory")
    return resolved


def _parse_asset_section(
    raw_section: object,
    kind: str,
    files: dict[str, AssetFile],
) -> dict[str, AssetFile]:
    if not isinstance(raw_section, dict) or not raw_section:
        raise LayeredAssetError(f"{kind} must be a non-empty object")
    parsed: dict[str, AssetFile] = {}
    for asset_id, raw_file in raw_section.items():
        if not _content_id(asset_id):
            raise LayeredAssetError(f"{kind} contains an invalid content id")
        asset = _parse_file(raw_file)
        if asset.sha256 != asset_id:
            raise LayeredAssetError(f"{kind} content id does not match sha256")
        if asset.path in files:
            raise LayeredAssetError(f"duplicate asset path: {asset.path!r}")
        parsed[asset_id] = asset
        files[asset.path] = asset
    return parsed


def _parse_costume_previews(
    raw_costumes: object,
    recipes: dict[str, LayeredSpriteRecipe],
    files: dict[str, AssetFile],
) -> None:
    if not isinstance(raw_costumes, dict) or not raw_costumes:
        raise LayeredAssetError("costumes must be a non-empty object")
    for costume, raw in raw_costumes.items():
        if not _safe_id(costume) or not isinstance(raw, dict):
            raise LayeredAssetError("costume entry is invalid")
        if raw.get("idle_sprite_id") not in recipes:
            raise LayeredAssetError(f"costume {costume!r} has no idle sprite")
        preview = raw.get("preview")
        if not isinstance(preview, dict) or not _content_id(preview.get("id")):
            raise LayeredAssetError(f"costume {costume!r} preview is invalid")
        asset = _parse_file(preview)
        if asset.sha256 != preview["id"]:
            raise LayeredAssetError(f"costume {costume!r} preview id is invalid")
        existing = files.get(asset.path)
        if existing is not None and existing != asset:
            raise LayeredAssetError(f"duplicate asset path: {asset.path!r}")
        files.setdefault(asset.path, asset)


def _parse_file(raw: object) -> AssetFile:
    if not isinstance(raw, dict):
        raise LayeredAssetError("asset metadata is invalid")
    path = raw.get("path")
    digest = raw.get("sha256")
    size = raw.get("size")
    width = raw.get("width")
    height = raw.get("height")
    if not _safe_relative_png(path):
        raise LayeredAssetError(f"unsafe asset path: {path!r}")
    if not (
        _content_id(digest)
        and _positive_int(size)
        and _positive_int(width)
        and _positive_int(height)
    ):
        raise LayeredAssetError("asset metadata is incomplete")
    return AssetFile(path, digest, size, width, height)


def _safe_relative_png(value: object) -> bool:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        return False
    parts = value.split("/")
    return (
        all(_safe_id(part) for part in parts)
        and parts[-1].lower().endswith(".png")
        and len(parts[-1]) > 4
    )


def _safe_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _content_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX for character in value)
    )


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0
