from __future__ import annotations

from support.filesystem import symlink_or_skip
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from agent_tools.visual.diff_service import VisualDiffService
from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from spica.config.manager import ConfigManager
from spica.conversation.character_loader import build_character_profile
from spica.conversation.prompt_builder import build_system_prompt
from spica.core.character import load_character_package
from spica.core.character_memory import export_character_save, restore_character_save
from spica.host.character_packages import (
    export_character_folder,
    import_character_folder,
    prepare_character_package,
)
from spica.host.management import ManagementSurface


@pytest.mark.parametrize("variant", ["static", "eye-rig"])
def test_documented_examples_import_render_persona_and_roundtrip(variant, tmp_path):
    source = Path(__file__).resolve().parents[1] / "Desktop-Packs" / "Characters" / "Examples" / variant
    package = prepare_character_package(
        import_character_folder(source, tmp_path / "installed"),
        data_root=tmp_path / "state",
    )
    profile = build_character_profile(None, package.skill_dir, "NewAlias")
    assert "小星" in profile and "NewAlias" in profile and "星光小屋" in profile
    assert "旅人" not in profile and "{{" not in profile
    visual = VisualDiffService(package.visual_config_path)
    assert visual.resolve_expression_image("casual", "normal", "000").is_file()
    assert json.loads(Path(package.tts_config_path).read_text())["provider"] == "text_only"
    export_character_folder(package, tmp_path / "share")
    restored = import_character_folder(tmp_path / "share", tmp_path / "reimported")
    assert restored.manifest == package.manifest


@pytest.fixture
def folder(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    Image.new("RGBA", (80, 160), (240, 120, 50, 255)).save(source / "sana.png")
    (source / "persona.md").write_text("{{char}}は新吾と話す。", encoding="utf-8")
    meta = {
        "pack_format": 1,
        "slug": "sana",
        "version": "1.0",
        "name": "乾紗凪",
        "char_name": "紗凪",
        "user_aliases": ["新吾"],
        "visuals": {
            "sprites": {"sana": "sana.png"},
            "default_costume": "school",
            "costumes": [{"id": "school", "label": "校服", "default_sprite": "sana"}],
        },
    }
    (source / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    return source


def management(tmp_path):
    return ManagementSurface(
        registry=None,
        config_manager=ConfigManager(tmp_path / "app.yaml"),
        plugin_host=None,
        characters_root=tmp_path / "data",
    )


def test_folder_import_selects_and_survives_removing_original(folder, tmp_path):
    surface = management(tmp_path)
    result = surface.import_character(folder)
    shutil.rmtree(folder)
    config = surface.read_config()
    assert config["character"]["package_dir"] == result["dir"]
    package = prepare_character_package(
        load_character_package(result["dir"]), data_root=tmp_path / "data"
    )
    visual = VisualDiffService(package.visual_config_path)
    assert visual.current_default_sprite_id() == "sana"
    assert visual.list_costume_sets() == ["school"]
    assert visual.set_costume("school") == "school"
    restarted = prepare_character_package(package, data_root=tmp_path / "data")
    assert VisualDiffService(restarted.visual_config_path).current_costume() == "school"
    for emotion, expression in (
        ("happy", "002"),
        ("angry", "013"),
        ("sad", "010"),
        ("surprised", "009"),
    ):
        image = visual.resolve_expression_image("school", "normal", expression)
        with Image.open(image) as png:
            assert png.size == (1024, 1024)
            assert png.getpixel((512, 512)) == (240, 120, 50, 255)
    assert (
        json.loads(Path(package.tts_config_path).read_text())["provider"] == "text_only"
    )
    profile = build_character_profile(None, package.skill_dir, "サン")
    assert "紗凪はサンと話す" in profile
    prompt = build_system_prompt(character_name=package.char_name)
    assert "Spica" not in prompt
    assert "没有可控制现实物体的实体身体" in prompt


def test_import_preserves_exact_expression_and_hand_pose_rules(folder, tmp_path):
    for name, color in (("normal", "red"), ("pointing", "blue")):
        Image.new("RGBA", (80, 160), color).save(folder / f"{name}.png")
    rules = {
        "hand_poses": {
            "normal": {"folder": "normal"},
            "index_finger": {"folder": "pointing"},
        },
        "expressions": [
            {"id": "000", "emotion_group": "neutral", "intensity": 1},
            {"id": "004", "emotion_group": "joy", "emotion_subtype": "talking_light",
             "intensity": 3, "recommended_hand_pose": "index_finger",
             "compatible_hand_poses": ["normal", "index_finger"]},
        ],
    }
    (folder / "rules.json").write_text(json.dumps(rules))
    meta = json.loads((folder / "meta.json").read_text())
    meta["visuals"]["rules_file"] = "rules.json"
    meta["visuals"]["sprites"].update(normal="normal.png", pointing="pointing.png")
    meta["visuals"]["costumes"][0]["expression_sprites"] = {
        "normal": {"000": "sana", "004": "normal"},
        "index_finger": {"004": "pointing"},
    }
    (folder / "meta.json").write_text(json.dumps(meta))
    package = prepare_character_package(
        import_character_folder(folder, tmp_path / "installed"),
        data_root=tmp_path / "state",
    )
    shutil.rmtree(folder)
    visual = VisualDiffService(package.visual_config_path)
    assert visual.current_default_sprite_id() == "sana"
    assert visual.resolve_expression_image("school", "normal", "004").stem == "normal"
    assert visual.resolve_expression_image("school", "index_finger", "004").stem == "pointing"
    assert visual.rules == rules
    exported = tmp_path / "shared"
    export_character_folder(package, exported)
    assert json.loads((exported / "rules.json").read_text()) == rules
    assert load_character_package(exported).manifest.visuals.costumes[0].expression_sprites


def test_interrupted_import_is_not_listed_or_selectable(folder, tmp_path):
    surface = management(tmp_path)
    surface.write_config({"character": {"package_dir": "previous"}})
    staging = tmp_path / "data" / "characters" / ".import-interrupted" / "package"
    shutil.copytree(folder, staging)

    assert not any(Path(item["dir"]) == staging for item in surface.list_characters())
    with pytest.raises(ValueError, match="请先导入"):
        surface.select_character(staging)
    assert surface.read_config()["character"]["package_dir"] == "previous"


@pytest.mark.parametrize("failure", ["missing", "escape", "settings_escape", "symlink", "corrupt", "case_collision"])
def test_failed_import_keeps_selection_and_source(folder, tmp_path, failure):
    surface = management(tmp_path)
    surface.write_config({"character": {"package_dir": "previous"}})
    meta = json.loads((folder / "meta.json").read_text())
    if failure == "missing":
        meta["visuals"]["sprites"]["sana"] = "absent.png"
    elif failure == "escape":
        meta["visuals"]["sprites"]["sana"] = "../outside.png"
    elif failure == "settings_escape":
        meta["settings_background"] = "../outside.png"
    elif failure == "symlink":
        symlink_or_skip(folder / "linked.png", folder / "sana.png")
        meta["visuals"]["sprites"]["sana"] = "linked.png"
    elif failure == "case_collision":
        (tmp_path / "data" / "characters" / "Sana").mkdir(parents=True)
    else:
        (folder / "sana.png").write_bytes(b"broken")
    (folder / "meta.json").write_text(json.dumps(meta))
    with pytest.raises((ValueError, OSError)):
        surface.import_character(folder)
    assert surface.read_config()["character"]["package_dir"] == "previous"
    assert (folder / "meta.json").is_file()
    assert not list((tmp_path / "data" / "characters").glob("*/*/meta.json"))


def test_private_save_restores_once_and_public_export_excludes_it(folder, tmp_path):
    from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
    from spica.galgame.models import CompanionBeat

    installed = import_character_folder(folder, tmp_path / "installed")
    package = prepare_character_package(installed, data_root=tmp_path / "first")
    first = SimpleNamespace(
        memory_store=SQLiteMemoryStore(tmp_path / "first.sqlite"),
        recent_memory=RecentMemory(),
        game_memory_adapter=GameMemorySqliteAdapter(tmp_path / "game-first.sqlite"),
    )
    first.game_memory_adapter.add_companion_beat(
        CompanionBeat(
            beat_id="shared",
            game_id="game",
            content="一起看到春雪",
            scope={"character_id": "sana", "user_id": "user"},
        )
    )
    first.memory_store.add_memory("sana::default", "user", "喜欢红茶")
    first.memory_store.add_memory("spica::default", "user", "Spica 私人记忆")
    first.recent_memory.append_turn("sana::default", "明天继续", "約束ね。")
    first.recent_memory.append_turn(
        "sana::game:test", "游戏 OCR", "游戏旁白", interaction_mode="galgame"
    )
    snapshot = export_character_save(package, first)
    assert len(snapshot["memories"]) == 1
    private = tmp_path / "private"
    export_character_folder(package, private, memory=snapshot)
    restored_package = prepare_character_package(
        import_character_folder(private, tmp_path / "second-installed"),
        data_root=tmp_path / "second",
    )
    state = Path(restored_package.state_dir)
    second = SimpleNamespace(
        memory_store=SQLiteMemoryStore(tmp_path / "second.sqlite"),
        recent_memory=RecentMemory(path=state / "recent.json", key_prefix="sana::"),
        game_memory_adapter=GameMemorySqliteAdapter(tmp_path / "game-second.sqlite"),
    )
    restore_character_save(restored_package, second)
    assert (
        second.memory_store.list_memories("sana::default")[0]["content"] == "喜欢红茶"
    )
    assert (
        second.game_memory_adapter.companion_beats("game", "user", "sana")[0].content
        == "一起看到春雪"
    )
    assert (
        RecentMemory(path=state / "recent.json", key_prefix="sana::").get_recent(
            "sana::default"
        )[0]["assistant_text"]
        == "約束ね。"
    )
    second.recent_memory.clear("sana::default")
    restore_character_save(restored_package, second)
    assert second.recent_memory.get_recent("sana::default") == []
    public = tmp_path / "public"
    export_character_folder(restored_package, public)
    assert "memory_file" not in json.loads((public / "meta.json").read_text())
    assert not (public / "memory" / "personal.json").exists()
    assert "Spica 私人记忆" not in json.dumps(snapshot, ensure_ascii=False)
    assert "游戏 OCR" not in json.dumps(snapshot, ensure_ascii=False)


def test_models_and_reference_parameters_reach_existing_tts_service(
    folder, tmp_path, monkeypatch
):
    import wave
    from spica.host import app_host
    from agent_tools.tts import load_tts_config
    from spica.host.character_packages import character_song_config

    base_tts = load_tts_config()
    base_tts["output_dir"] = str(tmp_path / "generated" / "voice")
    monkeypatch.setattr("agent_tools.tts.load_tts_config", lambda path=None:
                        base_tts if path is None else load_tts_config(path))

    for name in ("voice.ckpt", "voice.pth", "song.pth", "song.index"):
        (folder / name).write_bytes(b"test weights, never deserialized")
    with wave.open(str(folder / "reference.wav"), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(32000)
        output.writeframes(b"\0\0" * 32000)
    for name in ("happy-extra.wav", "angry-extra.wav"):
        shutil.copyfile(folder / "reference.wav", folder / name)
    meta = json.loads((folder / "meta.json").read_text())
    meta["tts"] = {
        "gpt": "voice.ckpt",
        "sovits": "voice.pth",
        "reference": {"audio": "reference.wav", "text": "おはよう",
                      "additional_audio": ["happy-extra.wav"]},
        "emotions": {"angry": {"audio": "reference.wav", "text": "こら。",
                               "additional_audio": ["angry-extra.wav"]}},
        "parameters": {"top_k": 5},
    }
    meta["rvc"] = {"model": "song.pth", "index": "song.index"}
    (folder / "meta.json").write_text(json.dumps(meta))
    package = prepare_character_package(
        import_character_folder(folder, tmp_path / "installed"),
        data_root=tmp_path / "state",
    )
    config = load_tts_config(package.tts_config_path)
    assert Path(config["output_dir"]) == tmp_path / "generated" / "voice" / "sana"
    seen = []
    sentinel = object()
    monkeypatch.setattr(
        app_host,
        "GPTSoVITSTool",
        lambda *, config_path: seen.append(config_path) or sentinel,
    )
    host = app_host.AppHost()
    from spica.config.schema import AppConfig

    host.config = AppConfig()
    monkeypatch.setattr(
        host.registry, "resolve_tts", lambda provider, config, service: service
    )
    _, service, adapter = host._resolve_tts_assembly(config)
    assert service is adapter is sentinel
    assert seen == [package.tts_config_path]
    assert Path(config["gpt_model_path"]).is_relative_to(package.package_root)
    assert config["emotions"]["happy"]["prompt_text"] == "おはよう"
    assert config["tts_params"]["top_k"] == 5
    from agent_tools.tts.gptsovits.service import GPTSoVITSTool
    import numpy as np

    calls = []

    def synthesize_chunks(**kwargs):
        calls.append(kwargs)
        return [(32000, np.zeros(320, dtype=np.float32))]

    tool = GPTSoVITSTool(package.tts_config_path)
    tool._driver = SimpleNamespace(
        i18n=lambda value: value, load=lambda **_kwargs: None,
        synthesize_chunks=synthesize_chunks,
        generation=0,
    )
    for emotion in ("happy", "angry"):
        assert tool.synthesize("はい。", emotion)["ok"]
        references = calls[-1]["inp_refs"]
        assert len(references) == 1
        assert Path(references[0]).name == f"{emotion}-extra.wav"
        assert Path(references[0]).is_relative_to(package.package_root)
        assert Path(references[0]).is_file()
    song = character_song_config(
        package, {"enabled": True, "generated_root": str(tmp_path / "generated" / "song"),
                  "rvc": {"runtime_python": "machine-python"}}
    )
    assert Path(song["generated_root"]) == tmp_path / "generated" / "song" / "sana"
    assert song["rvc"]["runtime_python"] == "machine-python"
    assert song["rvc"]["voice_model"] == "sana"
    assert Path(song["rvc"]["voices"]["sana"]["model_path"]).is_file()


def test_runtime_material_is_imported_versioned_and_uses_only_declared_author_sections(folder, tmp_path):
    from spica.conversation.character_loader import select_character_material
    meta = json.loads((folder/'meta.json').read_text())
    meta['runtime_prompt_file'] = 'runtime_prompt.json'
    (folder/'meta.json').write_text(json.dumps(meta))
    (folder/'self.md').write_text('## 部活\n\n{{char}}和原作新吾一起照顾动物。\n\n## 家庭\n\n原作家人。', encoding='utf-8')
    material = dict(schema_version=1, core='{{char}}与{{user}}熟悉地说话。',
        expressions=[dict(scenes=['home.welcome'], triggers=[], text='笑着迎接，但不猜测下班。')],
        background=[dict(file='self.md', heading='## 部活', triggers=['部活'])])
    (folder/'runtime_prompt.json').write_text(json.dumps(material, ensure_ascii=False), encoding='utf-8')
    first = import_character_folder(folder, tmp_path/'installed')
    assert (Path(first.skill_dir)/'runtime_prompt.json').is_file()
    profile = build_character_profile(None, first.skill_dir, '小明')
    core, background = select_character_material(profile, '部活怎么样')
    assert '紗凪与小明' in core and '照顾动物' in background and '原作家人' not in background
    assert '原作新吾' in background and '小明' not in background
    material['core'] += '也有自己的意见。'
    (folder/'runtime_prompt.json').write_text(json.dumps(material, ensure_ascii=False), encoding='utf-8')
    second = import_character_folder(folder, tmp_path/'installed')
    assert first.revision != second.revision
    assert build_character_profile('作者自己的整段原文', first.skill_dir) == '作者自己的整段原文'


@pytest.mark.parametrize('invalid', ['version', 'memory', 'heading', 'symlink'])
def test_invalid_runtime_material_does_not_replace_selected_role(folder, tmp_path, invalid):
    surface = management(tmp_path)
    surface.import_character(folder)
    before = surface.read_config()
    meta = json.loads((folder/'meta.json').read_text())
    meta['runtime_prompt_file'] = 'runtime_prompt.json'
    material = dict(schema_version=1, core='新的角色核心', background=[])
    if invalid == 'version':
        material['schema_version'] = 2
    elif invalid == 'memory':
        material['background'] = [dict(file='memory.json', heading='## 私密')]
    elif invalid == 'heading':
        material['background'] = [dict(file='persona.md', heading='## 不存在')]
    (folder/'meta.json').write_text(json.dumps(meta))
    if invalid == 'symlink':
        outside = tmp_path/'outside.json'
        outside.write_text(json.dumps(material))
        try:
            (folder/'runtime_prompt.json').symlink_to(outside)
        except OSError as exc:
            if getattr(exc, 'winerror', None) == 1314:
                pytest.skip('Windows requires file symlink privileges for this case')
            raise
    else:
        (folder/'runtime_prompt.json').write_text(json.dumps(material))
    with pytest.raises((ValueError, OSError)):
        surface.import_character(folder)
    assert surface.read_config() == before


def add_floating_animation(folder, *, interactions=False, idle_actions=False):
    directory = folder / 'floating'
    directory.mkdir()
    frames = [Image.new('RGBA', (16, 16), color) for color in ('red', 'blue')]
    frames[0].save(directory / 'idle.webp', save_all=True, append_images=frames[1:], duration=50, loop=0)
    frames[0].save(directory / 'poster.png')
    spec = dict(canvas=[16, 16], fps=20, poster='poster.png',
                states={'idle': dict(file='idle.webp', duration_ms=100, frames=2)})
    if interactions:
        for state in ('click', 'dizzy'):
            shutil.copyfile(directory / 'idle.webp', directory / f'{state}.webp')
            spec['states'][state] = dict(file=f'{state}.webp', duration_ms=100, frames=2)
        for state, color in (('flustered', 'yellow'), ('dizzy_held', 'purple')):
            Image.new('RGBA', (16, 16), color).save(directory / f'{state}.png')
            spec[state] = f'{state}.png'
    if idle_actions:
        spec['idle_actions'] = {}
        for action, weight in (('grass_flute', 1), ('pizza', 1), ('breeze', 4)):
            shutil.copyfile(directory / 'idle.webp', directory / f'{action}.webp')
            spec['idle_actions'][action] = dict(file=f'{action}.webp', frames=2,
                                              duration_ms=100, weight=weight)
    (directory / 'animation.json').write_text(json.dumps(spec))
    meta = json.loads((folder / 'meta.json').read_text())
    meta['visuals']['floating'] = 'floating/animation.json'
    (folder / 'meta.json').write_text(json.dumps(meta))
    return directory, spec


def test_floating_assets_survive_import_export_and_create_new_revision(folder, tmp_path):
    directory, spec = add_floating_animation(folder, interactions=True, idle_actions=True)
    spec['states']['dizzy']['start_frame'] = 1
    (directory / 'animation.json').write_text(json.dumps(spec))
    first = import_character_folder(folder, tmp_path / 'installed')
    spec['suggested_display_px'] = 120
    (directory / 'animation.json').write_text(json.dumps(spec))
    second = import_character_folder(folder, tmp_path / 'installed')
    assert first.revision != second.revision
    shutil.rmtree(folder)
    exported = tmp_path / 'export'
    export_character_folder(second, exported)
    loaded = load_character_package(exported)
    assert loaded.character_id == 'sana'
    assert loaded.manifest.visuals.floating == 'floating/animation.json'
    assert (exported / 'floating/idle.webp').is_file()
    for relative in ('click.webp', 'dizzy.webp', 'flustered.png', 'dizzy_held.png'):
        assert (exported / 'floating' / relative).is_file()
    assert json.loads((exported / 'floating/animation.json').read_text())['suggested_display_px'] == 120
    assert json.loads((exported / 'floating/animation.json').read_text())['states']['dizzy']['start_frame'] == 1
    for action in spec['idle_actions']:
        assert (exported / 'floating' / f'{action}.webp').is_file()
    assert json.loads((exported / 'floating/animation.json').read_text())['idle_actions']['breeze']['weight'] == 4


@pytest.mark.parametrize('delays,declared_duration,valid', [
    ([50, 150], 200, True),  # The encoder coalesces three identical timeline frames.
    ([50, 150], 100, False),
    ([0, 100], 100, False),
])
def test_floating_timing_uses_encoded_frame_delays(folder, delays, declared_duration, valid):
    from spica.core.floating_character import validate_floating_images

    directory, spec = add_floating_animation(folder, idle_actions=True)
    frames = [Image.new('RGBA', (16, 16), color) for color in ('red', 'blue')]
    frames[0].save(directory / 'breeze.webp', save_all=True, append_images=frames[1:],
                   duration=delays, loop=0)
    spec['idle_actions']['breeze']['duration_ms'] = declared_duration
    (directory / 'animation.json').write_text(json.dumps(spec))
    if valid:
        validate_floating_images(folder, 'floating/animation.json')
    else:
        with pytest.raises(ValueError, match='timing'):
            validate_floating_images(folder, 'floating/animation.json')


@pytest.mark.parametrize('invalid', ['path', 'symlink', 'frames', 'held_path', 'held_size', 'start_frame',
                                    'idle_path', 'idle_frames', 'idle_weight', 'idle_interval'])
def test_invalid_floating_update_preserves_selected_character(folder, tmp_path, invalid):
    directory, spec = add_floating_animation(folder)
    surface = management(tmp_path)
    surface.import_character(folder)
    before = surface.read_config()
    if invalid == 'path':
        spec['poster'] = '../sana.png'
    elif invalid == 'symlink':
        (directory / 'poster.png').unlink()
        try:
            (directory / 'poster.png').symlink_to(folder / 'sana.png')
        except OSError:
            pytest.skip('platform does not allow test symlinks')
    elif invalid == 'held_path':
        spec['dizzy_held'] = '../sana.png'
    elif invalid == 'held_size':
        Image.new('RGBA', (32, 32), 'purple').save(directory / 'held.png')
        spec['dizzy_held'] = 'held.png'
    elif invalid == 'start_frame':
        spec['states']['idle']['start_frame'] = 2
    elif invalid.startswith('idle_'):
        spec['idle_actions'] = {'breeze': dict(file='idle.webp', frames=2, duration_ms=100, weight=1)}
        if invalid == 'idle_path':
            spec['idle_actions']['breeze']['file'] = '../sana.png'
        elif invalid == 'idle_frames':
            spec['idle_actions']['breeze']['frames'] = 3
        elif invalid == 'idle_weight':
            spec['idle_actions']['breeze']['weight'] = 0
        else:
            spec['idle_interval_ms'] = [90000, 45000]
    else:
        spec['states']['idle']['frames'] = 3
    (directory / 'animation.json').write_text(json.dumps(spec))
    with pytest.raises(ValueError):
        surface.import_character(folder)
    assert surface.read_config() == before
