import json
from pathlib import Path
import pytest

from spica.adapters.visual.spica_diff import build_spica_visual
from spica.host.character_packages import import_character_folder, prepare_character_package
from test_cubism_packages import cubism_folder, write_json


@pytest.mark.parametrize("use_pool", [False, True])
def test_native_director_selects_locally_without_advancing_playback(cubism_folder, tmp_path, use_pool):
    root = cubism_folder
    path = root / "live2d/director.json"
    write_json(path, {"format": "spica-cubism-director", "version": 1, "rules": [
        {"id": "explosion", "keywords": ["爆裂魔法", "エクスプロージョン", "explosion"],
         "exclude": ["不要", "使わない", "don't", "について", "means"],
         **({"motions": ["cast", "missing", "cast-again"]} if use_pool else {"motion": "cast"})},
    ]})
    binding = json.loads((root / "live2d/bindings.json").read_text())
    binding["models"]["classic"]["motions"]["cast"] = {"group": "cast", "index": 0, "kind": "skill", "cooldown": 20}
    binding["models"]["classic"]["motions"]["cast-again"] = {"group": "cast", "index": 0, "kind": "skill", "cooldown": 20}
    model_path = root / "live2d/classic/Character.model3.json"
    model = json.loads(model_path.read_text())
    model["FileReferences"]["Motions"]["cast"] = [{"File": "cast.motion3.json"}]
    write_json(model_path, model)
    write_json(root / "live2d/classic/cast.motion3.json", {
        "Version": 3, "Meta": {"Duration": 2, "Loop": False}, "Curves": [],
    })
    write_json(root / "live2d/bindings.json", binding)
    package = prepare_character_package(import_character_folder(root, tmp_path / "installed"), data_root=tmp_path / "state")
    visual = build_spica_visual(package.visual_config_path)
    context = visual.prepare_stream_context()
    def select(text, index=0):
        return visual.build_unit_visual_payload(text, "happy", index, runtime_context=context)["cue"]
    for phrase in ("爆裂魔法！", "エクスプロージョン！", "Explosion!"):
        cue = select(phrase)
        assert cue["cubism"]["motion"] == "cast"
        assert cue["cubism"]["motions"] == (["cast", "cast-again"] if use_pool else ["cast"])
        assert Path(cue["image_path"]).is_file()
        assert Path(cue["image_path"]).suffix == ".png"
    # Preparing a later unit never consumes a cooldown or changes an earlier cue.
    assert select("爆裂魔法！", 8)["cubism"]["motion"] == "cast"
    assert select("爆裂魔法！")["cubism"]["motion"] == "cast"
    for phrase in ("不要用爆裂魔法", "エクスプロージョンは使わない", "Don't use explosion", "explosion means 爆裂魔法",
                   "她说：‘爆裂魔法！’", "「エクスプロージョン！」と叫んだ。", 'She shouted "Explosion!" yesterday.',
                   "I will not cast explosion!", "爆裂魔法を放たないでください。"):
        assert select(phrase)["cubism"]["motion"] is None
