"""Desktop layered sprite resolution keeps the existing one-pixmap UI seam."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
import pytest
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from agent_tools.visual.layered_assets import LayeredAssetError, load_layered_manifest
from agent_tools.visual.layered_bundle_builder import build_layered_bundle
from ui.layered_sprite_store import LocalSpriteStore, PixmapByteCache


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _write_asset(root: Path, kind: str, image: Image.Image) -> tuple[str, dict]:
    scratch = root / f"{kind}.png"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    image.save(scratch, "PNG", optimize=True)
    data = scratch.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    relative = f"{kind}/{digest}.png"
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    scratch.replace(destination)
    return digest, {
        "path": relative,
        "sha256": digest,
        "size": len(data),
        "width": image.width,
        "height": image.height,
    }


def _write_bundle(root: Path) -> None:
    body = Image.new("RGBA", (4, 4), (20, 40, 80, 255))
    body.paste((0, 0, 0, 0), (1, 1, 3, 3))
    face = Image.new("RGBA", (2, 2), (240, 30, 20, 255))
    preview = Image.new("RGBA", (4, 4), (20, 40, 80, 255))
    body_id, body_meta = _write_asset(root, "bodies", body)
    face_id, face_meta = _write_asset(root, "faces", face)
    preview_id, preview_meta = _write_asset(root, "previews", preview)
    manifest = {
        "bundle_format": 3,
        "bundle_id": "sprites-4-fixture",
        "canvas": {"width": 4, "height": 4},
        "bodies": {body_id: body_meta},
        "faces": {face_id: face_meta},
        "sprites": {
            "sprite_000": {
                "body": body_id,
                "face": face_id,
                "x": 1,
                "y": 1,
                "costume": "校服spica",
                "hand_pose": "普通动作",
                "expression_id": 0,
            }
        },
        "costumes": {
            "校服spica": {
                "idle_sprite_id": "sprite_000",
                "preview": {"id": preview_id, **preview_meta},
            }
        },
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )


def test_layered_store_resolves_a_missing_legacy_path_to_one_composed_pixmap(
    qapp,
    tmp_path: Path,
) -> None:
    del qapp
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_bundle(bundle)
    store = LocalSpriteStore(bundle_root=bundle)
    logical_path = tmp_path / "legacy" / "sprite_000.png"

    resolved = store.resolve(logical_path)

    assert resolved is not None
    assert resolved.layered is True
    image = resolved.pixmap.toImage()
    assert image.pixelColor(0, 0).getRgb() == (20, 40, 80, 255)
    assert image.pixelColor(1, 1).getRgb() == (240, 30, 20, 255)
    cached = store.resolve(logical_path)
    assert cached is not None and cached.cache_hit is True
    assert cached.pixmap.cacheKey() == resolved.pixmap.cacheKey()


def test_layered_store_falls_back_to_a_legacy_full_png(qapp, tmp_path: Path) -> None:
    del qapp
    legacy_path = tmp_path / "legacy.png"
    Image.new("RGBA", (3, 3), (10, 200, 30, 255)).save(legacy_path, "PNG")
    store = LocalSpriteStore(bundle_root=tmp_path / "missing-bundle")

    resolved = store.resolve(legacy_path)

    assert resolved is not None
    assert resolved.layered is False
    assert resolved.pixmap.toImage().pixelColor(1, 1).getRgb() == (
        10,
        200,
        30,
        255,
    )


def test_local_sprite_store_evicts_decoded_pixmaps_by_byte_budget(
    qapp,
    tmp_path: Path,
) -> None:
    del qapp
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    Image.new("RGBA", (4, 4), (200, 10, 10, 255)).save(first, "PNG")
    Image.new("RGBA", (4, 4), (10, 10, 200, 255)).save(second, "PNG")
    store = LocalSpriteStore(
        bundle_root=tmp_path / "missing-bundle",
        max_bytes=4 * 4 * 4,
    )

    first_result = store.resolve(first)
    second_result = store.resolve(second)
    reloaded_first = store.resolve(first)
    assert first_result is not None and first_result.cache_hit is False
    assert second_result is not None and second_result.cache_hit is False
    assert reloaded_first is not None and reloaded_first.cache_hit is False


def test_face_changes_reuse_layers_without_retaining_every_full_frame(
    qapp, tmp_path: Path, monkeypatch,
) -> None:
    del qapp
    source = tmp_path / "diffs"
    group = source / "校服spica" / "普通动作"
    group.mkdir(parents=True)
    paths = []
    for index, color in enumerate(((250, 220, 180, 255), (230, 20, 40, 255), (20, 230, 40, 255))):
        image = Image.new("RGBA", (8, 8), (20, 40, 80, 255))
        image.paste(color, (3, 2, 5, 5))
        path = group / f"uniform_face001_{index:03d}.png"
        image.save(path, "PNG")
        paths.append(path)
    bundle = build_layered_bundle(source, tmp_path / "bundles", px=8)
    # One body + three 2x3 faces + one composite, all within the same budget.
    budget = 2 * 8 * 8 * 4 + 3 * 2 * 3 * 4
    store = LocalSpriteStore(bundle_root=bundle, max_bytes=budget)
    decoded_paths = []

    def load_pixmap(value):
        if isinstance(value, str):
            decoded_paths.append(value)
        return QPixmap(value)

    monkeypatch.setattr("ui.layered_sprite_store.QPixmap", load_pixmap)
    first = store.resolve(paths[0])
    for path in paths[1:] + paths:
        resolved = store.resolve(path)
        assert resolved is not None and resolved.layered
        assert store.cached_bytes <= budget

    assert len(decoded_paths) == 4  # each shared layer decoded only once
    assert first.pixmap.toImage().pixelColor(3, 2).getRgb() == (250, 220, 180, 255)
    assert store.resolve(paths[-1]).cache_hit is True


def test_pixmap_byte_cache_bounds_scaled_and_raw_pixmaps(qapp) -> None:
    del qapp
    cache = PixmapByteCache(max_bytes=4 * 4 * 4)
    first = QPixmap(4, 4)
    second = QPixmap(4, 4)

    assert cache.put("first", first) is True
    assert cache.put("second", second) is True

    assert cache.get("first") is None
    assert cache.get("second") is second
    assert len(cache) == 1


def test_desktop_builder_emits_an_exactly_reconstructable_bundle(
    tmp_path: Path,
) -> None:
    source = tmp_path / "diffs"
    group = source / "校服spica" / "普通动作"
    group.mkdir(parents=True)
    expected: dict[str, Image.Image] = {}
    for expression_id, color in ((0, (250, 220, 180, 255)), (1, (230, 20, 40, 255))):
        image = Image.new("RGBA", (8, 8), (20, 40, 80, 255))
        image.paste(color, (3, 2, 5, 5))
        sprite_id = f"uniform_face001_{expression_id:03d}"
        image.save(group / f"{sprite_id}.png", "PNG")
        expected[sprite_id] = image

    bundle = build_layered_bundle(source, tmp_path / "bundles", px=8)
    manifest = load_layered_manifest(bundle)

    assert set(manifest.recipes) == set(expected)
    for sprite_id, expected_image in expected.items():
        recipe = manifest.resolve(sprite_id)
        assert recipe is not None
        with Image.open(manifest.root / recipe.body_path) as body:
            reconstructed = body.convert("RGBA")
        with Image.open(manifest.root / recipe.face_path) as face:
            reconstructed.alpha_composite(face.convert("RGBA"), (recipe.x, recipe.y))
        assert reconstructed.tobytes() == expected_image.tobytes()


def test_desktop_manifest_rejects_a_layer_path_that_escapes_the_bundle(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_bundle(bundle)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    body_id = next(iter(manifest["bodies"]))
    manifest["bodies"][body_id]["path"] = "../outside.png"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(LayeredAssetError, match="unsafe asset path"):
        load_layered_manifest(bundle)
