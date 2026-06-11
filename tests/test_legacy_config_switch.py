"""P0b step 3-1 pins: the carrier switch (D6 -- one WHOLE chain by legacy-file
existence, never a merge of the two).

- legacy file present -> the old chain's values win entirely + a migration
  WARNING; absent -> the new chain (env > app.yaml > defaults) + silence;
- a renamed *.migrated file is NOT read (rollback/migration end state);
- the song app.yaml chain runs the SAME composition engine as the json chain
  (equivalence pin) and never mutates AppConfig state;
- plugins keep the str-shorthand semantics through the typed section;
- the app.yaml comment template parses to {} (resolution-neutral by math).
"""

from __future__ import annotations

import logging

import pytest

from agent_tools.function_tools.screen.config import (
    load_screen_config,
    resolve_effective_screen_config,
)
from agent_tools.function_tools.song.config import (
    PROJECT_ROOT,
    load_song_config,
    resolve_effective_song_config,
)
from spica.config.env_roster import SCREEN_ENV_MAP
from spica.config.manager import ConfigManager
from spica.config.schema import AppConfig
from spica.plugins.manifest import PluginEntry, resolve_effective_plugin_entries

REPO_ROOT = PROJECT_ROOT  # same repo root, reuse the song module's anchor


@pytest.fixture
def masked_screen_env(monkeypatch):
    for name in SCREEN_ENV_MAP.values():
        monkeypatch.setenv(name, "")
    return monkeypatch


# -- screen switch ---------------------------------------------------------------


def test_screen_legacy_present_wins_entirely_and_warns(masked_screen_env, tmp_path, caplog):
    legacy = tmp_path / "screen_vision_config.json"
    legacy.write_text('{"device": "cpu", "max_side": 512}', encoding="utf-8")
    # app.yaml says otherwise -- and must be IGNORED (whole-chain switch, no merge)
    app_config = AppConfig.model_validate({"screen": {"device": "cuda", "max_side": 1024}})
    with caplog.at_level(logging.WARNING, logger="agent_tools.function_tools.screen.config"):
        config = resolve_effective_screen_config(config=app_config, legacy_path=legacy)
    assert (config.device, config.max_side) == ("cpu", 512)
    assert config.model_id == "vikhyatk/moondream2"  # untouched key -> defaults, old chain
    assert any("migrate_config_p0b" in r.getMessage() for r in caplog.records)


def test_screen_legacy_absent_uses_app_yaml_chain_silently(masked_screen_env, tmp_path, caplog):
    app_yaml = tmp_path / "app.yaml"
    app_yaml.write_text("screen:\n  device: cpu\n  max_side: 99999\n", encoding="utf-8")
    loaded = ConfigManager(app_yaml).load()
    with caplog.at_level(logging.WARNING, logger="agent_tools.function_tools.screen.config"):
        config = resolve_effective_screen_config(
            config=loaded, legacy_path=tmp_path / "absent.json"
        )
    assert (config.device, config.max_side) == ("cpu", 4096)  # validator clamps file values
    assert caplog.records == []


def test_screen_new_chain_env_beats_app_yaml(masked_screen_env, tmp_path):
    app_yaml = tmp_path / "app.yaml"
    app_yaml.write_text("screen:\n  device: cpu\n", encoding="utf-8")
    masked_screen_env.setenv("SPICA_SCREEN_DEVICE", "cuda")
    loaded = ConfigManager(app_yaml).load()
    config = resolve_effective_screen_config(config=loaded, legacy_path=tmp_path / "absent.json")
    assert config.device == "cuda"  # env > app.yaml > defaults


def test_screen_migrated_rename_is_not_read(masked_screen_env, tmp_path):
    (tmp_path / "screen_vision_config.json.migrated").write_text(
        '{"device": "cpu"}', encoding="utf-8"
    )
    config = resolve_effective_screen_config(
        config=AppConfig(), legacy_path=tmp_path / "screen_vision_config.json"
    )
    assert config.device == "cuda"  # new chain defaults; the .migrated file is dead


# -- song switch -----------------------------------------------------------------


def test_song_legacy_present_wins_and_warns(tmp_path, caplog):
    legacy = tmp_path / "song_config.json"
    legacy.write_text('{"search": {"limit": 5}}', encoding="utf-8")
    app_config = AppConfig.model_validate({"song": {"search": {"limit": 9}}})
    with caplog.at_level(logging.WARNING, logger="agent_tools.function_tools.song.config"):
        config = resolve_effective_song_config(config=app_config, legacy_path=legacy)
    assert config["search"] == {"limit": 5, "bitrate": 320000}  # old chain, app.yaml ignored
    assert any("migrate_config_p0b" in r.getMessage() for r in caplog.records)


def test_song_new_chain_shares_the_composition_engine(tmp_path):
    """Equivalence pin: the same override dict through the json carrier and the
    app.yaml carrier must compose identically (apart from the carrier label)."""
    override = {"search": {"limit": 7}, "rvc": {"voices": {"spica": {"transpose": 2}}}}
    legacy = tmp_path / "song_config.json"
    legacy.write_text(
        '{"search": {"limit": 7}, "rvc": {"voices": {"spica": {"transpose": 2}}}}',
        encoding="utf-8",
    )
    via_json = load_song_config(legacy)
    via_app = resolve_effective_song_config(
        config=AppConfig.model_validate({"song": override}),
        legacy_path=tmp_path / "absent.json",
    )
    via_json.pop("_config_path")
    label = via_app.pop("_config_path")
    assert via_json == via_app
    assert label.endswith("app.yaml#song")


def test_song_new_chain_never_mutates_app_config(tmp_path):
    app_config = AppConfig.model_validate(
        {"song": {"rvc": {"voices": {"spica": {"model_path": "relative/model.pth"}}}}}
    )
    before = repr(app_config.song)
    resolved = resolve_effective_song_config(config=app_config, legacy_path=tmp_path / "absent.json")
    assert resolved["rvc"]["voices"]["spica"]["model_path"].startswith(str(PROJECT_ROOT))
    assert repr(app_config.song) == before  # path resolution ran on a deep copy


# -- plugins switch ----------------------------------------------------------------


def test_plugins_legacy_present_wins_and_warns(tmp_path, caplog):
    legacy = tmp_path / "plugins.yaml"
    legacy.write_text("plugins:\n  - legacy_plugin\n", encoding="utf-8")
    app_config = AppConfig.model_validate({"plugins": ["app_yaml_plugin"]})
    with caplog.at_level(logging.WARNING, logger="spica.plugins.manifest"):
        entries = resolve_effective_plugin_entries(config=app_config, legacy_path=legacy)
    assert entries == [PluginEntry(name="legacy_plugin")]
    assert any("migrate_config_p0b" in r.getMessage() for r in caplog.records)


def test_plugins_new_chain_keeps_str_shorthand_and_enabled_flag(tmp_path):
    app_config = AppConfig.model_validate(
        {"plugins": ["short_name", {"name": "full", "enabled": False}, "", {"x": 1}]}
    )
    entries = resolve_effective_plugin_entries(
        config=app_config, legacy_path=tmp_path / "absent.yaml"
    )
    assert entries == [
        PluginEntry(name="short_name", enabled=True),
        PluginEntry(name="full", enabled=False),
    ]  # blanks/invalid dropped, manifest semantics preserved


# -- the template itself -----------------------------------------------------------


def test_app_yaml_parses_and_validates():
    """Durable invariant for the shipped app.yaml (3-2 adaptation: the 3-1
    version pinned `== {}` for the pre-migration all-comment template; the
    migrated file legitimately holds sections now). The file must stay valid
    yaml that resolves to a MAPPING and validates against AppConfig -- a broken
    edit (stray half-uncommented key, bad indent, unknown type) goes red here."""
    app_yaml = REPO_ROOT / "data" / "config" / "app.yaml"
    assert app_yaml.is_file()
    data = ConfigManager._read_yaml(app_yaml)
    assert isinstance(data, dict)
    AppConfig.model_validate(data)  # must not raise
