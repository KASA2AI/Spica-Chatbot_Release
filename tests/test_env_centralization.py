"""P0b step 1 pins: env reads centralized in the config layer.

- manager.screen_env_overrides / respeaker_env_overrides: RAW passthrough
  (no strip, no coercion -- that stays at the domain loader), field-keyed so
  consumers hold no env-name knowledge, read at CALL time.
- runtime_env: the vendored-runtime shims moved from service.py keep their
  exact behaviour (HF_HOME pinned only when unset, dirs under the cache root,
  proxy strip).
- secrets: residual DeepSeek legacy names WARN (never silent) -- the dual-name
  died with B2; there is no reader left to be "compatible" with.
- F19 guard: qt_overlay.main()'s first statement must stay load_secrets().
"""

from __future__ import annotations

import ast
import logging
import os
from pathlib import Path

import pytest

from spica.config.env_roster import RESPEAKER_ENV_MAP, SCREEN_ENV_MAP
from spica.config.manager import respeaker_env_overrides, screen_env_overrides
from spica.config.runtime_env import (
    prime_vendored_runtime_cache_env,
    resolve_runtime_cache_root,
    strip_proxy_env_for_vendored_runtime,
)
from spica.config.secrets import load_secrets

REPO_ROOT = Path(__file__).resolve().parents[1]


# -- domain override functions: raw, field-keyed, call-time ---------------------


def test_screen_overrides_cover_all_fields_and_pass_raw_values(monkeypatch):
    monkeypatch.setenv("SPICA_SCREEN_MAX_SIDE", "  99999  ")  # raw: NOT stripped here
    monkeypatch.delenv("SPICA_SCREEN_DEVICE", raising=False)
    values = screen_env_overrides()
    assert set(values) == set(SCREEN_ENV_MAP)  # all 15 fields, keyed by field name
    assert values["max_side"] == "  99999  "
    assert values["device"] is None  # unset -> None, not ""


def test_screen_overrides_read_at_call_time(monkeypatch):
    monkeypatch.setenv("SPICA_SCREEN_PROVIDER", "first")
    assert screen_env_overrides()["provider"] == "first"
    monkeypatch.setenv("SPICA_SCREEN_PROVIDER", "second")
    assert screen_env_overrides()["provider"] == "second"


def test_screen_loader_takes_env_through_manager(monkeypatch, tmp_path):
    from agent_tools.function_tools.screen.config import load_screen_config

    for name in SCREEN_ENV_MAP.values():
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("SPICA_SCREEN_DEVICE", "cpu")
    assert load_screen_config(tmp_path / "absent.json").device == "cpu"


def test_respeaker_overrides_cover_all_fields(monkeypatch):
    monkeypatch.setenv("RESPEAKER_INPUT_DEVICE_INDEX", "3")
    monkeypatch.delenv("RESPEAKER_TUNING_PATH", raising=False)
    values = respeaker_env_overrides()
    assert set(values) == set(RESPEAKER_ENV_MAP)
    assert values["input_device_index"] == "3"
    assert values["tuning_path"] is None


# -- runtime_env shims (moved from service.py, behaviour pinned) -----------------


def test_resolve_cache_root_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SPICA_RUNTIME_CACHE_DIR", str(tmp_path / "cache"))
    assert resolve_runtime_cache_root() == (tmp_path / "cache").resolve()


def test_prime_respects_existing_hf_home(monkeypatch, tmp_path):
    monkeypatch.setenv("SPICA_RUNTIME_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("HF_HOME", "/custom/hf")
    monkeypatch.setenv("NUMBA_CACHE_DIR", "/custom/numba")
    prime_vendored_runtime_cache_env()
    assert os.environ["HF_HOME"] == "/custom/hf"  # set -> never touched
    assert os.environ["NUMBA_CACHE_DIR"] == "/custom/numba"


def test_prime_sets_hf_home_and_cache_dirs_when_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("SPICA_RUNTIME_CACHE_DIR", str(tmp_path))
    for name in ("HF_HOME", "NUMBA_CACHE_DIR", "MPLCONFIGDIR", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    prime_vendored_runtime_cache_env()
    assert os.environ["HF_HOME"] == str(Path.home() / ".cache" / "huggingface")
    for name, dirname in (
        ("NUMBA_CACHE_DIR", "numba"),
        ("MPLCONFIGDIR", "matplotlib"),
        ("XDG_CACHE_HOME", "xdg"),
    ):
        assert os.environ[name] == str(tmp_path.resolve() / dirname)
        assert (tmp_path / dirname).is_dir()


def test_strip_proxy_env_removes_all_variants(monkeypatch):
    for name in ("all_proxy", "ALL_PROXY", "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.setenv(name, "http://proxy:8080")
    strip_proxy_env_for_vendored_runtime()
    for name in ("all_proxy", "ALL_PROXY", "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        assert name not in os.environ


# -- DeepSeek legacy residue: warn, never silent ---------------------------------


def test_legacy_deepseek_residue_warns(monkeypatch, caplog):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "residual-value")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "")
    with caplog.at_level(logging.WARNING, logger="spica.config.secrets"):
        load_secrets()
    messages = [r.getMessage() for r in caplog.records]
    assert any("DEEPSEEK_API_KEY" in m for m in messages)
    assert not any("DEEPSEEK_BASE_URL" in m for m in messages)  # empty == unset


def test_no_legacy_residue_loads_quietly(monkeypatch, caplog):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")  # "" survives dotenv re-priming
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "")
    with caplog.at_level(logging.WARNING, logger="spica.config.secrets"):
        load_secrets()
    assert caplog.records == []


# -- F19 recurrence guard: the entry point primes env before constructing -------


def test_qt_overlay_main_primes_secrets_first():
    """AST pin: the FIRST statement of qt_overlay.main() is load_secrets().
    Anything constructed before priming reads an un-primed environment and
    stays wrong forever (F19) -- this keeps the P0a fix from being hoisted away."""
    tree = ast.parse((REPO_ROOT / "ui" / "qt_overlay.py").read_text(encoding="utf-8"))
    main_def = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    statements = main_def.body
    if (  # skip a docstring if one ever appears
        isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        statements = statements[1:]
    first = statements[0]
    assert (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Call)
        and isinstance(first.value.func, ast.Name)
        and first.value.func.id == "load_secrets"
    ), "qt_overlay.main() must call load_secrets() as its FIRST statement (CLAUDE.md #10 / F19)"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
