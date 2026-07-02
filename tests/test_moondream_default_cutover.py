"""Runtime Cutover Rehearsal step 1: repo default Moondream provider.

Pins the intentional production-default change in ``data/config/app.yaml`` while
leaving the schema built-in default and legacy seam as rollback/fallback paths.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_tools.function_tools.screen.backends import moondream_runtime
from spica.config.env_roster import SCREEN_ENV_MAP
from spica.config.manager import ConfigManager
from spica.config.schema import ScreenConfig
from spica.host.app_host import AppHost
from spica.local_runtime.vision import MoondreamHfProvider
from spica.plugins.host import PluginHost
from spica.plugins.registry import CapabilityRegistry


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_YAML = REPO_ROOT / "data" / "config" / "app.yaml"


def _masked_env() -> dict[str, str]:
    return {name: "" for name in SCREEN_ENV_MAP.values()}


def test_repo_app_yaml_live_screen_provider_resolves_to_moondream_hf():
    with patch("spica.config.manager.load_dotenv", lambda *a, **k: None), patch.dict(
        os.environ, _masked_env(), clear=True
    ):
        config = ConfigManager(APP_YAML).load()

    assert config.screen.provider == "moondream_hf"


def test_schema_builtin_default_remains_legacy_moondream_local():
    assert ScreenConfig().provider == "moondream_local"


def test_explicit_moondream_local_config_still_selects_legacy_fallback(tmp_path):
    app_yaml = tmp_path / "app.yaml"
    app_yaml.write_text("screen:\n  provider: moondream_local\n", encoding="utf-8")
    with patch("spica.config.manager.load_dotenv", lambda *a, **k: None), patch.dict(
        os.environ, _masked_env(), clear=True
    ):
        config = ConfigManager(app_yaml).load()

    moondream_runtime.reset_active_moondream_provider()
    try:
        assert config.screen.provider == "moondream_local"
        assert moondream_runtime.get_active_moondream_provider() is None
    finally:
        moondream_runtime.reset_active_moondream_provider()


class _FakeChatEngine:
    def __init__(self, services, config):
        self.services = services
        self.config = config
        self.game_binding_provider = None

    def set_game_binding_provider(self, provider):
        self.game_binding_provider = provider


def _fake_character_package():
    return SimpleNamespace(
        character_id="spica",
        char_name="Spica",
        skill_dir=REPO_ROOT / "spica_data" / "Spica",
        visual_config_path=None,
        tts_config_path=None,
    )


def _fake_services():
    return SimpleNamespace(
        ocr_adapter=object(),
        llm_client=object(),
        recent_memory=object(),
        memory_store=object(),
        game_memory_adapter=object(),
        screen_capture_adapter=object(),
        window_locator_adapter=object(),
    )


def _host_initialize_patches():
    return (
        patch("spica.config.manager.load_dotenv", lambda *a, **k: None),
        patch.dict(os.environ, _masked_env(), clear=True),
        patch.object(PluginHost, "load", lambda self: None),
        patch("spica.host.app_host.load_secrets", return_value=SimpleNamespace(openai_api_key="key")),
        patch("spica.host.app_host.load_character_package", return_value=_fake_character_package()),
        patch("spica.host.app_host.load_tts_config", return_value={"provider": "fake_tts"}),
        patch.object(CapabilityRegistry, "resolve_visual", return_value=object()),
        patch.object(CapabilityRegistry, "resolve_tts", return_value=object()),
        patch.object(CapabilityRegistry, "resolve_llm", return_value=object()),
        patch.object(CapabilityRegistry, "resolve_memory", return_value=object()),
        patch("spica.host.app_host.build_agent_services", return_value=_fake_services()),
        patch("spica.host.app_host.ChatEngine", _FakeChatEngine),
        patch.object(AppHost, "_new_reaction_judge", return_value=None),
        patch.object(AppHost, "_build_reaction_engine", return_value=None),
        patch.object(AppHost, "_new_stt_adapter", return_value=object()),
    )


def _initialize_host(host: AppHost) -> None:
    patches = _host_initialize_patches()
    entered = []
    try:
        for item in patches:
            entered.append(item)
            item.__enter__()
        host.initialize()
    finally:
        while entered:
            entered.pop().__exit__(None, None, None)


def test_app_host_default_config_installs_moondream_hf_provider():
    moondream_runtime.reset_active_moondream_provider()
    try:
        with patch("spica.config.manager.load_dotenv", lambda *a, **k: None), patch.dict(
            os.environ, _masked_env(), clear=True
        ):
            host = AppHost()
        assert host.screen_config.provider == "moondream_hf"

        _initialize_host(host)

        provider = moondream_runtime.get_active_moondream_provider()
        assert isinstance(provider, MoondreamHfProvider)
    finally:
        moondream_runtime.reset_active_moondream_provider()


def test_app_host_explicit_moondream_local_installs_no_provider():
    moondream_runtime.reset_active_moondream_provider()
    try:
        with patch("spica.config.manager.load_dotenv", lambda *a, **k: None), patch.dict(
            os.environ, _masked_env(), clear=True
        ):
            host = AppHost()
        host.screen_config = replace(host.screen_config, provider="moondream_local")

        _initialize_host(host)

        assert moondream_runtime.get_active_moondream_provider() is None
    finally:
        moondream_runtime.reset_active_moondream_provider()
