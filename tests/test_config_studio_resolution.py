from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

import pytest

import spica.config.secrets as secrets_owner
from spica.config.env_roster import LEGACY_ENV_VARS, consumed_env_names
from spica.config.environment_snapshot import EnvironmentSnapshot, EnvironmentValue
from spica.config.manager import ConfigManager
from spica.config.secrets import (
    EnvironmentRefreshError,
    LoadedSecrets,
    Secrets,
    load_secrets,
)


@pytest.fixture(autouse=True)
def clear_real_config_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in consumed_env_names() | frozenset(LEGACY_ENV_VARS):
        monkeypatch.delenv(name, raising=False)


def test_resolve_snapshot_uses_explicit_environment_without_global_state(monkeypatch):
    monkeypatch.setenv("MODEL", "process-global-model")
    snapshot = EnvironmentSnapshot.from_mapping(
        {"MODEL": "snapshot-model"},
        layer="inherited",
    )

    resolution = ConfigManager().resolve_snapshot(
        {"llm": {"model": "file-model"}},
        snapshot,
    )

    model = resolution.leaf(("llm", "model"))
    assert resolution.to_app_config().llm.model == "snapshot-model"
    assert model.next_launch_value == "snapshot-model"
    assert model.source.kind == "env_override"
    assert model.source.environment_variable == "MODEL"
    assert model.source.environment_layer == "inherited"


def test_environment_snapshot_rejects_secrets_and_cannot_leak_through_repr_or_json():
    with pytest.raises(ValueError, match="secret environment variable"):
        EnvironmentSnapshot.from_mapping(
            {"OPENAI_API_KEY": "do-not-store-this"},
            layer="repo_dotenv",
        )

    canary = "profile-canary-86f1"
    snapshot = EnvironmentSnapshot.from_mapping(
        {"SPICA_CHARACTER_PROFILE": canary},
        layer="repo_dotenv",
    )

    assert canary not in repr(snapshot)
    with pytest.raises(TypeError):
        json.dumps(snapshot)


@pytest.mark.parametrize(
    "values",
    [
        {"OPENAI_API_KEY": EnvironmentValue("secret", "inherited")},
        {"UNREVIEWED_NAME": EnvironmentValue("value", "inherited")},
        {"MODEL": "raw-string-bypass"},
        {"MODEL": EnvironmentValue("value", "")},
        {"MODEL": EnvironmentValue(7, "inherited")},
    ],
)
def test_environment_snapshot_constructor_cannot_bypass_allowlist_or_types(values):
    with pytest.raises((TypeError, ValueError)):
        EnvironmentSnapshot(values)  # type: ignore[arg-type]


def test_environment_snapshot_from_mapping_rejects_instead_of_string_coercing():
    with pytest.raises(TypeError):
        EnvironmentSnapshot.from_mapping(
            {"MODEL": object()},  # type: ignore[dict-item]
            layer="inherited",
        )


def test_resolution_returns_defensive_configs_and_safe_leaf_representations():
    canary = "private-profile-canary-a13b"
    resolution = ConfigManager().resolve_snapshot(
        {"character": {"profile_override": canary}},
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )

    first = resolution.to_app_config()
    first.character.profile_override = "mutated-by-caller"

    assert resolution.to_app_config().character.profile_override == canary
    assert canary not in repr(resolution)
    assert canary not in repr(resolution.leaf(("character", "profile_override")))
    with pytest.raises(TypeError):
        json.dumps(resolution)


@pytest.mark.parametrize(
    ("field", "raw_value", "expected_value"),
    [
        ("provider", "", "moondream_local"),
        ("max_side", "not-an-integer", 768),
        ("infer_timeout_sec", "not-a-number", 30.0),
    ],
)
def test_resolution_provenance_reports_owner_fallback_as_default(
    field,
    raw_value,
    expected_value,
):
    resolution = ConfigManager().resolve_snapshot(
        {"screen": {field: raw_value}},
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )

    leaf = resolution.leaf(("screen", field))

    assert leaf.next_launch_value == expected_value
    assert leaf.source.kind == "default"


def test_environment_snapshot_preserves_production_layer_precedence_per_variable():
    snapshot = EnvironmentSnapshot.from_layers(
        inherited={"MODEL": "inherited-model"},
        repo_dotenv={"MODEL": "repo-model"},
        parent_dotenv={
            "MODEL": "parent-model",
            "JUDGE_MODEL": "parent-judge-model",
        },
    )

    assert snapshot.get("MODEL") == "inherited-model"
    assert snapshot.layer_for("MODEL") == "inherited"
    assert snapshot.get("JUDGE_MODEL") == "parent-judge-model"
    assert snapshot.layer_for("JUDGE_MODEL") == "parent_dotenv"


def test_secrets_are_immutable_opaque_values_not_generic_serializable_records():
    canary = "secret-canary-19d7"
    secrets = Secrets(openai_api_key=canary)

    assert secrets.openai_api_key == canary
    assert canary not in repr(secrets)
    with pytest.raises(AttributeError):
        secrets.openai_api_key = "replacement"
    with pytest.raises(TypeError):
        json.dumps(secrets)
    with pytest.raises(TypeError):
        asdict(secrets)


def test_named_secret_load_keeps_secret_values_out_of_environment_snapshot(tmp_path):
    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={
            "MODEL": "explicit-inherited-model",
            "OPENAI_API_KEY": "synthetic-openai-canary",
        },
        repo_env_path=tmp_path / "repo" / "xiaosan.env",
        parent_env_path=tmp_path / "parent" / "xiaosan.env",
        prime_process=False,
    )

    assert loaded.secrets.openai_api_key == "synthetic-openai-canary"
    assert loaded.environment_snapshot.get("MODEL") == "explicit-inherited-model"
    assert loaded.environment_snapshot.layer_for("MODEL") == "inherited"
    assert loaded.environment_snapshot.get("OPENAI_API_KEY") is None


def test_named_secret_load_resolves_explicit_repo_and_parent_layers_without_globals(
    tmp_path,
    monkeypatch,
):
    repo_env = tmp_path / "repo" / "xiaosan.env"
    parent_env = tmp_path / "parent" / "xiaosan.env"
    repo_env.parent.mkdir()
    parent_env.parent.mkdir()
    repo_env.write_text(
        "MODEL=repo-model\nOPENAI_API_KEY=repo-secret\n",
        encoding="utf-8",
    )
    parent_env.write_text("JUDGE_MODEL=parent-judge\n", encoding="utf-8")
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.delenv("JUDGE_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={},
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )

    assert loaded.environment_snapshot.get("MODEL") == "repo-model"
    assert loaded.environment_snapshot.layer_for("MODEL") == "repo_dotenv"
    assert loaded.environment_snapshot.get("JUDGE_MODEL") == "parent-judge"
    assert loaded.environment_snapshot.layer_for("JUDGE_MODEL") == "parent_dotenv"
    assert loaded.secrets.openai_api_key == "repo-secret"
    assert "MODEL" not in __import__("os").environ
    assert "OPENAI_API_KEY" not in __import__("os").environ


def test_loaded_owner_refreshes_dotenv_without_using_primed_process_values(
    tmp_path,
    monkeypatch,
):
    repo_env = tmp_path / "repo" / "xiaosan.env"
    parent_env = tmp_path / "parent" / "xiaosan.env"
    repo_env.parent.mkdir()
    parent_env.parent.mkdir()
    repo_env.write_text(
        "MODEL=first-repo-model\nOPENAI_API_KEY=first-repo-secret\n",
        encoding="utf-8",
    )
    parent_env.write_text("JUDGE_MODEL=parent-judge\n", encoding="utf-8")
    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={"REASONING_EFFORT": "inherited-effort"},
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )
    repo_env.write_text(
        "MODEL=second-repo-model\nOPENAI_API_KEY=second-repo-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODEL", "stale-primed-model")
    monkeypatch.setenv("OPENAI_API_KEY", "stale-primed-secret")

    refreshed = loaded.refresh()

    assert refreshed.environment_snapshot.get("MODEL") == "second-repo-model"
    assert refreshed.environment_snapshot.layer_for("MODEL") == "repo_dotenv"
    assert refreshed.environment_snapshot.get("JUDGE_MODEL") == "parent-judge"
    assert refreshed.environment_snapshot.get("REASONING_EFFORT") == "inherited-effort"
    assert refreshed.environment_snapshot.layer_for("REASONING_EFFORT") == "inherited"
    assert refreshed.secrets.openai_api_key == "second-repo-secret"
    rendered = repr(loaded) + repr(refreshed)
    assert "first-repo-secret" not in rendered
    assert "second-repo-secret" not in rendered
    assert str(repo_env) not in rendered


def test_refresh_and_candidate_resolution_preserve_full_inherited_interpolation_base(
    tmp_path,
    monkeypatch,
):
    repo_env = tmp_path / "repo" / "xiaosan.env"
    parent_env = tmp_path / "parent" / "xiaosan.env"
    repo_env.parent.mkdir()
    parent_env.parent.mkdir()
    repo_env.write_text("MODEL=${CUSTOM_MODEL_NAME}\n", encoding="utf-8")
    parent_env.write_text("", encoding="utf-8")
    interpolation_canary = "synthetic-interpolation-model"
    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={"CUSTOM_MODEL_NAME": interpolation_canary},
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )
    monkeypatch.setenv("CUSTOM_MODEL_NAME", "stale-process-value")

    candidate = loaded.resolve_repo_dotenv(repo_env.read_bytes())
    refreshed = loaded.refresh()

    assert loaded.environment_snapshot.get("MODEL") == interpolation_canary
    assert candidate.environment_snapshot.get("MODEL") == interpolation_canary
    assert refreshed.environment_snapshot.get("MODEL") == interpolation_canary
    assert refreshed.environment_snapshot.layer_for("MODEL") == "repo_dotenv"
    assert loaded.environment_snapshot.get("CUSTOM_MODEL_NAME") is None
    assert refreshed.environment_snapshot.get("CUSTOM_MODEL_NAME") is None
    interpolation_base = loaded._inherited_interpolation_base
    rendered = repr(loaded) + repr(refreshed) + repr(interpolation_base)
    assert interpolation_canary not in rendered
    assert "stale-process-value" not in rendered
    with pytest.raises(TypeError):
        json.dumps(interpolation_base)
    with pytest.raises(AttributeError):
        interpolation_base._items = ()


def test_refresh_capable_loaded_owner_requires_an_explicit_interpolation_base(
    tmp_path,
):
    with pytest.raises(TypeError, match="interpolation base"):
        LoadedSecrets(
            secrets=Secrets(),
            environment_snapshot=EnvironmentSnapshot.from_mapping(
                {}, layer="synthetic"
            ),
            refresh_repo_env_path=tmp_path / "repo.env",
            refresh_parent_env_path=tmp_path / "parent.env",
        )


def test_refresh_keeps_previously_tainted_repo_override_quarantined(
    tmp_path,
):
    old_secret = "synthetic-old-secret-canary"
    repo_env = tmp_path / "repo" / "xiaosan.env"
    parent_env = tmp_path / "parent" / "xiaosan.env"
    repo_env.parent.mkdir()
    parent_env.parent.mkdir()
    repo_env.write_text(
        "OPENAI_API_KEY=synthetic-old-secret-canary\n"
        "MODEL=${CUSTOM_SECRET_ALIAS}\n",
        encoding="utf-8",
    )
    parent_env.write_text("", encoding="utf-8")
    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={"CUSTOM_SECRET_ALIAS": old_secret},
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )
    assert loaded.environment_snapshot.is_tainted("MODEL") is True
    assert loaded.environment_snapshot.layer_for("MODEL") == "repo_dotenv"
    repo_env.write_text(
        "OPENAI_API_KEY=synthetic-new-secret-canary\n"
        "MODEL=${CUSTOM_SECRET_ALIAS}\n",
        encoding="utf-8",
    )

    refreshed = loaded.refresh()

    assert refreshed.environment_snapshot.get("MODEL") is None
    assert refreshed.environment_snapshot.is_tainted("MODEL") is True
    assert refreshed.environment_snapshot.layer_for("MODEL") == "repo_dotenv"
    assert old_secret not in repr(refreshed)


def test_refresh_preserves_an_inherited_secret_tainted_override_over_lower_layers(
    tmp_path,
):
    secret = "inherited-secret-canary"
    repo_env = tmp_path / "repo" / "xiaosan.env"
    parent_env = tmp_path / "parent" / "xiaosan.env"
    repo_env.parent.mkdir()
    parent_env.parent.mkdir()
    repo_env.write_text("MODEL=repo-safe-model\n", encoding="utf-8")
    parent_env.write_text("MODEL=parent-safe-model\n", encoding="utf-8")
    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={
            "OPENAI_API_KEY": secret,
            "MODEL": f"prefix-{secret}",
        },
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )

    refreshed = loaded.refresh()

    assert refreshed.environment_snapshot.get("MODEL") is None
    assert refreshed.environment_snapshot.is_tainted("MODEL") is True
    assert refreshed.environment_snapshot.layer_for("MODEL") == "inherited"
    assert refreshed.tainted_environment_names == ("MODEL",)
    assert secret not in repr(refreshed)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink unavailable")
def test_loaded_owner_refresh_rejects_a_swapped_dotenv_symlink(tmp_path: Path):
    repo_env = tmp_path / "repo.env"
    parent_env = tmp_path / "parent.env"
    outside = tmp_path / "outside.env"
    repo_env.write_text("MODEL=initial\n", encoding="utf-8")
    parent_env.write_text("", encoding="utf-8")
    outside.write_text("OPENAI_API_KEY=outside-secret-canary\n", encoding="utf-8")
    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={},
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )
    repo_env.unlink()
    repo_env.symlink_to(outside)

    with pytest.raises(EnvironmentRefreshError) as raised:
        loaded.refresh()

    assert str(raised.value) == "environment owner document unsafe"
    assert "outside-secret-canary" not in repr(raised.value)


@pytest.mark.skipif(os.name != "posix", reason="POSIX hardlink contract")
def test_loaded_owner_refresh_rejects_a_swapped_dotenv_hardlink(tmp_path: Path):
    repo_env = tmp_path / "repo.env"
    parent_env = tmp_path / "parent.env"
    outside = tmp_path / "outside.env"
    repo_env.write_text("MODEL=initial\n", encoding="utf-8")
    parent_env.write_text("", encoding="utf-8")
    outside.write_text("OPENAI_API_KEY=outside-secret-canary\n", encoding="utf-8")
    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={},
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )
    repo_env.unlink()
    os.link(outside, repo_env)

    with pytest.raises(EnvironmentRefreshError) as raised:
        loaded.refresh()

    assert str(raised.value) == "environment owner document unsafe"
    assert "outside-secret-canary" not in repr(raised.value)


@pytest.mark.skipif(not hasattr(os, "link"), reason="hardlink unavailable")
def test_loaded_owner_rejects_hardlinks_even_outside_the_posix_uid_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo_env = tmp_path / "repo.env"
    parent_env = tmp_path / "parent.env"
    outside = tmp_path / "outside.env"
    repo_env.write_text("MODEL=initial\n", encoding="utf-8")
    parent_env.write_text("", encoding="utf-8")
    outside.write_text("OPENAI_API_KEY=outside-secret-canary\n", encoding="utf-8")
    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={},
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )
    repo_env.unlink()
    os.link(outside, repo_env)

    class _NonPosixOsProxy:
        name = "nt"

        def __getattr__(self, name: str):
            return getattr(os, name)

    monkeypatch.setattr(secrets_owner, "os", _NonPosixOsProxy())

    with pytest.raises(EnvironmentRefreshError) as raised:
        loaded.refresh()

    assert str(raised.value) == "environment owner document unsafe"
    assert "outside-secret-canary" not in repr(raised.value)


def test_loaded_owner_resolves_a_repo_candidate_with_parent_fallbacks(
    tmp_path,
):
    repo_secret = "repo-secret-canary"
    parent_secret = "parent-secret-canary"
    repo_env = tmp_path / "repo" / "xiaosan.env"
    parent_env = tmp_path / "parent" / "xiaosan.env"
    repo_env.parent.mkdir()
    parent_env.parent.mkdir()
    repo_env.write_text(
        f"OPENAI_API_KEY={repo_secret}\nMODEL=repo-model\n",
        encoding="utf-8",
    )
    parent_env.write_text(
        f"OPENAI_API_KEY={parent_secret}\nMODEL=parent-model\n",
        encoding="utf-8",
    )
    owner = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={},
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )

    transition = owner.resolve_repo_transition(repo_env.read_bytes(), b"")
    before = transition.before
    after = transition.after

    assert before.environment_snapshot.get("MODEL") == "repo-model"
    assert after.environment_snapshot.get("MODEL") == "parent-model"
    assert after.secrets.openai_api_key == parent_secret
    assert after.secret_source("openai_api_key") == "parent_dotenv"
    assert after.secret_configured("openai_api_key") is True
    assert transition.repo_change("OPENAI_API_KEY") == "will_clear"
    rendered = repr(before) + repr(after) + repr(transition)
    assert repo_secret not in rendered
    assert parent_secret not in rendered
    with pytest.raises(TypeError):
        json.dumps(after)


def test_resolved_repo_environment_never_retains_secret_plaintext_outside_secrets(
    tmp_path,
):
    secret = "repo-plaintext-canary"
    repo_env = tmp_path / "repo" / "xiaosan.env"
    parent_env = tmp_path / "parent" / "xiaosan.env"
    repo_env.parent.mkdir()
    parent_env.parent.mkdir()
    repo_env.write_text(f"OPENAI_API_KEY={secret}\n", encoding="utf-8")
    parent_env.write_text("", encoding="utf-8")
    owner = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={},
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )

    resolved = owner.resolve_repo_dotenv(repo_env.read_bytes())
    transition = owner.resolve_repo_transition(repo_env.read_bytes(), b"")

    assert not hasattr(resolved, "_repo_values")
    assert type(resolved).__slots__ == ("_loaded", "_repo_names")
    assert resolved.repo_contains("OPENAI_API_KEY") is True
    assert secret not in repr(resolved)
    assert secret not in repr(resolved._repo_names)
    assert resolved._loaded.secrets.openai_api_key == secret
    for slot in resolved._loaded.__slots__:
        if slot != "_secrets":
            assert secret not in repr(getattr(resolved._loaded, slot))
    assert type(transition).__slots__ == (
        "_after",
        "_before",
        "_changed_names",
    )
    assert secret not in repr(transition)
    assert secret not in repr(transition._changed_names)
    with pytest.raises(TypeError):
        json.dumps(resolved)
    with pytest.raises(TypeError):
        json.dumps(transition)


def test_named_secret_load_quarantines_nonsecret_overrides_tainted_by_secret_interpolation(
    tmp_path,
):
    secret = "synthetic-secret-canary"
    repo_env = tmp_path / "repo" / "xiaosan.env"
    parent_env = tmp_path / "parent" / "xiaosan.env"
    repo_env.parent.mkdir()
    parent_env.parent.mkdir()
    repo_env.write_text(
        f"OPENAI_API_KEY={secret}\nMODEL=${{OPENAI_API_KEY}}\n",
        encoding="utf-8",
    )
    parent_env.write_text("MODEL=safe-lower-layer\n", encoding="utf-8")

    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={},
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )

    assert loaded.secrets.openai_api_key == secret
    assert loaded.environment_snapshot.get("MODEL") is None
    assert loaded.environment_snapshot.is_tainted("MODEL") is True
    assert loaded.environment_snapshot.layer_for("MODEL") == "repo_dotenv"
    assert loaded.tainted_environment_names == ("MODEL",)
    assert secret not in repr(loaded)


def test_retired_legacy_api_key_is_still_a_taint_canary_not_a_config_owner(
    tmp_path,
):
    secret = "legacy-super-secret"
    repo_env = tmp_path / "repo" / "xiaosan.env"
    parent_env = tmp_path / "parent" / "xiaosan.env"
    repo_env.parent.mkdir()
    parent_env.parent.mkdir()
    repo_env.write_text(
        f"DEEPSEEK_API_KEY={secret}\nMODEL=${{DEEPSEEK_API_KEY}}\n",
        encoding="utf-8",
    )
    parent_env.write_text("", encoding="utf-8")

    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={},
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )

    assert loaded.environment_snapshot.get("MODEL") is None
    assert loaded.environment_snapshot.is_tainted("MODEL") is True
    assert loaded.tainted_environment_names == ("MODEL",)
    assert loaded.secrets.openai_api_key is None
    assert secret not in repr(loaded)


def test_loaded_owner_safely_retains_every_secret_value_seen_across_layers(
    tmp_path: Path,
):
    repo_env = tmp_path / "repo" / "xiaosan.env"
    parent_env = tmp_path / "parent" / "xiaosan.env"
    repo_env.parent.mkdir()
    parent_env.parent.mkdir()
    inherited_openai = "inherited-openai-canary"
    repo_openai = "shadowed-repo-openai-canary"
    parent_openai = "shadowed-parent-openai-canary"
    judge_first = "duplicate-judge-first-canary"
    judge_interpolated = "duplicate-judge-interpolated-canary"
    cookie = "repo-cookie-canary"
    qbit = "repo-qbit-canary"
    legacy_first = "duplicate-legacy-first-canary"
    legacy_interpolated = "duplicate-legacy-interpolated-canary"
    parent_judge = "shadowed-parent-judge-canary"
    repo_env.write_text(
        f"OPENAI_API_KEY={repo_openai}\n"
        f"JUDGE_API_KEY={judge_first}\n"
        "JUDGE_API_KEY=${JUDGE_SECRET_SEED}\n"
        f"BILIBILI_COOKIE={cookie}\n"
        f"QBITTORRENT_PASSWORD={qbit}\n"
        f"DEEPSEEK_API_KEY={legacy_first}\n"
        "DEEPSEEK_API_KEY=${LEGACY_SECRET_SEED}\n",
        encoding="utf-8",
    )
    parent_env.write_text(
        f"OPENAI_API_KEY={parent_openai}\n"
        f"JUDGE_API_KEY={parent_judge}\n",
        encoding="utf-8",
    )

    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={
            "OPENAI_API_KEY": inherited_openai,
            "JUDGE_SECRET_SEED": judge_interpolated,
            "LEGACY_SECRET_SEED": legacy_interpolated,
        },
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )

    observed = (
        inherited_openai,
        repo_openai,
        parent_openai,
        judge_first,
        judge_interpolated,
        parent_judge,
        cookie,
        qbit,
        legacy_first,
        legacy_interpolated,
    )
    assert loaded.secrets.openai_api_key == inherited_openai
    assert loaded.secrets.judge_api_key == judge_interpolated
    for canary in observed:
        assert loaded.contains_secret_material(f"prefix::{canary}::suffix") is True
    sanitized = loaded.sanitize_secret_material(" | ".join(observed))
    assert all(canary not in sanitized for canary in observed)
    assert "«REDACTED:OPENAI_API_KEY»" in sanitized
    assert "«REDACTED:DEEPSEEK_API_KEY»" in sanitized
    assert not hasattr(loaded, "secret_material_values")
    assert all(canary not in repr(loaded) for canary in observed)
    with pytest.raises(TypeError):
        json.dumps(loaded)


def test_loaded_owner_refresh_rebuilds_all_secret_material_from_current_layers(
    tmp_path: Path,
):
    repo_env = tmp_path / "repo" / "xiaosan.env"
    parent_env = tmp_path / "parent" / "xiaosan.env"
    repo_env.parent.mkdir()
    parent_env.parent.mkdir()
    inherited = "refresh-inherited-secret-canary"
    old_first = "refresh-old-first-canary"
    old_second = "refresh-old-second-canary"
    current_first = "refresh-current-first-canary"
    current_second = "refresh-current-second-canary"
    parent = "refresh-parent-secret-canary"
    repo_env.write_text(
        f"OPENAI_API_KEY={old_first}\nOPENAI_API_KEY={old_second}\n",
        encoding="utf-8",
    )
    parent_env.write_text(f"OPENAI_API_KEY={parent}\n", encoding="utf-8")
    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={"JUDGE_API_KEY": inherited},
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )
    repo_env.write_text(
        f"OPENAI_API_KEY={current_first}\n"
        f"OPENAI_API_KEY={current_second}\n",
        encoding="utf-8",
    )

    refreshed = loaded.refresh()

    for canary in (inherited, current_first, current_second, parent):
        assert refreshed.contains_secret_material(canary) is True
    assert refreshed.contains_secret_material(old_first) is False
    assert refreshed.contains_secret_material(old_second) is False
    sanitized = refreshed.sanitize_secret_material(
        f"{inherited}|{current_first}|{current_second}|{parent}"
    )
    assert all(
        canary not in sanitized
        for canary in (inherited, current_first, current_second, parent)
    )
    assert all(
        canary not in repr(refreshed)
        for canary in (inherited, current_first, current_second, parent)
    )
    with pytest.raises(TypeError):
        json.dumps(refreshed)


def test_loaded_owner_compares_complete_shadowed_and_duplicate_secret_material(
    tmp_path: Path,
):
    repo_env = tmp_path / "repo" / "xiaosan.env"
    parent_env = tmp_path / "parent" / "xiaosan.env"
    repo_env.parent.mkdir()
    parent_env.parent.mkdir()
    first_shadowed = "same-winner-first-shadowed-canary"
    second_shadowed = "same-winner-second-shadowed-canary"
    winner = "same-winner-secret-canary"
    repo_env.write_text(
        f"OPENAI_API_KEY={first_shadowed}\nOPENAI_API_KEY={winner}\n",
        encoding="utf-8",
    )
    parent_env.write_text("", encoding="utf-8")
    original = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={},
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )
    identical = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={},
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )
    repo_env.write_text(
        f"OPENAI_API_KEY={second_shadowed}\nOPENAI_API_KEY={winner}\n",
        encoding="utf-8",
    )
    changed_shadowed = original.refresh()

    assert original.secrets.openai_api_key == winner
    assert changed_shadowed.secrets.openai_api_key == winner
    assert original.same_secret_material(identical) is True
    assert identical.same_secret_material(original) is True
    assert original.same_secret_material(changed_shadowed) is False
    assert changed_shadowed.same_secret_material(original) is False
    rendered = repr(original) + repr(identical) + repr(changed_shadowed)
    assert first_shadowed not in rendered
    assert second_shadowed not in rendered
    assert winner not in rendered


def test_secret_material_comparison_includes_parse_completeness(tmp_path: Path):
    repo_env = tmp_path / "repo" / "xiaosan.env"
    parent_env = tmp_path / "parent" / "xiaosan.env"
    repo_env.parent.mkdir()
    parent_env.parent.mkdir()
    repo_env.write_text("MODEL=safe-model\n", encoding="utf-8")
    parent_env.write_text("", encoding="utf-8")
    complete = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={},
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )
    repo_env.write_text('OPENAI_API_KEY="unterminated\n', encoding="utf-8")
    incomplete = complete.refresh()

    assert complete.same_secret_material(incomplete) is False
    assert incomplete.same_secret_material(complete) is False
    with pytest.raises(TypeError):
        complete.same_secret_material(Secrets())  # type: ignore[arg-type]


def test_repo_transition_safely_retains_material_from_each_candidate(
    tmp_path: Path,
):
    repo_env = tmp_path / "repo" / "xiaosan.env"
    parent_env = tmp_path / "parent" / "xiaosan.env"
    repo_env.parent.mkdir()
    parent_env.parent.mkdir()
    parent_secret = "transition-parent-secret-canary"
    before_first = "transition-before-first-canary"
    before_second = "transition-before-second-canary"
    after_first = "transition-after-first-canary"
    after_legacy = "transition-after-legacy-canary"
    repo_env.write_text("", encoding="utf-8")
    parent_env.write_text(
        f"QBITTORRENT_PASSWORD={parent_secret}\n",
        encoding="utf-8",
    )
    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={},
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )

    transition = loaded.resolve_repo_transition(
        (
            f"BILIBILI_COOKIE={before_first}\n"
            f"BILIBILI_COOKIE={before_second}\n"
        ).encode(),
        (
            f"BILIBILI_COOKIE={after_first}\n"
            f"DEEPSEEK_API_KEY={after_legacy}\n"
        ).encode(),
    )

    assert transition.before.contains_secret_material(before_first) is True
    assert transition.before.contains_secret_material(before_second) is True
    assert transition.after.contains_secret_material(after_first) is True
    assert transition.after.contains_secret_material(after_legacy) is True
    assert transition.before.contains_secret_material(parent_secret) is True
    assert transition.after.contains_secret_material(parent_secret) is True
    assert transition.before.same_secret_material(transition.before) is True
    assert transition.before.same_secret_material(transition.after) is False
    assert transition.contains_secret_material(before_first) is True
    assert transition.contains_secret_material(after_first) is True
    sanitized = transition.sanitize_secret_material(
        f"{before_first}|{before_second}|{after_first}|{after_legacy}|{parent_secret}"
    )
    assert all(
        canary not in sanitized
        for canary in (
            before_first,
            before_second,
            after_first,
            after_legacy,
            parent_secret,
        )
    )
    rendered = repr(transition.before) + repr(transition.after) + repr(transition)
    assert all(
        canary not in rendered
        for canary in (
            before_first,
            before_second,
            after_first,
            after_legacy,
            parent_secret,
        )
    )
    with pytest.raises(TypeError):
        json.dumps(transition.before)
    with pytest.raises(TypeError):
        json.dumps(transition.after)
    with pytest.raises(TypeError):
        json.dumps(transition)


def test_secret_material_sanitizer_covers_serialized_and_escaped_variants(
    tmp_path: Path,
):
    secret = "multiline-secret\nUnicode-路径\\segment"
    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={"OPENAI_API_KEY": secret},
        repo_env_path=tmp_path / "repo" / "xiaosan.env",
        parent_env_path=tmp_path / "parent" / "xiaosan.env",
        prime_process=False,
    )
    variants = {
        secret,
        secret.encode("unicode_escape").decode("ascii"),
        json.dumps(secret, ensure_ascii=True)[1:-1],
        json.dumps(secret, ensure_ascii=False)[1:-1],
        repr(secret)[1:-1],
    }

    for variant in variants:
        assert loaded.contains_secret_material(f"prefix::{variant}::suffix") is True
        assert variant not in loaded.sanitize_secret_material(
            f"prefix::{variant}::suffix"
        )


def test_malformed_dotenv_secret_binding_forces_conservative_sanitization(
    tmp_path: Path,
):
    repo_env = tmp_path / "repo" / "xiaosan.env"
    parent_env = tmp_path / "parent" / "xiaosan.env"
    repo_env.parent.mkdir()
    parent_env.parent.mkdir()
    repo_env.write_text(
        'OPENAI_API_KEY="unterminated-secret\nMODEL=otherwise-visible\n',
        encoding="utf-8",
    )
    parent_env.write_text("", encoding="utf-8")

    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={},
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )

    assert loaded.contains_secret_material("ordinary catalog value") is True
    assert loaded.sanitize_secret_material("ordinary catalog value") == (
        "«REDACTED:UNVERIFIED_SECRET_MATERIAL»"
    )
    assert loaded.environment_snapshot.get("MODEL") is None
    assert loaded.environment_snapshot.is_tainted("MODEL") is True
    assert "unterminated-secret" not in repr(loaded)
    with pytest.raises(TypeError):
        json.dumps(loaded)
