from __future__ import annotations

from spica.adapters.config_studio.platform import platform_capabilities_for
from spica.config.env_roster import APP_ENV_MAP, SCREEN_ENV_MAP
from spica.config.secrets import Secrets
from spica.config_studio.sensitive_status import (
    inspect_readonly_env_status,
    inspect_sensitive_env_status,
)


def _linux_platform(tmp_path):
    import os

    return platform_capabilities_for(
        os_family="posix",
        runtime_name="linux",
        user_id=os.getuid(),
        temp_directory=tmp_path / "platform-tmp",
    )


def test_sensitive_status_exposes_configuration_and_health_without_values(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env_path = repo / "xiaosan.env"
    env_path.write_bytes(
        b"OPENAI_API_KEY='repo-secret-canary'\n"
        b"DEEPSEEK_API_KEY='legacy-secret-canary'\n"
        b"export MODEL=synthetic-model\n"
    )
    env_path.chmod(0o664)

    status = inspect_sensitive_env_status(
        repo,
        Secrets(openai_api_key="effective-secret-canary"),
        platform_capabilities=_linux_platform(tmp_path),
    )
    wire = status.to_wire()
    managed_overrides = wire.pop("managed_overrides")

    assert wire == {
        "permission_health": "TOO_PERMISSIVE",
        "parse_health": "VALID",
        "secret_slots": {
            "openai_api_key": True,
            "judge_api_key": False,
            "bilibili_cookie": False,
            "qbittorrent_password": False,
        },
        "legacy_entries": ["DEEPSEEK_API_KEY"],
    }
    assert next(
        item
        for item in managed_overrides
        if item["environment_variable"] == "MODEL"
    )["repo_defined"] is True
    rendered = repr(status) + repr(wire)
    assert "repo-secret-canary" not in rendered
    assert "legacy-secret-canary" not in rendered
    assert "effective-secret-canary" not in rendered
    assert str(env_path) not in rendered


def test_sensitive_status_projects_the_owner_allowlist_without_override_values(
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    env_path = repo / "xiaosan.env"
    env_path.write_bytes(
        b"MODEL=synthetic-model-canary\n"
        b"export SPICA_SCREEN_ENABLED=false\n"
        b"RESPEAKER_TUNING_PATH=outside-scope-canary\n"
    )
    env_path.chmod(0o600)

    wire = inspect_sensitive_env_status(
        repo,
        Secrets(),
        platform_capabilities=_linux_platform(tmp_path),
    ).to_wire()
    rows = {
        row["environment_variable"]: row
        for row in wire["managed_overrides"]
    }

    assert set(rows) == set(APP_ENV_MAP.values()) | set(SCREEN_ENV_MAP.values())
    assert rows["MODEL"] == {
        "environment_variable": "MODEL",
        "affected_fields": ["llm.model"],
        "repo_defined": True,
    }
    assert rows["SPICA_SCREEN_ENABLED"] == {
        "environment_variable": "SPICA_SCREEN_ENABLED",
        "affected_fields": ["screen.enabled"],
        "repo_defined": True,
    }
    assert rows["OPENAI_BASE_URL"]["repo_defined"] is False
    assert "RESPEAKER_TUNING_PATH" not in rows
    assert "synthetic-model-canary" not in repr(wire)
    assert "outside-scope-canary" not in repr(wire)


def test_sensitive_status_fails_closed_for_invalid_or_unsafe_document(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.env"
    outside.write_bytes(b"OPENAI_API_KEY=outside-secret\n")
    env_path = repo / "xiaosan.env"
    env_path.symlink_to(outside)

    unsafe = inspect_sensitive_env_status(
        repo,
        Secrets(),
        platform_capabilities=_linux_platform(tmp_path),
    )

    assert unsafe.permission_health == "DOCUMENT_UNSAFE"
    assert unsafe.parse_health == "UNAVAILABLE"
    assert unsafe.legacy_entries == ()

    env_path.unlink()
    env_path.write_bytes(b"MODEL='unterminated\n")
    invalid = inspect_sensitive_env_status(
        repo,
        Secrets(),
        platform_capabilities=_linux_platform(tmp_path),
    )

    assert invalid.parse_health == "INVALID"


def test_sensitive_status_reports_missing_without_creating_state(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    status = inspect_sensitive_env_status(
        repo,
        Secrets(),
        platform_capabilities=_linux_platform(tmp_path),
    )

    assert status.permission_health == "MISSING"
    assert status.parse_health == "MISSING"
    assert list(repo.iterdir()) == []


def test_sensitive_status_uses_injected_platform_permission_capability(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "xiaosan.env").write_bytes(b"MODEL=synthetic\n")

    status = inspect_sensitive_env_status(
        repo,
        Secrets(),
        platform_capabilities=platform_capabilities_for(
            os_family="nt",
            runtime_name="win32",
            user_id=None,
            temp_directory=tmp_path,
        ),
    )

    assert status.permission_health == "DACL_UNVERIFIED"


def test_parent_env_status_is_read_only_and_never_exposes_slots_or_values(tmp_path):
    parent = tmp_path / "xiaosan.env"
    parent.write_text(
        "OPENAI_API_KEY=parent-secret-canary\nDEEPSEEK_API_KEY=legacy\n",
        encoding="utf-8",
    )
    parent.chmod(0o600)

    wire = inspect_readonly_env_status(
        parent,
        platform_capabilities=_linux_platform(tmp_path),
    ).to_wire()

    assert wire == {
        "permission_health": "PRIVATE",
        "parse_health": "VALID",
        "legacy_entries": ["DEEPSEEK_API_KEY"],
    }
    assert "secret_slots" not in wire
    assert "parent-secret-canary" not in repr(wire)
