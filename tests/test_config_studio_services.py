from __future__ import annotations

import os
from pathlib import Path

import pytest

from spica.adapters.config_studio.platform import platform_capabilities_for
from spica.config.environment_snapshot import EnvironmentSnapshot
from spica.config.secrets import LoadedSecrets, Secrets, load_secrets
from spica.config_studio.sensitive_env import (
    ClearMappedOverride,
    SensitiveEnvDocument,
)
from spica.config_studio.services import (
    ConfigStudioServiceError,
    OwnerBackedConfigStudioServices,
    ReadOnlyConfigStudioServices,
)


def test_read_only_sensitive_status_reports_unsafe_hardlink_without_refreshing(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_bytes(b"{}\n")
    source = tmp_path / "synthetic-sensitive-source"
    source.write_bytes(b"OPENAI_API_KEY=must-not-be-read\n")
    source.chmod(0o600)
    os.link(source, repo_root / "xiaosan.env")
    platform = platform_capabilities_for(
        os_family="posix",
        runtime_name="linux",
        user_id=source.stat().st_uid,
        temp_directory=tmp_path / "platform-tmp",
    )

    def unavailable_owner() -> LoadedSecrets:
        raise RuntimeError("synthetic owner must not be called")

    services = ReadOnlyConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {}, layer="synthetic"
        ),
        background_health_code=None,
        platform_capabilities=platform,
        secrets=Secrets(),
        environment_owner=unavailable_owner,
    )

    status = services.sensitive_status(session_id="synthetic-session")

    assert status["permission_health"] == "MULTIPLE_LINKS"
    assert status["secret_slots"] == [
        {"slot": "openai_api_key", "configured": False},
        {"slot": "judge_api_key", "configured": False},
        {"slot": "bilibili_cookie", "configured": False},
        {"slot": "qbittorrent_password", "configured": False},
    ]


def test_abandoned_sensitive_previews_expire_without_saturating_service(
    tmp_path: Path,
) -> None:
    now = [100.0]
    preview_tokens = iter(
        f"synthetic-sensitive-preview-{index:03d}" for index in range(65)
    )
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_bytes(b"max_tool_rounds: 2\n")
    env_path = repo_root / "xiaosan.env"
    env_path.write_bytes(b"MODEL=synthetic-model\n")
    env_path.chmod(0o600)
    parent_env_path = tmp_path / "sandbox-parent" / "xiaosan.env"
    parent_env_path.parent.mkdir()
    parent_env_path.write_bytes(b"")
    parent_env_path.chmod(0o600)
    environment_owner = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={},
        repo_env_path=env_path,
        parent_env_path=parent_env_path,
        prime_process=False,
    )
    assert isinstance(environment_owner, LoadedSecrets)
    platform = platform_capabilities_for(
        os_family="posix",
        runtime_name="linux",
        user_id=1000,
        temp_directory=tmp_path / "platform-tmp",
    )
    sensitive_document = SensitiveEnvDocument(
        env_path,
        backup_root=tmp_path / "sandbox-state" / "sensitive-backups",
        environment_owner=environment_owner,
        clock=lambda: now[0],
        preview_factory=lambda: next(preview_tokens),
        receipt_ttl_seconds=10.0,
        platform_capabilities=platform,
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {}, layer="synthetic_inherited"
        ),
        background_health_code=None,
        platform_capabilities=platform,
        sensitive_document=sensitive_document,
        enabled_write_capabilities=frozenset({"sensitive_write"}),
        sensitive_preview_clock=lambda: now[0],
        sensitive_preview_ttl_seconds=10.0,
    )

    assert repr(services) == (
        "OwnerBackedConfigStudioServices(<fixed production owners>)"
    )
    assert "read-only" not in repr(services)

    previews = [
        services.preview_sensitive(
            ClearMappedOverride("MODEL"), session_id="synthetic-session"
        )
        for _ in range(64)
    ]
    now[0] += 11.0

    replacement = services.preview_sensitive(
        ClearMappedOverride("MODEL"), session_id="synthetic-session"
    )

    assert replacement["preview_id"] == "synthetic-sensitive-preview-064"
    with pytest.raises(ConfigStudioServiceError) as expired:
        services.commit_sensitive_preview(
            str(previews[0]["preview_id"]),
            None,
            session_id="synthetic-session",
        )
    assert expired.value.code == "CONFIRMATION_REQUIRED"
