"""P0b step 2a pins: the screen config is resolved ONCE by the host and the
injected instance is what production consumers use.

- AppHost.__init__ resolves ScreenPipelineConfig and the REGISTERED tools
  (inspect_screen / watch_game_screen) hold that very instance;
- an injected adapter never falls back to load_screen_config() at run time;
- the Moondream manager singleton reuses the same manager for the same
  config signature (inject != reload) and rebuilds only when the signature
  actually changes (model_id/revision/device/... value change);
- AppConfig.screen resolves through the same coercion engine (manager env
  node + ScreenConfig validator), so the typed section and the loader agree.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_tools.function_tools.screen.config import ScreenPipelineConfig
from agent_tools.function_tools.screen.model_manager import (
    clear_moondream_manager,
    get_moondream_manager,
)
from spica.adapters.tools.screen import InspectScreenTool
from spica.config.env_roster import SCREEN_ENV_MAP
from spica.config.manager import ConfigManager
from spica.config.schema import ScreenConfig
from spica.host.app_host import AppHost


def _pipeline_config(**overrides) -> ScreenPipelineConfig:
    return ScreenPipelineConfig(**{**ScreenConfig().model_dump(), **overrides})


class _SpyScreen:
    def __init__(self):
        self.calls = []

    def analyze_image(self, image, mode, prompt, **kwargs):
        self.calls.append((image, mode, prompt, kwargs))
        return {"schema_version": "screen_observation.v1"}


# -- host wiring: registered production tools hold the host-resolved instance ---


def test_app_host_resolves_once_and_registered_tools_hold_the_instance():
    host = AppHost()
    assert isinstance(host.screen_config, ScreenPipelineConfig)
    inspect_handler = host.registry.tool_handler("inspect_screen")
    watch_handler = host.registry.tool_handler("watch_game_screen")
    assert inspect_handler.__self__._config is host.screen_config
    assert watch_handler.__self__._config is host.screen_config


def test_injected_adapter_never_calls_the_fallback_loader():
    spy = _SpyScreen()
    tool = InspectScreenTool(spy, config=_pipeline_config())
    with patch(
        "spica.adapters.tools.screen.capture_full_screen",
        return_value=SimpleNamespace(image="IMG", metadata={}),
    ), patch(
        "spica.adapters.tools.screen.load_screen_config",
        side_effect=AssertionError("injected path must not re-load config"),
    ):
        out = tool.run(target="full_screen", question="帮我看看屏幕上有没有报错")
    assert out == {"schema_version": "screen_observation.v1"}
    assert spy.calls[0][3]["config"] is tool._config  # the injected instance is used


# -- model manager: same signature -> same manager (inject != reload) -----------


def test_moondream_manager_reuses_same_manager_for_same_config():
    clear_moondream_manager()
    try:
        config = _pipeline_config()
        first = get_moondream_manager(config)
        assert get_moondream_manager(config) is first  # same instance: no rebuild
        # equal VALUES (fresh equal instance) keep the same manager too --
        # the cache keys on the signature values, not object identity
        assert get_moondream_manager(_pipeline_config()) is first
        # a signature-relevant change rebuilds
        changed = get_moondream_manager(_pipeline_config(revision="other-rev"))
        assert changed is not first
    finally:
        clear_moondream_manager()


# -- AppConfig.screen: typed section runs the same coercion engine --------------


@pytest.fixture
def masked_screen_env(monkeypatch):
    for name in SCREEN_ENV_MAP.values():
        monkeypatch.setenv(name, "")
    return monkeypatch


def test_app_config_screen_defaults(masked_screen_env, tmp_path):
    config = ConfigManager(tmp_path / "absent.yaml").load()
    assert config.screen == ScreenConfig()
    assert config.screen.model_dump() == ScreenConfig().model_dump()


def test_app_config_screen_env_folds_with_screen_coercion(masked_screen_env, tmp_path):
    masked_screen_env.setenv("SPICA_SCREEN_MAX_SIDE", "99999")  # clamps, not raises
    masked_screen_env.setenv("SPICA_SCREEN_ENABLED", "off")
    masked_screen_env.setenv("SPICA_SCREEN_DEVICE", "  cpu  ")
    config = ConfigManager(tmp_path / "absent.yaml").load()
    assert config.screen.max_side == 4096
    assert config.screen.enabled is False
    assert config.screen.device == "cpu"


def test_app_config_screen_agrees_with_loader(masked_screen_env, tmp_path):
    """The typed section and load_screen_config (no json file present) must
    resolve identically -- one coercion engine, two entry points."""
    from agent_tools.function_tools.screen.config import load_screen_config

    masked_screen_env.setenv("SPICA_SCREEN_DTYPE", "FLOAT16")
    masked_screen_env.setenv("SPICA_SCREEN_INFER_TIMEOUT_SEC", "-5")
    app_screen = ConfigManager(tmp_path / "absent.yaml").load().screen
    loader_config = load_screen_config(tmp_path / "absent.json")
    assert app_screen.model_dump() == loader_config.__dict__
