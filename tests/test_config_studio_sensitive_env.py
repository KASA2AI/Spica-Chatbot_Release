from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import stat

import pytest

from spica.config.env_roster import (
    APP_ENV_MAP,
    LEGACY_ENV_VARS,
    RESPEAKER_ENV_MAP,
    RUNTIME_CACHE_ENV_MAP,
    SCREEN_ENV_MAP,
    SECRETS_ENV_MAP,
)
from spica.config.environment_snapshot import EnvironmentSnapshot
from spica.config.secrets import (
    EnvironmentRefreshError,
    LoadedSecrets,
    Secrets,
    load_secrets,
)
from spica.adapters.config_studio.platform import platform_capabilities_for
from spica.config_studio.sensitive_env import (
    ClearSecret,
    ClearMappedOverride,
    OverrideRollbackChange,
    RollbackConfirmation,
    SetSecret,
    SensitiveRollbackPreview,
    SensitiveEnvDocument as _SensitiveEnvDocument,
    SensitiveEnvError,
)


def test_sensitive_rollback_dtos_never_repr_values_paths_or_receipts() -> None:
    secret = "synthetic-rollback-repr-secret"
    private_path = "/outside/private/synthetic-owner-path"
    receipt = "synthetic-one-time-receipt"
    change = OverrideRollbackChange(
        environment_variable="MODEL",
        affected_fields=("llm.model",),
        before_next_launch=secret,
        after_next_launch=private_path,
        winning_source_before="file",
        winning_source_after="default",
        still_shadowed=False,
    )
    preview = SensitiveRollbackPreview(
        restore_point_id="opaque-restore-point",
        secret_changes=(),
        override_changes=(change,),
        unmanaged_content_changed=False,
        unmanaged_change_count=0,
        permission_hardening=False,
        resolution_error_before=False,
        resolution_error_after=False,
    )
    confirmation = RollbackConfirmation(
        receipt_token=receipt,
        preview=preview,
    )

    rendered = repr(change) + repr(preview) + repr(confirmation)

    assert secret not in rendered
    assert private_path not in rendered
    assert receipt not in rendered


def _sensitive_document(
    document,
    *,
    backup_root,
    inherited_environment,
    parent_environment,
    **kwargs,
):
    """Build the production owner from explicit, synthetic test layers."""

    parent_path = backup_root.parent / f".{backup_root.name}-parent.env"
    parent_path.parent.mkdir(parents=True, exist_ok=True)
    parent_lines = []
    for name, value in parent_environment.items():
        quoted = value.replace("\\", "\\\\").replace("'", "\\'")
        parent_lines.append(f"{name}='{quoted}'\n")
    parent_path.write_text("".join(parent_lines), encoding="utf-8")
    owner = load_secrets(
        with_environment_snapshot=True,
        inherited_environment=inherited_environment,
        repo_env_path=document,
        parent_env_path=parent_path,
        prime_process=False,
    )
    kwargs.setdefault(
        "platform_capabilities",
        platform_capabilities_for(
            os_family="posix",
            runtime_name="linux",
            user_id=os.getuid(),
            temp_directory=backup_root.parent / "platform-tmp",
        ),
    )
    return _SensitiveEnvDocument(
        document,
        backup_root=backup_root,
        environment_owner=owner,
        **kwargs,
    )


def test_production_sensitive_owner_rejects_raw_layer_mappings_and_is_opaque(
    tmp_path,
):
    document = tmp_path / "repo" / "xiaosan.env"
    parent = tmp_path / "parent" / "xiaosan.env"
    document.parent.mkdir()
    parent.parent.mkdir()
    document.write_text("MODEL=repo-model\n", encoding="utf-8")
    parent.write_text("MODEL=parent-model\n", encoding="utf-8")
    secret = "inherited-secret-canary"
    owner = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={"OPENAI_API_KEY": secret},
        repo_env_path=document,
        parent_env_path=parent,
        prime_process=False,
    )

    managed = _SensitiveEnvDocument(
        document,
        backup_root=tmp_path / "backups",
        environment_owner=owner,
        platform_capabilities=platform_capabilities_for(
            os_family="posix",
            runtime_name="linux",
            user_id=os.getuid(),
            temp_directory=tmp_path / "platform-tmp",
        ),
    )

    assert not hasattr(managed, "_inherited")
    assert not hasattr(managed, "_parent")
    assert secret not in repr(managed)
    with pytest.raises(TypeError):
        json.dumps(managed)
    with pytest.raises(TypeError):
        _SensitiveEnvDocument(
            document,
            backup_root=tmp_path / "other-backups",
            inherited_environment={"OPENAI_API_KEY": secret},
            parent_environment={},
        )


def test_status_exposes_only_secret_configuration_and_permission_health(tmp_path):
    document = tmp_path / "xiaosan.env"
    document.write_bytes(
        b"OPENAI_API_KEY='repo-openai-canary'\n"
        b"BILIBILI_COOKIE=''\n"
        b"MODEL=repo-model\n"
    )
    document.chmod(0o664)
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={"JUDGE_API_KEY": "inherited-judge-canary"},
        parent_environment={"QBITTORRENT_PASSWORD": "parent-qbit-canary"},
    )

    status = managed.status()

    assert {item.slot: item.configured for item in status.secret_slots} == {
        "openai_api_key": True,
        "judge_api_key": True,
        "bilibili_cookie": False,
        "qbittorrent_password": True,
    }
    assert status.permission_health == "TOO_PERMISSIVE"
    rendered = repr(status)
    assert "repo-openai-canary" not in rendered
    assert "inherited-judge-canary" not in rendered
    assert "parent-qbit-canary" not in rendered
    assert str(document) not in rendered


def test_clear_secret_falls_back_to_parent_owner_without_exposing_its_value(
    tmp_path,
):
    document = tmp_path / "xiaosan.env"
    document.write_bytes(b"OPENAI_API_KEY=repo-secret-canary\n")
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={"OPENAI_API_KEY": "parent-secret-canary"},
    )
    preview = managed.preview(
        ClearSecret("openai_api_key"),
        session_id="owner-session",
    )

    assert preview.winning_source_before == "repo_dotenv"
    assert preview.winning_source_after == "parent_dotenv"
    assert preview.still_shadowed is True
    assert preview.before_next_launch is None
    assert preview.after_next_launch is None
    rendered = repr(managed) + repr(preview)
    assert "repo-secret-canary" not in rendered
    assert "parent-secret-canary" not in rendered

    confirmation = managed.prepare_secret_clear(
        preview,
        session_id="owner-session",
    )
    managed.commit(
        preview,
        session_id="owner-session",
        confirmation_token=confirmation.receipt_token,
    )

    configured = {
        item.slot: item.configured for item in managed.status().secret_slots
    }
    assert configured["openai_api_key"] is True


def test_mapped_override_commit_rejects_parent_owner_change_after_preview(
    tmp_path,
) -> None:
    document = tmp_path / "xiaosan.env"
    original = b"MODEL=repo-model\n"
    document.write_bytes(original)
    backup_root = tmp_path / "backups"
    managed = _sensitive_document(
        document,
        backup_root=backup_root,
        inherited_environment={},
        parent_environment={},
        base_document={"llm": {"model": "file-model"}},
    )
    preview = managed.preview(
        ClearMappedOverride("MODEL"),
        session_id="synthetic-session",
    )
    assert preview.after_next_launch == "file-model"
    assert preview.winning_source_after == "file"
    parent_path = backup_root.parent / f".{backup_root.name}-parent.env"
    parent_path.write_bytes(b"MODEL=late-parent-model\n")

    with pytest.raises(SensitiveEnvError) as caught:
        managed.commit(preview, session_id="synthetic-session")

    assert caught.value.code == "CONFIRMATION_REQUIRED"
    assert document.read_bytes() == original


def test_secret_clear_commit_rejects_parent_owner_change_after_receipt(
    tmp_path,
) -> None:
    document = tmp_path / "xiaosan.env"
    original = b"OPENAI_API_KEY=repo-secret\n"
    document.write_bytes(original)
    backup_root = tmp_path / "backups"
    managed = _sensitive_document(
        document,
        backup_root=backup_root,
        inherited_environment={},
        parent_environment={},
    )
    preview = managed.preview(
        ClearSecret("openai_api_key"),
        session_id="synthetic-session",
    )
    confirmation = managed.prepare_secret_clear(
        preview,
        session_id="synthetic-session",
    )
    parent_path = backup_root.parent / f".{backup_root.name}-parent.env"
    parent_path.write_bytes(b"OPENAI_API_KEY=late-parent-secret\n")

    with pytest.raises(SensitiveEnvError) as caught:
        managed.commit(
            preview,
            session_id="synthetic-session",
            confirmation_token=confirmation.receipt_token,
        )

    assert caught.value.code == "CONFIRMATION_REQUIRED"
    assert document.read_bytes() == original


def test_secret_clear_receipt_binds_opaque_parent_secret_material(
    tmp_path,
) -> None:
    document = tmp_path / "xiaosan.env"
    original = b"OPENAI_API_KEY=repo-secret\n"
    document.write_bytes(original)
    backup_root = tmp_path / "backups"
    managed = _sensitive_document(
        document,
        backup_root=backup_root,
        inherited_environment={},
        parent_environment={"OPENAI_API_KEY": "parent-secret-a"},
    )
    preview = managed.preview(
        ClearSecret("openai_api_key"),
        session_id="synthetic-session",
    )
    confirmation = managed.prepare_secret_clear(
        preview,
        session_id="synthetic-session",
    )
    parent_path = backup_root.parent / f".{backup_root.name}-parent.env"
    parent_path.write_bytes(b"OPENAI_API_KEY=parent-secret-b\n")

    with pytest.raises(SensitiveEnvError) as caught:
        managed.commit(
            preview,
            session_id="synthetic-session",
            confirmation_token=confirmation.receipt_token,
        )

    assert caught.value.code == "CONFIRMATION_REQUIRED"
    assert document.read_bytes() == original


def test_clear_mapped_override_removes_duplicates_and_preserves_unrelated_bytes(
    tmp_path,
):
    document = tmp_path / "xiaosan.env"
    original = (
        b"# keep heading\r\n"
        b"\r\n"
        b"export MODEL = 'first-model'\r\n"
        b"OPENAI_API_KEY='line one\r\nline two\\\\quote'\r\n"
        b"MODEL=second-model\r\n"
        + "UNICODE='星空'\r\n".encode()
        + b"JSON_CANARY='{\"MODEL\":\"false\"}'\r\n"
    )
    document.write_bytes(original)
    document.chmod(0o664)
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={"MODEL": "parent-model"},
    )

    preview = managed.preview(ClearMappedOverride("MODEL"))

    assert preview.command_kind == "clear_mapped_override"
    assert preview.target == "MODEL"
    assert preview.affected_fields == ("llm.model",)
    assert preview.before_next_launch == "second-model"
    assert preview.after_next_launch == "parent-model"
    assert preview.winning_source_before == "repo_dotenv"
    assert preview.winning_source_after == "parent_dotenv"
    assert preview.still_shadowed is True
    assert preview.permission_hardening is True
    assert preview.changed is True
    assert "line one" not in repr(preview)

    committed = managed.commit(preview)

    assert committed.restore_point_id
    assert document.read_bytes() == (
        b"# keep heading\r\n"
        b"\r\n"
        b"OPENAI_API_KEY='line one\r\nline two\\\\quote'\r\n"
        + "UNICODE='星空'\r\n".encode()
        + b"JSON_CANARY='{\"MODEL\":\"false\"}'\r\n"
    )
    assert (os.stat(document).st_mode & 0o777) == 0o600


def test_secret_set_is_write_only_roundtrips_and_clear_removes_all_definitions(
    tmp_path,
):
    document = tmp_path / "xiaosan.env"
    document.write_bytes(
        b"OPENAI_API_KEY=old-one\n"
        b"export OPENAI_API_KEY='old-two'\n"
        + "KEEP='星空\\\\value'\n".encode()
    )
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
    )
    secret_value = "line one\r\nline 'two' \\ path 星空"
    command = SetSecret("openai_api_key", secret_value)

    assert secret_value not in repr(command)
    set_preview = managed.preview(command)
    assert set_preview.secret_change == "will_replace"
    assert set_preview.before_next_launch is None
    assert set_preview.after_next_launch is None
    assert secret_value not in repr(set_preview)
    managed.commit(set_preview)

    assert _values_for(document, "OPENAI_API_KEY") == [secret_value]
    assert "KEEP='星空\\\\value'\n".encode() in document.read_bytes()
    assert {
        item.slot: item.configured for item in managed.status().secret_slots
    }["openai_api_key"] is True

    clear_preview = managed.preview(
        ClearSecret("openai_api_key"), session_id="owner-session"
    )
    assert clear_preview.secret_change == "will_clear"
    with pytest.raises(SensitiveEnvError) as unconfirmed:
        managed.commit(clear_preview, session_id="owner-session")
    assert unconfirmed.value.code == "SECRET_CLEAR_CONFIRMATION_REQUIRED"

    confirmation = managed.prepare_secret_clear(
        clear_preview,
        session_id="owner-session",
    )
    with pytest.raises(SensitiveEnvError) as wrong_session:
        managed.commit(
            clear_preview,
            session_id="other-session",
            confirmation_token=confirmation.receipt_token,
        )
    assert wrong_session.value.code == "SECRET_CLEAR_CONFIRMATION_INVALID"

    managed.commit(
        clear_preview,
        session_id="owner-session",
        confirmation_token=confirmation.receipt_token,
    )

    with pytest.raises(SensitiveEnvError) as reused:
        managed.commit(
            clear_preview,
            session_id="owner-session",
            confirmation_token=confirmation.receipt_token,
        )
    assert reused.value.code in {
        "PREVIEW_INVALID",
        "SECRET_CLEAR_CONFIRMATION_INVALID",
    }

    assert _values_for(document, "OPENAI_API_KEY") == []
    assert "KEEP='星空\\\\value'\n".encode() in document.read_bytes()


def test_secret_set_does_not_depend_on_the_app_document_owner(tmp_path):
    document = tmp_path / "xiaosan.env"
    document.write_bytes(b"KEEP=unchanged\n")

    def unavailable_app_owner():
        raise AssertionError("secret authoring must not read app.yaml")

    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
        base_document_owner=unavailable_app_owner,
    )

    preview = managed.preview(SetSecret("openai_api_key", "synthetic-secret"))
    managed.commit(preview)

    assert preview.secret_change == "will_set"
    assert _values_for(document, "OPENAI_API_KEY") == ["synthetic-secret"]


def test_secret_set_rejects_values_that_owner_dotenv_would_interpolate(tmp_path):
    document = tmp_path / "xiaosan.env"
    original = b"KEEP=unchanged\n"
    document.write_bytes(original)
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={"HOME": "/synthetic-home"},
        parent_environment={},
    )
    secret_value = "prefix-${HOME}-suffix"

    with pytest.raises(SensitiveEnvError) as raised:
        managed.preview(SetSecret("openai_api_key", secret_value))

    assert raised.value.code == "SECRET_VALUE_UNREPRESENTABLE"
    assert secret_value not in str(raised.value)
    assert document.read_bytes() == original
    assert not (tmp_path / "backups").exists()


def test_sensitive_preview_is_immutable_and_commit_uses_server_stored_candidate(
    tmp_path,
):
    document = tmp_path / "xiaosan.env"
    document.write_bytes(b"MODEL=safe-model\n")
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
    )
    preview = managed.preview(
        ClearMappedOverride("MODEL"),
        session_id="owner-session",
    )

    with pytest.raises(FrozenInstanceError):
        preview.target = "OPENAI_API_KEY"
    assert not hasattr(preview, "_candidate")

    managed.commit(preview, session_id="owner-session")

    assert document.read_bytes() == b""


def test_invalid_mapped_override_can_be_cleared_as_a_repair(tmp_path):
    document = tmp_path / "xiaosan.env"
    document.write_bytes(b'MODEL="unterminated\nKEEP=1\n')
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
    )

    preview = managed.preview(ClearMappedOverride("MODEL"))

    assert preview.resolution_error_before is True
    assert preview.resolution_error_after is False
    managed.commit(preview)
    assert document.read_bytes() == b"KEEP=1\n"


def test_override_preview_uses_production_owner_coercion_not_raw_dotenv_text(
    tmp_path,
):
    document = tmp_path / "xiaosan.env"
    document.write_bytes(b"SPICA_SCREEN_ENABLED=false\n")
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
        base_document={"screen": {"enabled": True}},
    )

    preview = managed.preview(ClearMappedOverride("SPICA_SCREEN_ENABLED"))

    assert preview.before_next_launch is False
    assert preview.after_next_launch is True
    assert preview.winning_source_before == "repo_dotenv"
    assert preview.winning_source_after == "file"
    assert preview.still_shadowed is False
    assert preview.resolution_error_before is False
    assert preview.resolution_error_after is False


@pytest.mark.parametrize(
    (
        "secret_value",
        "override_name",
        "override_value",
        "base_document",
        "before_value",
    ),
    (
        (
            "123",
            "RECENT_MEMORY_TURNS",
            "7",
            {"memory": {"recent_memory_turns": 123}},
            7,
        ),
        (
            "false",
            "SPICA_SCREEN_ENABLED",
            "true",
            {"screen": {"enabled": False}},
            True,
        ),
    ),
)
def test_override_preview_redacts_canonical_scalar_secret_values(
    tmp_path,
    secret_value,
    override_name,
    override_value,
    base_document,
    before_value,
) -> None:
    document = tmp_path / "xiaosan.env"
    document.write_text(
        f"OPENAI_API_KEY={secret_value}\n"
        f"{override_name}={override_value}\n",
        encoding="utf-8",
    )
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
        base_document=base_document,
    )

    preview = managed.preview(ClearMappedOverride(override_name))

    assert preview.before_next_launch == before_value
    assert preview.after_next_launch == "«REDACTED:OPENAI_API_KEY»"
    assert preview.target == override_name
    assert preview.winning_source_before == "repo_dotenv"
    assert preview.winning_source_after == "file"


def test_override_commit_requires_a_new_preview_when_app_owner_changes(tmp_path):
    document = tmp_path / "xiaosan.env"
    original = b"MODEL=repo-model\n"
    document.write_bytes(original)
    current_app = {"llm": {"model": "first-file-model"}}
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
        base_document_owner=lambda: current_app,
    )

    preview = managed.preview(ClearMappedOverride("MODEL"))
    assert preview.after_next_launch == "first-file-model"
    current_app["llm"]["model"] = "changed-file-model"

    with pytest.raises(SensitiveEnvError) as stale:
        managed.commit(preview)

    assert stale.value.code == "CONFIRMATION_REQUIRED"
    assert document.read_bytes() == original
    assert not (tmp_path / "backups").exists()


def test_invalid_owner_override_can_be_cleared_and_shows_error_to_candidate_value(
    tmp_path,
):
    document = tmp_path / "xiaosan.env"
    document.write_bytes(b"RECENT_MEMORY_TURNS=not-an-integer\n")
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
        base_document={"memory": {"recent_memory_turns": 7}},
    )

    preview = managed.preview(ClearMappedOverride("RECENT_MEMORY_TURNS"))

    assert preview.resolution_error_before is True
    assert preview.before_next_launch is None
    assert preview.resolution_error_after is False
    assert preview.after_next_launch == 7
    assert preview.winning_source_after == "file"


def test_secret_interpolated_into_mapped_override_is_quarantined_and_never_previewed(
    tmp_path,
):
    secret = "synthetic-secret-canary"
    document = tmp_path / "xiaosan.env"
    document.write_text(
        f"OPENAI_API_KEY={secret}\nMODEL=${{OPENAI_API_KEY}}\n",
        encoding="utf-8",
    )
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
        base_document={"llm": {"model": "safe-file-model"}},
    )

    preview = managed.preview(ClearMappedOverride("MODEL"))

    assert preview.resolution_error_before is True
    assert preview.before_next_launch is None
    assert preview.resolution_error_after is False
    assert preview.after_next_launch == "safe-file-model"
    assert secret not in repr(preview)


def test_inherited_secret_taint_remains_winner_after_repo_override_clear(tmp_path):
    secret = "inherited-secret-canary"
    document = tmp_path / "xiaosan.env"
    document.write_text("MODEL=repo-safe-model\n", encoding="utf-8")
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={
            "OPENAI_API_KEY": secret,
            "MODEL": f"prefix-{secret}",
        },
        parent_environment={"MODEL": "parent-safe-model"},
        base_document={"llm": {"model": "file-safe-model"}},
    )

    preview = managed.preview(ClearMappedOverride("MODEL"))

    assert preview.before_next_launch is None
    assert preview.after_next_launch is None
    assert preview.winning_source_before == "inherited"
    assert preview.winning_source_after == "inherited"
    assert preview.still_shadowed is True
    assert preview.resolution_error_before is True
    assert preview.resolution_error_after is True
    assert secret not in repr(preview)


def test_secret_set_retains_crlf_style_when_all_old_definitions_are_replaced(
    tmp_path,
):
    document = tmp_path / "xiaosan.env"
    document.write_bytes(
        b"OPENAI_API_KEY=first\r\nexport OPENAI_API_KEY=second\r\n"
    )
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
    )

    managed.commit(managed.preview(SetSecret("openai_api_key", "replacement")))

    assert document.read_bytes() == b"OPENAI_API_KEY='replacement'\r\n"


def test_first_approved_noop_write_still_hardens_live_file_permissions(tmp_path):
    document = tmp_path / "xiaosan.env"
    document.write_bytes(b"KEEP=1\n")
    document.chmod(0o664)
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={"MODEL": "inherited-model"},
        parent_environment={},
    )

    preview = managed.preview(ClearMappedOverride("MODEL"))
    assert preview.changed is False
    assert preview.permission_hardening is True
    assert preview.still_shadowed is True

    committed = managed.commit(preview)

    assert committed.restore_point_id
    assert document.read_bytes() == b"KEEP=1\n"
    assert (os.stat(document).st_mode & 0o777) == 0o600


def test_override_clear_allowlist_is_derived_only_from_app_and_screen_rosters(
    tmp_path,
):
    document = tmp_path / "xiaosan.env"
    document.write_bytes(b"")
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
    )
    expected = {
        **{name: field for field, name in APP_ENV_MAP.items()},
        **{name: f"screen.{field}" for field, name in SCREEN_ENV_MAP.items()},
    }

    for environment_name, field_path in expected.items():
        preview = managed.preview(ClearMappedOverride(environment_name))
        assert preview.affected_fields == (field_path,)

    excluded = (
        set(RESPEAKER_ENV_MAP.values())
        | set(RUNTIME_CACHE_ENV_MAP.values())
        | set(SECRETS_ENV_MAP.values())
        | set(LEGACY_ENV_VARS)
    )
    for environment_name in excluded:
        with pytest.raises(SensitiveEnvError) as caught:
            managed.preview(ClearMappedOverride(environment_name))
        assert caught.value.code == "OVERRIDE_NOT_MANAGED"


@pytest.mark.skipif(os.name != "posix", reason="POSIX hardlink contract")
def test_hardlinked_sensitive_document_is_read_only_and_fail_closed(tmp_path):
    source = tmp_path / "outside.env"
    source.write_bytes(b"MODEL=outside\n")
    document = tmp_path / "xiaosan.env"
    os.link(source, document)
    with pytest.raises(EnvironmentRefreshError) as owner_rejected:
        _sensitive_document(
            document,
            backup_root=tmp_path / "backups",
            inherited_environment={},
            parent_environment={},
        )
    assert str(owner_rejected.value) == "environment owner document unsafe"

    managed = _SensitiveEnvDocument(
        document,
        backup_root=tmp_path / "backups",
        environment_owner=LoadedSecrets(
            secrets=Secrets(),
            environment_snapshot=EnvironmentSnapshot.from_mapping(
                {}, layer="synthetic"
            ),
        ),
        platform_capabilities=platform_capabilities_for(
            os_family="posix",
            runtime_name="linux",
            user_id=os.getuid(),
            temp_directory=tmp_path / "platform-tmp",
        ),
    )

    assert managed.status().permission_health == "MULTIPLE_LINKS"
    with pytest.raises(SensitiveEnvError) as caught:
        managed.preview(ClearMappedOverride("MODEL"))
    assert caught.value.code == "MULTIPLE_LINKS"
    assert source.read_bytes() == b"MODEL=outside\n"


def test_windows_sensitive_write_is_disabled_without_verified_dacl(
    tmp_path,
):
    document = tmp_path / "xiaosan.env"
    document.write_bytes(b"MODEL=repo\n")
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
        platform_capabilities=platform_capabilities_for(
            os_family="nt",
            runtime_name="win32",
            user_id=None,
            temp_directory=tmp_path,
        ),
    )
    preview = managed.preview(ClearMappedOverride("MODEL"))

    with pytest.raises(SensitiveEnvError) as caught:
        managed.commit(preview)

    assert caught.value.code == "SENSITIVE_WRITES_UNVERIFIED_ON_WINDOWS"
    assert document.read_bytes() == b"MODEL=repo\n"


def test_whole_document_rollback_requires_bound_one_time_receipt_and_is_undoable(
    tmp_path,
):
    document = tmp_path / "xiaosan.env"
    original = (
        b"OPENAI_API_KEY='original-secret-canary'\n"
        b"MODEL=repo-model\n"
        b"KEEP=original\n"
    )
    document.write_bytes(original)
    tokens = iter(("receipt-one", "receipt-two"))
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={"MODEL": "parent-model"},
        receipt_factory=lambda: next(tokens),
    )
    cleared = managed.commit(managed.preview(ClearMappedOverride("MODEL")))
    current = (
        b"OPENAI_API_KEY='current-secret-canary'\n"
        b"KEEP=changed-by-other-writer\n"
    )
    document.write_bytes(current)

    confirmation = managed.prepare_rollback(
        cleared.restore_point_id,
        session_id="session-a",
    )

    secret_changes = {
        item.slot: item.change for item in confirmation.preview.secret_changes
    }
    assert secret_changes["openai_api_key"] == "will_replace"
    model_change, = [
        item
        for item in confirmation.preview.override_changes
        if item.environment_variable == "MODEL"
    ]
    assert model_change.before_next_launch == "parent-model"
    assert model_change.after_next_launch == "repo-model"
    assert model_change.winning_source_before == "parent_dotenv"
    assert model_change.winning_source_after == "repo_dotenv"
    assert confirmation.preview.unmanaged_content_changed is True
    assert confirmation.preview.unmanaged_change_count > 0
    assert "original-secret-canary" not in repr(confirmation)
    assert "current-secret-canary" not in repr(confirmation)

    rolled_back = managed.rollback(
        confirmation.receipt_token,
        session_id="session-a",
    )

    assert document.read_bytes() == original
    assert rolled_back.restore_point_id
    with pytest.raises(SensitiveEnvError) as reused:
        managed.rollback(confirmation.receipt_token, session_id="session-a")
    assert reused.value.code == "ROLLBACK_CONFIRMATION_INVALID"

    undo_confirmation = managed.prepare_rollback(
        rolled_back.restore_point_id,
        session_id="session-a",
    )
    managed.rollback(undo_confirmation.receipt_token, session_id="session-a")
    assert document.read_bytes() == current


def test_sensitive_rollback_dto_redacts_historical_secret_fallback(
    tmp_path,
) -> None:
    old_secret = "synthetic-historical-secret"
    new_secret = "synthetic-current-secret"
    document = tmp_path / "xiaosan.env"
    document.write_text(
        f"OPENAI_API_KEY={old_secret}\nMODEL=safe-override\n",
        encoding="utf-8",
    )
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
        base_document={"llm": {"model": old_secret}},
    )
    committed = managed.commit(managed.preview(ClearMappedOverride("MODEL")))
    document.write_text(
        f"OPENAI_API_KEY={new_secret}\n",
        encoding="utf-8",
    )
    document.chmod(0o600)

    confirmation = managed.prepare_rollback(
        committed.restore_point_id,
        session_id="synthetic-session",
    )

    model_change = next(
        change
        for change in confirmation.preview.override_changes
        if change.environment_variable == "MODEL"
    )
    assert model_change.before_next_launch == "«REDACTED:OPENAI_API_KEY»"
    assert model_change.after_next_launch == "safe-override"
    assert old_secret not in repr(confirmation)
    assert new_secret not in repr(confirmation)


def test_sensitive_rollback_redacts_canonical_integer_secret_value(
    tmp_path,
) -> None:
    document = tmp_path / "xiaosan.env"
    document.write_bytes(
        b"OPENAI_API_KEY=123\nRECENT_MEMORY_TURNS=7\n"
    )
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
        base_document={"memory": {"recent_memory_turns": 123}},
    )
    committed = managed.commit(
        managed.preview(ClearMappedOverride("RECENT_MEMORY_TURNS"))
    )

    confirmation = managed.prepare_rollback(
        committed.restore_point_id,
        session_id="synthetic-session",
    )

    change = next(
        item
        for item in confirmation.preview.override_changes
        if item.environment_variable == "RECENT_MEMORY_TURNS"
    )
    assert change.before_next_launch == "«REDACTED:OPENAI_API_KEY»"
    assert change.after_next_launch == 7
    assert change.winning_source_before == "file"
    assert change.winning_source_after == "repo_dotenv"


def test_rollback_receipt_binds_opaque_parent_secret_material(tmp_path) -> None:
    document = tmp_path / "xiaosan.env"
    original = b"OPENAI_API_KEY=repo-secret\nMODEL=repo-model\n"
    document.write_bytes(original)
    backup_root = tmp_path / "backups"
    managed = _sensitive_document(
        document,
        backup_root=backup_root,
        inherited_environment={},
        parent_environment={"OPENAI_API_KEY": "parent-secret-a"},
    )
    committed = managed.commit(managed.preview(ClearMappedOverride("MODEL")))
    current = document.read_bytes()
    confirmation = managed.prepare_rollback(
        committed.restore_point_id,
        session_id="synthetic-session",
    )
    parent_path = backup_root.parent / f".{backup_root.name}-parent.env"
    parent_path.write_bytes(b"OPENAI_API_KEY=parent-secret-b\n")

    with pytest.raises(SensitiveEnvError) as caught:
        managed.rollback(
            confirmation.receipt_token,
            session_id="synthetic-session",
        )

    assert caught.value.code == "ROLLBACK_CONFIRMATION_INVALID"
    assert document.read_bytes() == current


def test_rollback_receipt_is_bound_to_session_and_short_ttl(tmp_path):
    document = tmp_path / "xiaosan.env"
    document.write_bytes(b"MODEL=repo\n")
    now = [10.0]
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
        clock=lambda: now[0],
        receipt_factory=lambda: "ttl-receipt",
        receipt_ttl_seconds=30,
    )
    committed = managed.commit(managed.preview(ClearMappedOverride("MODEL")))
    confirmation = managed.prepare_rollback(
        committed.restore_point_id,
        session_id="owner-session",
    )

    with pytest.raises(SensitiveEnvError) as wrong_session:
        managed.rollback(confirmation.receipt_token, session_id="other-session")
    assert wrong_session.value.code == "ROLLBACK_CONFIRMATION_INVALID"

    now[0] = 40.0
    with pytest.raises(SensitiveEnvError) as expired:
        managed.rollback(confirmation.receipt_token, session_id="owner-session")
    assert expired.value.code == "ROLLBACK_CONFIRMATION_EXPIRED"
    assert document.read_bytes() == b""


def test_sensitive_rollback_detects_unmanaged_dotenv_reordering(tmp_path):
    document = tmp_path / "xiaosan.env"
    original = b"KEEP_A=one\nKEEP_B=${KEEP_A}\nMODEL=repo\n"
    document.write_bytes(original)
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
    )
    committed = managed.commit(managed.preview(ClearMappedOverride("MODEL")))
    document.write_bytes(b"KEEP_B=${KEEP_A}\nKEEP_A=one\n")

    confirmation = managed.prepare_rollback(
        committed.restore_point_id,
        session_id="session-a",
    )

    assert confirmation.preview.unmanaged_content_changed is True
    assert confirmation.preview.unmanaged_change_count >= 2


def test_rollback_receipt_consumes_on_revision_conflict(tmp_path):
    document = tmp_path / "xiaosan.env"
    document.write_bytes(b"MODEL=repo\n")
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
        receipt_factory=lambda: "conflict-receipt",
    )
    committed = managed.commit(managed.preview(ClearMappedOverride("MODEL")))
    confirmation = managed.prepare_rollback(
        committed.restore_point_id,
        session_id="session-a",
    )
    document.write_bytes(b"KEEP=concurrent-change\n")

    with pytest.raises(SensitiveEnvError) as conflict:
        managed.rollback(confirmation.receipt_token, session_id="session-a")
    assert conflict.value.code == "DOCUMENT_CONFLICT"
    with pytest.raises(SensitiveEnvError) as consumed:
        managed.rollback(confirmation.receipt_token, session_id="session-a")
    assert consumed.value.code == "ROLLBACK_CONFIRMATION_INVALID"
    assert document.read_bytes() == b"KEEP=concurrent-change\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_sensitive_restore_storage_retains_one_private_restore_point(tmp_path):
    document = tmp_path / "xiaosan.env"
    document.write_bytes(b"OPENAI_API_KEY='original'\n")
    backup_root = tmp_path / "backups"
    managed = _sensitive_document(
        document,
        backup_root=backup_root,
        inherited_environment={},
        parent_environment={},
    )
    first = managed.commit(managed.preview(SetSecret("openai_api_key", "first")))
    second = managed.commit(managed.preview(SetSecret("openai_api_key", "second")))

    with pytest.raises(SensitiveEnvError) as pruned:
        managed.prepare_rollback(first.restore_point_id, session_id="session-a")
    assert pruned.value.code == "NO_VALID_RESTORE_POINT"
    metadata_files = list(backup_root.rglob("metadata"))
    assert len(metadata_files) == 1
    restore_dir = metadata_files[0].parent
    assert restore_dir.name == second.restore_point_id
    assert (backup_root.stat().st_mode & 0o777) == 0o700
    assert (restore_dir.stat().st_mode & 0o777) == 0o700
    assert (metadata_files[0].stat().st_mode & 0o777) == 0o600
    assert ((restore_dir / "content").stat().st_mode & 0o777) == 0o600


def test_rollback_rejects_a_restore_point_with_invalid_dotenv(tmp_path):
    document = tmp_path / "xiaosan.env"
    document.write_bytes(b'MODEL="unterminated\n')
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
    )
    repaired = managed.commit(managed.preview(ClearMappedOverride("MODEL")))

    with pytest.raises(SensitiveEnvError) as invalid:
        managed.prepare_rollback(repaired.restore_point_id, session_id="session-a")

    assert invalid.value.code == "NO_VALID_RESTORE_POINT"
    assert document.read_bytes() == b""


def test_rollback_rejects_a_restore_point_the_production_owner_cannot_resolve(
    tmp_path,
):
    document = tmp_path / "xiaosan.env"
    document.write_bytes(b"RECENT_MEMORY_TURNS=not-an-integer\n")
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
        base_document={"memory": {"recent_memory_turns": 7}},
    )
    repaired = managed.commit(
        managed.preview(ClearMappedOverride("RECENT_MEMORY_TURNS"))
    )

    with pytest.raises(SensitiveEnvError) as invalid:
        managed.prepare_rollback(repaired.restore_point_id, session_id="session-a")

    assert invalid.value.code == "NO_VALID_RESTORE_POINT"
    assert document.read_bytes() == b""


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_sensitive_rollback_rejects_restore_storage_that_is_no_longer_private(
    tmp_path,
):
    document = tmp_path / "xiaosan.env"
    document.write_bytes(b"MODEL=repo\n")
    backup_root = tmp_path / "backups"
    managed = _sensitive_document(
        document,
        backup_root=backup_root,
        inherited_environment={},
        parent_environment={},
    )
    committed = managed.commit(managed.preview(ClearMappedOverride("MODEL")))
    restore_dir, = backup_root.rglob(committed.restore_point_id)
    (restore_dir / "content").chmod(0o640)

    with pytest.raises(SensitiveEnvError) as unsafe:
        managed.prepare_rollback(
            committed.restore_point_id,
            session_id="session-a",
        )

    assert unsafe.value.code == "NO_VALID_RESTORE_POINT"


def test_permission_hardening_failure_aborts_before_replacing_live_file(
    tmp_path, monkeypatch
):
    document = tmp_path / "xiaosan.env"
    original = b"MODEL=repo\n"
    document.write_bytes(original)
    document.chmod(0o664)
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
    )
    preview = managed.preview(ClearMappedOverride("MODEL"))

    def reject_chmod(descriptor, mode):
        raise PermissionError("synthetic chmod failure")

    monkeypatch.setattr(
        "spica.config.document_transaction.os.fchmod",
        reject_chmod,
    )
    with pytest.raises(SensitiveEnvError) as failed:
        managed.commit(preview)

    assert failed.value.code == "PERMISSION_HARDENING_FAILED"
    assert document.read_bytes() == original
    assert (document.stat().st_mode & 0o777) == 0o664


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_sensitive_commit_restores_original_and_fails_on_permission_interference(
    tmp_path, monkeypatch
):
    document = tmp_path / "xiaosan.env"
    original = b"MODEL=repo\n"
    document.write_bytes(original)
    document.chmod(0o664)
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
    )
    preview = managed.preview(ClearMappedOverride("MODEL"))
    real_replace = os.replace

    def replace_then_relax_permissions(source, target):
        real_replace(source, target)
        os.chmod(target, 0o640)

    monkeypatch.setattr(
        "spica.config.document_transaction.os.replace",
        replace_then_relax_permissions,
    )

    with pytest.raises(SensitiveEnvError) as failed:
        managed.commit(preview)

    assert failed.value.code == "PERMISSION_HARDENING_FAILED"
    assert document.read_bytes() == original
    assert (document.stat().st_mode & 0o777) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_sensitive_commit_uses_final_fstat_permission_facts(
    tmp_path, monkeypatch
):
    document = tmp_path / "xiaosan.env"
    original = b"KEEP=original\n"
    document.write_bytes(original)
    document.chmod(0o600)
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
    )
    preview = managed.preview(
        SetSecret("openai_api_key", "stale-permission-secret-canary")
    )
    real_replace = os.replace
    real_fstat = os.fstat
    publication_started = False
    permission_relaxed = False

    def publish_then_arm_permission_change(source, target):
        nonlocal publication_started
        real_replace(source, target)
        publication_started = True

    def initial_fstat_then_relax_before_final_fstat(descriptor):
        nonlocal permission_relaxed
        result = real_fstat(descriptor)
        if publication_started and not permission_relaxed:
            live = document.lstat()
            if (result.st_dev, result.st_ino) == (live.st_dev, live.st_ino):
                os.fchmod(descriptor, 0o640)
                permission_relaxed = True
        return result

    monkeypatch.setattr(
        "spica.config.document_transaction.os.replace",
        publish_then_arm_permission_change,
    )
    monkeypatch.setattr(
        "spica.config.document_transaction.os.fstat",
        initial_fstat_then_relax_before_final_fstat,
    )

    with pytest.raises(SensitiveEnvError) as failed:
        managed.commit(preview)

    assert failed.value.code == "PERMISSION_HARDENING_FAILED"
    assert document.read_bytes() == original
    assert stat.S_IMODE(document.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_failed_sensitive_recovery_never_chmods_a_swapped_symlink(
    tmp_path, monkeypatch
):
    document = tmp_path / "xiaosan.env"
    original = b"KEEP=original\n"
    document.write_bytes(original)
    document.chmod(0o600)
    outside = tmp_path / "outside-owner-file"
    outside.write_bytes(b"outside must remain untouched\n")
    outside.chmod(0o644)
    outside_mode = stat.S_IMODE(outside.stat().st_mode)
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
    )
    preview = managed.preview(
        SetSecret("openai_api_key", "symlink-recovery-secret-canary")
    )
    real_replace = os.replace
    real_path_chmod = Path.chmod
    replace_count = 0
    swap_attempted = False

    def first_replace_then_relax_permissions(source, target):
        nonlocal replace_count
        real_replace(source, target)
        replace_count += 1
        if replace_count == 1:
            os.chmod(target, 0o640)

    def swap_sensitive_path_before_chmod(path, mode):
        nonlocal swap_attempted
        if path == document and not swap_attempted:
            swap_attempted = True
            document.unlink()
            document.symlink_to(outside)
        return real_path_chmod(path, mode)

    monkeypatch.setattr(
        "spica.config.document_transaction.os.replace",
        first_replace_then_relax_permissions,
    )
    monkeypatch.setattr(Path, "chmod", swap_sensitive_path_before_chmod)

    with pytest.raises(SensitiveEnvError) as failed:
        managed.commit(preview)

    assert failed.value.code == "PERMISSION_HARDENING_FAILED"
    assert stat.S_IMODE(outside.stat().st_mode) == outside_mode
    assert outside.read_bytes() == b"outside must remain untouched\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_failed_secret_publication_does_not_enter_user_restore_history(
    tmp_path, monkeypatch
):
    document = tmp_path / "xiaosan.env"
    document.write_bytes(b"MODEL=repo\n")
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
    )
    seeded = managed.commit(managed.preview(ClearMappedOverride("MODEL")))
    live_before_failure = document.read_bytes()
    restore_points_before = managed.restore_points()
    assert [point.id for point in restore_points_before] == [
        seeded.restore_point_id
    ]

    failed_secret = "failed-publication-secret-canary"
    preview = managed.preview(SetSecret("openai_api_key", failed_secret))
    real_replace = os.replace
    replace_count = 0

    def first_replace_then_relax_permissions(source, target):
        nonlocal replace_count
        real_replace(source, target)
        replace_count += 1
        if replace_count == 1:
            os.chmod(target, 0o640)

    monkeypatch.setattr(
        "spica.config.document_transaction.os.replace",
        first_replace_then_relax_permissions,
    )

    with pytest.raises(SensitiveEnvError) as failed:
        managed.commit(preview)

    assert failed.value.code == "PERMISSION_HARDENING_FAILED"
    assert document.read_bytes() == live_before_failure
    assert (document.stat().st_mode & 0o777) == 0o600
    assert managed.restore_points() == restore_points_before
    assert failed_secret.encode("utf-8") not in document.read_bytes()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_sensitive_rollback_restores_current_and_fails_on_permission_interference(
    tmp_path,
    monkeypatch,
):
    document = tmp_path / "xiaosan.env"
    document.write_bytes(b"MODEL=repo\n")
    managed = _sensitive_document(
        document,
        backup_root=tmp_path / "backups",
        inherited_environment={},
        parent_environment={},
    )
    committed = managed.commit(managed.preview(ClearMappedOverride("MODEL")))
    current = document.read_bytes()
    confirmation = managed.prepare_rollback(
        committed.restore_point_id,
        session_id="session-a",
    )
    real_replace = os.replace

    def replace_then_relax_permissions(source, target):
        real_replace(source, target)
        os.chmod(target, 0o640)

    monkeypatch.setattr(
        "spica.config.document_transaction.os.replace",
        replace_then_relax_permissions,
    )

    with pytest.raises(SensitiveEnvError) as failed:
        managed.rollback(
            confirmation.receipt_token,
            session_id="session-a",
        )

    assert failed.value.code == "PERMISSION_HARDENING_FAILED"
    assert document.read_bytes() == current
    assert (document.stat().st_mode & 0o777) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_existing_sensitive_backup_root_must_already_be_owner_private(tmp_path):
    document = tmp_path / "xiaosan.env"
    original = b"MODEL=repo\n"
    document.write_bytes(original)
    backup_root = tmp_path / "backups"
    backup_root.mkdir(mode=0o775)
    backup_root.chmod(0o775)
    managed = _sensitive_document(
        document,
        backup_root=backup_root,
        inherited_environment={},
        parent_environment={},
    )
    preview = managed.preview(ClearMappedOverride("MODEL"))

    with pytest.raises(SensitiveEnvError) as unsafe:
        managed.commit(preview)

    assert unsafe.value.code == "SENSITIVE_BACKUP_UNSAFE"
    assert document.read_bytes() == original
    assert (backup_root.stat().st_mode & 0o777) == 0o775


def _values_for(document, name):
    from dotenv.parser import parse_stream

    with document.open("r", encoding="utf-8", newline="") as stream:
        return [
            binding.value
            for binding in parse_stream(stream)
            if binding.key == name and not binding.error
        ]
