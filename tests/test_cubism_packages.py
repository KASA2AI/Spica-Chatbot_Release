"""Native character packs use the existing portable import/export boundary."""

import json
import shutil
import sys
from pathlib import Path

from PIL import Image
import pytest

from spica.host.character_packages import (
    export_character_folder, import_character_folder, prepare_character_package,
)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def cubism_folder(tmp_path):
    root = tmp_path / "角色包 source"
    root.mkdir()
    Image.new("RGBA", (24, 48), (190, 40, 30, 255)).save(root / "preview.png")
    (root / "persona.md").write_text("{{char}}は{{user}}の仲間です。", encoding="utf-8")
    model = root / "live2d" / "classic"
    model.mkdir(parents=True)
    (model / "Character.moc3").write_bytes(b"MOC3\x02" + b"\0" * 59)
    Image.new("RGBA", (16, 16), (10, 20, 30, 255)).save(model / "texture.png")
    write_json(model / "idle.motion3.json", {
        "Version": 3, "Meta": {"Duration": 2, "Fps": 30, "Loop": True}, "Curves": [],
    })
    write_json(model / "Character.model3.json", {
        "Version": 3, "FileReferences": {"Moc": "Character.moc3", "Textures": ["texture.png"],
            "Motions": {"idle": [{"File": "idle.motion3.json"}]}},
    })
    write_json(root / "live2d" / "director.json", {"format": "spica-cubism-director", "version": 1, "rules": []})
    write_json(root / "live2d" / "bindings.json", {
        "format": "spica-cubism", "version": 1, "director": "live2d/director.json",
        "models": {"classic": {"model": "live2d/classic/Character.model3.json",
            "idle": "idle", "motions": {"idle": {"group": "idle", "index": 0, "kind": "idle"}}}},
    })
    write_json(root / "meta.json", {
        "pack_format": 3, "slug": "native-test", "version": "1.0", "name": "Native", "char_name": "Native",
        "visuals": {"renderer": "cubism", "cubism": "live2d/bindings.json",
            "sprites": {"classic": "preview.png"}, "default_costume": "classic",
            "costumes": [{"id": "classic", "label": "Classic", "default_sprite": "classic"}]},
    })
    return root


def test_native_pack_is_self_contained_without_loading_renderer(cubism_folder, tmp_path):
    before = set(sys.modules)
    source_moc = (cubism_folder / "live2d/classic/Character.moc3").read_bytes()
    package = import_character_folder(cubism_folder, tmp_path / "installed")
    shutil.rmtree(cubism_folder)
    package = prepare_character_package(package, data_root=tmp_path / "state")
    config = Path(package.visual_config_path).read_text(encoding="utf-8")
    assert '"cubism"' in config or "cubism:" in config
    assert (Path(package.package_root) / "live2d/classic/Character.moc3").read_bytes() == source_moc
    destination = tmp_path / "share"
    export_character_folder(package, destination)
    again = import_character_folder(destination, tmp_path / "reinstalled")
    assert again.manifest == package.manifest
    for relative in ("live2d/bindings.json", "live2d/director.json", "live2d/classic/Character.moc3",
                     "live2d/classic/Character.model3.json", "live2d/classic/texture.png",
                     "live2d/classic/idle.motion3.json", "preview.png", "persona.md"):
        assert (Path(again.package_root) / relative).read_bytes() == (Path(package.package_root) / relative).read_bytes()
    assert not any(name == "live2d" or name.startswith(("live2d.", "OpenGL.")) for name in set(sys.modules) - before)




@pytest.mark.parametrize("relative, damage", [
    ("live2d/classic/Character.moc3", "broken"),
    ("live2d/classic/Character.moc3", "same_size"),
    ("meta.json", "broken"),
    (".presentation/sprites/classic.png", "missing"),
])
def test_reimport_repairs_damaged_installed_assets(cubism_folder, tmp_path, relative, damage):
    installed = import_character_folder(cubism_folder, tmp_path / "installed")
    damaged = Path(installed.package_root) / relative
    original = damaged.read_bytes()
    if damage == "missing":
        damaged.unlink()
    elif damage == "same_size":
        # A valid MOC header and unchanged length do not prove intact content.
        damaged.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    else:
        damaged.write_bytes(b"broken local file")

    repaired = import_character_folder(cubism_folder, tmp_path / "installed")

    assert repaired.package_root == installed.package_root
    assert repaired.revision == installed.revision
    assert damaged.read_bytes() == original
    if (cubism_folder / relative).exists():
        assert (cubism_folder / relative).read_bytes() == original


@pytest.mark.parametrize("damage", ["texture", "escape", "loop"])
def test_invalid_native_resource_does_not_publish_an_installation(cubism_folder, tmp_path, damage):
    root = cubism_folder
    path = root / "live2d/classic/Character.model3.json"
    model = json.loads(path.read_text())
    if damage == "texture":
        (root / "live2d/classic/texture.png").write_bytes(b"broken PNG")
    elif damage == "escape":
        model["FileReferences"]["Textures"] = ["../../preview.png"]
        write_json(path, model)
    else:
        motion = root / "live2d/classic/idle.motion3.json"
        value = json.loads(motion.read_text())
        value["Meta"]["Loop"] = False
        write_json(motion, value)
    with pytest.raises((ValueError, OSError)):
        import_character_folder(root, tmp_path / "installed")
    assert not list((tmp_path / "installed").rglob(".installed.json"))


def test_native_card_uses_the_selected_name_and_its_worldbook(cubism_folder):
    from spica.conversation.character_loader import load_spica_character_profile

    path = cubism_folder / "meta.json"
    meta = json.loads(path.read_text())
    meta["user_aliases"] = ["OldAlias"]
    meta["worldbook_file"] = "world.md"
    write_json(path, meta)
    (cubism_folder / "world.md").write_text("OldAlias と {{char}} は仲間です。", encoding="utf-8")
    profile = load_spica_character_profile(cubism_folder, interlocutor_name="傘")
    assert "傘 と Native は仲間です。" in profile
    assert "{{" not in profile and "OldAlias" not in profile
