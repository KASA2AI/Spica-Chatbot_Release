from pathlib import Path
import sys
import shutil
import pytest
import yaml
from scripts.setup_desktop import initial_config, write_initial_config
from spica.config.schema import AppConfig
from spica.host.character_packages import load_character_package


def test_first_run_text_config_is_portable_and_preserves_existing_file(tmp_path):
    document = initial_config('my-model', 'https://example.invalid/v1')
    config = AppConfig.model_validate(document)
    assert config.platform.os == 'auto' and config.stt.backend == 'qwen_asr'
    assert not config.tts.enabled and not config.home.enabled and not config.memory.consolidation_enabled
    assert config.stt.device == 'cpu' and config.stt.compute_type == 'float32'
    assert load_character_package(config.character.package_dir).package_root
    path = tmp_path / 'app.yaml'
    write_initial_config(path, document)
    assert yaml.safe_load(path.read_text()) == document
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_initial_config(path, initial_config('other'))
    assert path.read_bytes() == original


def test_config_only_reports_the_current_python_without_creating_an_environment(tmp_path, monkeypatch, capsys):
    from scripts import setup_desktop

    shutil.copytree(setup_desktop.ROOT / 'Desktop-Packs/Characters/Examples/static',
                    tmp_path / 'Desktop-Packs/Characters/Examples/static')
    monkeypatch.setattr(setup_desktop, "ROOT", tmp_path)
    assert setup_desktop.main(["--write-config", "--model", "my-model"]) == 0
    assert f"Python: {sys.executable}" in capsys.readouterr().out
    assert not (tmp_path / ".venv-desktop").exists()
    config = yaml.safe_load((tmp_path / 'data/config/app.yaml').read_text(encoding='utf-8'))
    from spica.host.character_packages import prepare_character_package
    package = prepare_character_package(load_character_package(config['character']['package_dir']),
                                        data_root=tmp_path / 'data/runtime')
    assert Path(package.visual_config_path).is_file()


def test_explicit_text_setup_works_with_shipped_config_and_preserves_other_settings(tmp_path, monkeypatch):
    from scripts import setup_desktop

    shutil.copytree(setup_desktop.ROOT / 'Desktop-Packs/Characters/Examples/static',
                    tmp_path / 'Desktop-Packs/Characters/Examples/static')
    config_path = tmp_path / 'data/config/app.yaml'
    config_path.parent.mkdir(parents=True)
    config_path.write_text('memory:\n  recent_memory_turns: 7\n', encoding='utf-8')
    monkeypatch.setattr(setup_desktop, 'ROOT', tmp_path)
    assert setup_desktop.main(['--configure-text', '--model', 'my-model']) == 0
    config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    assert config['memory']['recent_memory_turns'] == 7
    assert config['llm']['model'] == 'my-model'
    assert not config['tts']['enabled'] and not config['home']['enabled']
    assert Path(config['character']['package_dir'], '.installed.json').is_file()
