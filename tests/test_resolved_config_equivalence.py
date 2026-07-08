"""P0b Layer B: resolution-semantics equivalence pins (committed half of the gate).

FROZEN for the duration of P0b: steps 2-4 must keep every test here green
WITHOUT EDITING THIS FILE. A semantic change in config resolution (precedence,
coercion, clamping, empty-string fallthrough) must show up as THIS FILE going
red -- never as the pins being adjusted to match new behaviour.

Why this exists next to Layer A (scripts/dump_resolved_config.py): the real
machine sets all 15 SPICA_SCREEN_* vars, so whole resolution branches (unset ->
file -> default fallthrough, clamping, invalid-value recovery) are never
exercised by the live snapshot. This file drives them with synthetic awkward
values generated against the PRE-MIGRATION loaders, pinning the semantics that
the migration must preserve. The golden literals below were captured from the
current (step-0, unmigrated) code and verified green before landing.

Notable quirks pinned ON PURPOSE (current behaviour, do not "fix" silently):
- manager: an invalid int env (RECENT_MEMORY_TURNS=abc) RAISES ValueError;
- screen _bounded_int: invalid env int falls through to the file value, then
  default, with min/max clamping;
- screen infer_timeout_sec: an invalid env float does NOT fall through to the
  file value -- it goes straight to the default (asymmetric with _bounded_int);
- secrets: a masked ("") OPENAI_API_KEY loads as "" (empty string, not None).
"""

from __future__ import annotations

import pytest

from agent_tools.function_tools.screen.config import ScreenPipelineConfig, load_screen_config
from agent_tools.function_tools.song.config import PROJECT_ROOT, load_song_config
from spica.config.env_roster import LEGACY_ENV_VARS, consumed_env_names
from spica.config.manager import ConfigManager
from spica.config.schema import AppConfig
from spica.config.secrets import load_secrets

EXPECTED_SCREEN_DEFAULTS = ScreenPipelineConfig(
    enabled=True,
    provider="moondream_local",
    model_id="vikhyatk/moondream2",
    revision="2025-06-21",
    device="cuda",
    dtype="bfloat16",
    max_side=768,
    reasoning=False,
    preload=False,
    ocr_enabled=True,
    ocr_engine="rapidocr",
    capture_format="png",
    infer_timeout_sec=30.0,
    log_timing=True,
    debug_save_images=False,
)


@pytest.fixture
def clean_env(monkeypatch):
    """Mask every consumed env name to "" (treated as unset by all loaders;
    survives load_dotenv(override=False) re-priming because the var exists)."""
    for name in sorted(consumed_env_names() | set(LEGACY_ENV_VARS)):
        monkeypatch.setenv(name, "")
    return monkeypatch


def _screen_file(tmp_path, payload: str):
    path = tmp_path / "screen.json"
    path.write_text(payload, encoding="utf-8")
    return path


# -- screen domain: env > file > default + every coercion branch ---------------


def test_screen_all_defaults_when_env_masked_and_no_file(clean_env, tmp_path):
    assert load_screen_config(tmp_path / "absent.json") == EXPECTED_SCREEN_DEFAULTS


def test_screen_file_overrides_defaults(clean_env, tmp_path):
    path = _screen_file(tmp_path, '{"device": "cpu", "max_side": 512, "revision": "r-file"}')
    config = load_screen_config(path)
    assert (config.device, config.max_side, config.revision) == ("cpu", 512, "r-file")
    assert config.provider == EXPECTED_SCREEN_DEFAULTS.provider  # untouched key -> default


def test_screen_env_wins_over_file_and_strips_whitespace(clean_env, tmp_path):
    path = _screen_file(tmp_path, '{"device": "cpu", "revision": "r-file", "max_side": 512}')
    clean_env.setenv("SPICA_SCREEN_DEVICE", "  cuda  ")
    clean_env.setenv("SPICA_SCREEN_REVISION", " r-env ")
    config = load_screen_config(path)
    assert (config.device, config.revision) == ("cuda", "r-env")
    assert config.max_side == 512  # env unset for this key -> file still wins


def test_screen_whitespace_only_env_falls_through_to_file(clean_env, tmp_path):
    path = _screen_file(tmp_path, '{"device": "cpu"}')
    clean_env.setenv("SPICA_SCREEN_DEVICE", "   ")
    assert load_screen_config(path).device == "cpu"


@pytest.mark.parametrize(
    "raw,expected",
    [("1", True), ("true", True), ("YES", True), ("on", True), ("y", True),
     ("0", False), ("off", False), ("garbage", False)],
)
def test_screen_env_bool_variants(clean_env, tmp_path, raw, expected):
    clean_env.setenv("SPICA_SCREEN_ENABLED", raw)
    assert load_screen_config(tmp_path / "absent.json").enabled is expected


def test_screen_empty_bool_env_falls_to_file_then_default(clean_env, tmp_path):
    path = _screen_file(tmp_path, '{"enabled": false}')
    assert load_screen_config(path).enabled is False  # "" -> file value
    assert load_screen_config(tmp_path / "absent.json").enabled is True  # "" -> default


@pytest.mark.parametrize(
    "env_value,file_payload,expected",
    [
        ("99999", None, 4096),            # clamp to maximum
        ("64", None, 128),                # clamp to minimum
        (" 512 ", None, 512),             # int() tolerates surrounding whitespace
        ("abc", '{"max_side": 1024}', 1024),  # invalid env -> file value
        ("abc", '{"max_side": "bogus"}', 768),  # invalid env + file -> default
        ("abc", None, 768),               # invalid env, no file -> default
    ],
)
def test_screen_bounded_int_semantics(clean_env, tmp_path, env_value, file_payload, expected):
    path = _screen_file(tmp_path, file_payload) if file_payload else tmp_path / "absent.json"
    clean_env.setenv("SPICA_SCREEN_MAX_SIDE", env_value)
    assert load_screen_config(path).max_side == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("FLOAT16", "float16"), ("bogus", "auto"), ("bfloat16", "bfloat16")],
)
def test_screen_dtype_normalization(clean_env, tmp_path, raw, expected):
    clean_env.setenv("SPICA_SCREEN_DTYPE", raw)
    assert load_screen_config(tmp_path / "absent.json").dtype == expected


def test_screen_dtype_file_value_passes_normalizer(clean_env, tmp_path):
    path = _screen_file(tmp_path, '{"dtype": "float32"}')
    assert load_screen_config(path).dtype == "float32"
    path = _screen_file(tmp_path, '{"dtype": "weird"}')
    assert load_screen_config(path).dtype == "auto"


@pytest.mark.parametrize("raw,expected", [("jpg", "png"), ("PNG", "png")])
def test_screen_capture_format_whitelist(clean_env, tmp_path, raw, expected):
    clean_env.setenv("SPICA_SCREEN_CAPTURE_FORMAT", raw)
    assert load_screen_config(tmp_path / "absent.json").capture_format == expected


@pytest.mark.parametrize(
    "env_value,file_payload,expected",
    [
        ("2.5", None, 2.5),
        ("-5", None, 30.0),               # non-positive -> default
        ("abc", None, 30.0),
        # PINNED ASYMMETRY: invalid env float skips the file value entirely
        # (truthy env string short-circuits `or raw.get(...)`), unlike _bounded_int.
        ("abc", '{"infer_timeout_sec": 60}', 30.0),
        ("", '{"infer_timeout_sec": 60}', 60.0),  # empty env DOES fall to file
    ],
)
def test_screen_timeout_positive_float_semantics(clean_env, tmp_path, env_value, file_payload, expected):
    path = _screen_file(tmp_path, file_payload) if file_payload else tmp_path / "absent.json"
    clean_env.setenv("SPICA_SCREEN_INFER_TIMEOUT_SEC", env_value)
    assert load_screen_config(path).infer_timeout_sec == expected


def test_screen_revision_empty_env_falls_to_file_then_default(clean_env, tmp_path):
    assert load_screen_config(_screen_file(tmp_path, '{"revision": "r-file"}')).revision == "r-file"
    assert load_screen_config(tmp_path / "absent.json").revision == "2025-06-21"


# -- app domain (manager): env > yaml > defaults --------------------------------


def test_manager_defaults_when_masked_and_no_file(clean_env, tmp_path):
    assert ConfigManager(tmp_path / "absent.yaml").load() == AppConfig()


def test_manager_env_wins_yaml_wins_defaults(clean_env, tmp_path):
    path = tmp_path / "app.yaml"
    path.write_text(
        "llm:\n  model: file-model\nmemory:\n  recent_memory_turns: 9\n", encoding="utf-8"
    )
    clean_env.setenv("MODEL", "env-model")
    clean_env.setenv("RECENT_CONTEXT_LIMIT", "4")
    config = ConfigManager(path).load()
    assert config.llm.model == "env-model"          # env > file
    assert config.memory.recent_memory_turns == 9   # file > default
    assert config.memory.recent_context_limit == 4  # env > default
    assert config.memory.long_term_memory_limit == 5  # default


def test_manager_empty_env_falls_through_to_file(clean_env, tmp_path):
    path = tmp_path / "app.yaml"
    path.write_text("llm:\n  model: file-model\n", encoding="utf-8")
    assert ConfigManager(path).load().llm.model == "file-model"


def test_manager_invalid_int_env_raises(clean_env, tmp_path):
    # Current semantics: int("abc") raises -- a loud failure, not a silent
    # default. Step 2a typed-ization must not start swallowing this.
    clean_env.setenv("RECENT_MEMORY_TURNS", "abc")
    with pytest.raises(ValueError):
        ConfigManager(tmp_path / "absent.yaml").load()


def test_manager_max_tool_rounds_env(clean_env, tmp_path):
    clean_env.setenv("MAX_TOOL_ROUNDS", "7")
    assert ConfigManager(tmp_path / "absent.yaml").load().max_tool_rounds == 7


# -- secrets ---------------------------------------------------------------------


def test_secrets_masked_key_loads_as_empty_string(clean_env):
    assert load_secrets().openai_api_key == ""


def test_secrets_reads_openai_api_key(clean_env):
    clean_env.setenv("OPENAI_API_KEY", "fake-key-for-equivalence-test")
    assert load_secrets().openai_api_key == "fake-key-for-equivalence-test"


# -- song domain: defaults + deep merge + path resolution (no env consumed) -----


def test_song_defaults_when_no_file(clean_env, tmp_path):
    config = load_song_config(tmp_path / "absent.json")
    assert config["search"] == {"limit": 20, "bitrate": 320000}
    assert config["download"]["timeout_sec"] == 60
    assert config["generated_root"] == str((PROJECT_ROOT / "static/generated_song").resolve())
    assert config["_config_path"] == str(tmp_path / "absent.json")


def test_song_deep_merge_keeps_sibling_defaults(clean_env, tmp_path):
    path = tmp_path / "song.json"
    path.write_text(
        '{"search": {"limit": 5}, "rvc": {"voices": {"spica": {"transpose": 2}}}}',
        encoding="utf-8",
    )
    config = load_song_config(path)
    assert config["search"] == {"limit": 5, "bitrate": 320000}  # sibling key survives
    voice = config["rvc"]["voices"]["spica"]
    assert voice["transpose"] == 2
    assert voice["f0_method"] == "rmvpe"  # untouched nested defaults survive
    assert voice["model_path"].startswith(str(PROJECT_ROOT))  # relative -> absolute


# -- roster meta-pin -------------------------------------------------------------


def test_roster_covers_every_env_name_in_the_config_layer():
    """Every UPPERCASE quoted name in the config layer's source must be in the
    roster (consumed/written/stripped/legacy) -- a new env knob cannot dodge
    the Layer A/B mask lists by being added quietly to manager/secrets."""
    import re
    from pathlib import Path

    from spica.config import env_roster

    known = (
        set(consumed_env_names())
        | set(env_roster.WRITTEN_ENV_VARS)
        | set(env_roster.STRIPPED_ENV_VARS)
        | set(LEGACY_ENV_VARS)
    )
    repo = Path(__file__).resolve().parents[1]
    pattern = re.compile(r'"([A-Z][A-Z0-9_]{2,})"')
    unknown: dict[str, list[str]] = {}
    for rel in ("spica/config/manager.py", "spica/config/secrets.py", "spica/config/runtime_env.py"):
        path = repo / rel
        if not path.is_file():  # runtime_env.py lands in P0b step 1
            continue
        hits = [name for name in pattern.findall(path.read_text(encoding="utf-8")) if name not in known]
        if hits:
            unknown[rel] = sorted(set(hits))
    assert unknown == {}, f"env names missing from spica/config/env_roster.py: {unknown}"


# -- platform domain (W1, WINDOWS_COMPAT_PLAN §3.4): fold pins -- ADDITIONS ONLY --
# Machine-independent by construction: fold_platform is pure and every pin below
# INJECTS the host platform value; the real sys.platform is never read here.


def test_fold_platform_auto_on_linux_host_folds_linux():
    from spica.host.agent_assembly import fold_platform

    assert fold_platform("auto", "linux") == "linux"


def test_fold_platform_auto_on_win32_host_folds_windows():
    from spica.host.agent_assembly import fold_platform

    assert fold_platform("auto", "win32") == "windows"


def test_fold_platform_explicit_value_ignores_host():
    from spica.host.agent_assembly import fold_platform

    assert fold_platform("windows", "linux") == "windows"
    assert fold_platform("linux", "win32") == "linux"


def test_fold_platform_auto_on_unknown_host_raises():
    # P2-2: auto + darwin/cygwin/... must FAIL LOUD, never silently fold onto
    # the wmctrl lane of a non-Linux host.
    from spica.host.agent_assembly import fold_platform

    with pytest.raises(ValueError):
        fold_platform("auto", "darwin")


def test_stt_mic_backend_default_auto_and_literal_fails_loud(clean_env, tmp_path):
    # A5 (W3): typed yaml-only key, no env name (roster untouched). Default "auto"
    # keeps load() == AppConfig() equivalence; illegal values die at the Literal.
    from pydantic import ValidationError

    from spica.config.schema import SttConfig

    assert AppConfig().stt.mic_backend == "auto"
    assert ConfigManager(tmp_path / "absent.yaml").load().stt.mic_backend == "auto"
    with pytest.raises(ValidationError):
        SttConfig(mic_backend="usb")


def test_resolve_mic_backend_auto_folds_by_platform():
    from spica.host.app_host import resolve_mic_backend

    assert resolve_mic_backend("auto", "linux") == "respeaker"
    assert resolve_mic_backend("auto", "windows") == "generic"


def test_resolve_mic_backend_explicit_value_ignores_platform():
    from spica.host.app_host import resolve_mic_backend

    assert resolve_mic_backend("respeaker", "windows") == "respeaker"
    assert resolve_mic_backend("generic", "linux") == "generic"


def test_resolve_mic_backend_unknown_values_raise():
    # Same fail-loud discipline as fold_platform: a mis-set backend must never
    # silently fold onto some mic path.
    from spica.host.app_host import resolve_mic_backend

    with pytest.raises(ValueError):
        resolve_mic_backend("usb", "linux")
    with pytest.raises(ValueError):
        resolve_mic_backend("auto", "darwin")


def test_platform_config_default_auto_and_literal_fails_loud(clean_env, tmp_path):
    # Schema layer: default stays "auto" (load() == AppConfig() equivalence keeps
    # holding with the new section); an illegal value dies at the Literal, so
    # fold_platform never sees it.
    from pydantic import ValidationError

    from spica.config.schema import PlatformConfig

    assert AppConfig().platform.os == "auto"
    assert ConfigManager(tmp_path / "absent.yaml").load().platform.os == "auto"
    with pytest.raises(ValidationError):
        PlatformConfig(os="macos")
