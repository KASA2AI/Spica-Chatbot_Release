from pathlib import Path
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
