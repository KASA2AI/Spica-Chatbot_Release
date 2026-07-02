"""Runtime Cutover Rehearsal step 3: repo default OCR provider.

Pins the intentional production-default change in ``data/config/app.yaml`` while
leaving the schema built-in default and legacy RapidOCR seam as fallback paths.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_tools.function_tools.screen.backends import ocr_runtime
from spica.adapters.ocr import RapidOcrAdapter, RapidOcrOrtAdapter
from spica.config.env_roster import consumed_env_names
from spica.config.manager import ConfigManager
from spica.config.schema import AppConfig, OcrConfig
from spica.host.agent_assembly import build_ocr_adapter
from spica.host.app_host import _install_ocr_runtime_provider


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_YAML = REPO_ROOT / "data" / "config" / "app.yaml"


def _masked_env() -> dict[str, str]:
    return {name: "" for name in consumed_env_names()}


def test_repo_app_yaml_live_ocr_provider_resolves_to_rapidocr_ort():
    with patch("spica.config.manager.load_dotenv", lambda *a, **k: None), patch.dict(
        os.environ, _masked_env(), clear=True
    ):
        config = ConfigManager(APP_YAML).load()

    assert config.ocr.provider == "rapidocr_ort"
    assert config.ocr.fallback_provider == "rapidocr"


def test_schema_builtin_ocr_default_remains_legacy_rapidocr():
    assert OcrConfig().provider == "rapidocr"
    assert OcrConfig().fallback_provider == "rapidocr"


def test_rapidocr_ort_factory_selects_ort_adapter():
    assert isinstance(build_ocr_adapter("rapidocr_ort"), RapidOcrOrtAdapter)


def test_install_hook_installs_repo_default_rapidocr_ort_provider():
    ocr_runtime.reset_active_ocr_provider()
    try:
        config = AppConfig(ocr=OcrConfig(provider="rapidocr_ort", fallback_provider="rapidocr"))
        services = SimpleNamespace(ocr_adapter=RapidOcrOrtAdapter(runtime=object()))

        _install_ocr_runtime_provider(config, services)

        assert ocr_runtime.get_active_ocr_provider() is services.ocr_adapter
    finally:
        ocr_runtime.reset_active_ocr_provider()


def test_install_hook_resets_stale_provider_for_legacy_rapidocr_config():
    stale = RapidOcrOrtAdapter(runtime=object())
    ocr_runtime.set_active_ocr_provider(stale)
    try:
        config = AppConfig(ocr=OcrConfig(provider="rapidocr", fallback_provider="rapidocr"))
        services = SimpleNamespace(ocr_adapter=RapidOcrAdapter())

        _install_ocr_runtime_provider(config, services)

        assert ocr_runtime.get_active_ocr_provider() is None
    finally:
        ocr_runtime.reset_active_ocr_provider()
