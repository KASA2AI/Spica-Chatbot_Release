from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
import os

import pytest

from spica.adapters.config_studio.platform import platform_capabilities_for
from spica.config.environment_snapshot import EnvironmentSnapshot
from spica.config_studio.app_document import (
    AppConfigDocument,
    AppDocumentError,
    RuamelRoundTripEditor,
)
from spica.config_studio.authoring import SetValue
from spica.config_studio.authoring import UnsetValue
from spica.config_studio.paths import ConfigFieldPath, FieldSegment, MapKeySegment


@dataclass
class FixedEditor:
    expected_base: bytes
    candidate: bytes

    def apply(self, base: bytes, operations: tuple[SetValue, ...]) -> bytes:
        assert base == self.expected_base
        assert operations
        return self.candidate


def _document(
    tmp_path,
    *,
    base: bytes,
    candidate: bytes,
    environment=None,
    environment_snapshot_owner=None,
):
    path = tmp_path / "repo" / "data" / "config" / "app.yaml"
    path.parent.mkdir(parents=True)
    path.write_bytes(base)
    return AppConfigDocument(
        path,
        backup_root=tmp_path / "state" / "backups",
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            environment or {},
            layer="repo_dotenv",
        ),
        environment_snapshot_owner=environment_snapshot_owner,
        round_trip_editor=FixedEditor(base, candidate),
        platform_capabilities=platform_capabilities_for(
            os_family="posix",
            runtime_name="linux",
            user_id=os.getuid(),
            temp_directory=tmp_path / "platform-tmp",
        ),
        token_factory=lambda: "preview-token-opaque",
    ), path


def test_app_preview_validates_with_owner_and_commits_exact_candidate_bytes(tmp_path):
    base = b"# keep this comment\nllm:\n  model: old-model\n"
    candidate = b"# keep this comment\nllm:\n  model: new-model\n"
    document, path = _document(tmp_path, base=base, candidate=candidate)
    operation = SetValue(
        ConfigFieldPath.fields("llm", "model"),
        "new-model",
    )

    preview = document.preview((operation,))

    assert preview.preview_id == "preview-token-opaque"
    assert preview.changed is True
    assert preview.effect_policy == "next_spica_launch"
    assert len(preview.changes) == 1
    change = preview.changes[0]
    assert change.display_path == "llm.model"
    assert change.file_value_before == "old-model"
    assert change.file_value_after == "new-model"
    assert change.next_launch_value_before == "old-model"
    assert change.next_launch_value_after == "new-model"
    assert change.source_after == "file"
    assert change.file_value_shadowed is False
    assert path.read_bytes() == base

    committed = document.commit(preview.preview_id)

    assert path.read_bytes() == candidate
    assert committed.restore_point_id
    assert committed.effect_policy == "next_spica_launch"


def test_app_unset_can_remove_a_preexisting_forbidden_secret_value(tmp_path):
    secret_canary = "synthetic-preexisting-secret"
    base = f"llm:\n  model: {secret_canary}\n".encode("utf-8")
    candidate = b"llm: {}\n"
    document, path = _document(tmp_path, base=base, candidate=candidate)
    operation = UnsetValue(ConfigFieldPath.fields("llm", "model"))

    preview = document.preview(
        (operation,),
        forbidden_values=(secret_canary,),
    )
    committed = document.commit(
        preview.preview_id,
        forbidden_values=(secret_canary,),
    )

    assert committed.restore_point_id
    assert path.read_bytes() == candidate


def test_app_preview_rejects_secret_material_after_owner_type_coercion(tmp_path):
    secret_canary = "123"
    base = b"screen:\n  provider: 123\ntts:\n  enabled: true\n"
    candidate = b"screen:\n  provider: 123\ntts:\n  enabled: false\n"
    document, path = _document(tmp_path, base=base, candidate=candidate)

    with pytest.raises(AppDocumentError) as caught:
        document.preview(
            (SetValue(ConfigFieldPath.fields("tts", "enabled"), False),),
            forbidden_values=(secret_canary,),
        )

    assert caught.value.code == "DOCUMENT_INVALID"
    assert path.read_bytes() == base


def test_app_preview_reports_env_shadow_without_claiming_behavior_changed(tmp_path):
    base = b"llm:\n  model: old-model\n"
    candidate = b"llm:\n  model: new-model\n"
    document, _ = _document(
        tmp_path,
        base=base,
        candidate=candidate,
        environment={"MODEL": "winning-env-model"},
    )

    preview = document.preview(
        (SetValue(ConfigFieldPath.fields("llm", "model"), "new-model"),)
    )

    change = preview.changes[0]
    assert change.next_launch_value_before == "winning-env-model"
    assert change.next_launch_value_after == "winning-env-model"
    assert change.source_after == "env_override"
    assert change.file_value_shadowed is True
    assert change.semantic_warning == "APP_FILE_VALUE_SHADOWED_BY_ENV"


def test_app_commit_requires_a_new_preview_when_environment_snapshot_changes(
    tmp_path,
):
    base = b"llm:\n  model: old-model\n"
    candidate = b"llm:\n  model: new-model\n"
    current_environment = {
        "snapshot": EnvironmentSnapshot.from_mapping(
            {"MODEL": "first-env-model"},
            layer="repo_dotenv",
        )
    }
    document, path = _document(
        tmp_path,
        base=base,
        candidate=candidate,
        environment_snapshot_owner=lambda: current_environment["snapshot"],
    )
    preview = document.preview(
        (SetValue(ConfigFieldPath.fields("llm", "model"), "new-model"),)
    )
    assert preview.changes[0].next_launch_value_after == "first-env-model"
    current_environment["snapshot"] = EnvironmentSnapshot.from_mapping(
        {"MODEL": "changed-env-model"},
        layer="repo_dotenv",
    )

    with pytest.raises(AppDocumentError) as stale:
        document.commit(preview.preview_id)

    assert stale.value.code == "CONFIRMATION_REQUIRED"
    assert path.read_bytes() == base
    assert not (tmp_path / "state").exists()


def test_app_unset_preview_falls_back_to_owner_default_and_commits_removal(tmp_path):
    base = b"tts:\n  enabled: false\n"
    candidate = b"tts: {}\n"
    document, path = _document(tmp_path, base=base, candidate=candidate)

    preview = document.preview(
        (UnsetValue(ConfigFieldPath.fields("tts", "enabled")),)
    )

    change = preview.changes[0]
    assert change.file_value_before is False
    assert change.file_value_after is None
    assert change.next_launch_value_before is False
    assert change.next_launch_value_after is True
    assert change.source_after == "default"
    document.commit(preview.preview_id)
    assert path.read_bytes() == candidate


def test_app_commit_rechecks_revision_inside_transaction(tmp_path):
    base = b"llm:\n  model: old-model\n"
    candidate = b"llm:\n  model: new-model\n"
    document, path = _document(tmp_path, base=base, candidate=candidate)
    preview = document.preview(
        (SetValue(ConfigFieldPath.fields("llm", "model"), "new-model"),)
    )
    path.write_bytes(b"llm:\n  model: another-session\n")

    with pytest.raises(AppDocumentError) as caught:
        document.commit(preview.preview_id)

    assert caught.value.code == "DOCUMENT_CONFLICT"
    assert path.read_bytes() == b"llm:\n  model: another-session\n"


def test_app_preview_rejects_roundtrip_semantic_drift(tmp_path):
    base = b"tts:\n  enabled: true\n"
    candidate = b"tts:\n  enabled: 'false'\n"
    document, path = _document(tmp_path, base=base, candidate=candidate)

    with pytest.raises(AppDocumentError) as caught:
        document.preview(
            (SetValue(ConfigFieldPath.fields("tts", "enabled"), False),)
        )

    assert caught.value.code == "DOCUMENT_INVALID"
    assert path.read_bytes() == base


def test_damaged_app_document_enters_recovery_only_instead_of_resetting(tmp_path):
    base = b"llm: [unterminated\n"
    document, path = _document(tmp_path, base=base, candidate=b"llm: {}\n")

    status = document.status()

    assert status.recovery_only is True
    assert status.error_code == "RECOVERY_ONLY"
    assert status.manual_repair_code == "APP_YAML_MANUAL_REPAIR_REQUIRED"
    with pytest.raises(AppDocumentError) as caught:
        document.preview(
            (SetValue(ConfigFieldPath.fields("llm", "model"), "safe"),)
        )
    assert caught.value.code == "RECOVERY_ONLY"
    assert path.read_bytes() == base


def test_duplicate_yaml_keys_block_live_authoring_and_candidate_validation(tmp_path):
    duplicate_base = b"llm:\n  model: first\n  model: second\n"
    document, path = _document(
        tmp_path,
        base=duplicate_base,
        candidate=b"llm:\n  model: safe\n",
    )

    assert document.status().recovery_only is True
    with pytest.raises(AppDocumentError) as live_error:
        document.preview(
            (SetValue(ConfigFieldPath.fields("llm", "model"), "safe"),)
        )
    assert live_error.value.code == "RECOVERY_ONLY"
    assert path.read_bytes() == duplicate_base

    valid_base = b"llm:\n  model: old\n"
    duplicate_candidate = b"llm:\n  model: old\n  model: new\n"
    candidate_document, candidate_path = _document(
        tmp_path / "candidate",
        base=valid_base,
        candidate=duplicate_candidate,
    )
    with pytest.raises(AppDocumentError) as candidate_error:
        candidate_document.preview(
            (SetValue(ConfigFieldPath.fields("llm", "model"), "new"),)
        )
    assert candidate_error.value.code == "DOCUMENT_INVALID"
    assert candidate_path.read_bytes() == valid_base


def test_yaml_alias_graph_is_read_only_and_cannot_share_an_authoring_branch(tmp_path):
    base = b"""galgame:
  reaction_table:
    low: &shared-tier
      min_score: 4
      max_per_window: 3
      cooldown_seconds: 90.0
    normal: *shared-tier
"""
    candidate = base.replace(b"min_score: 4", b"min_score: 5")
    document, path = _document(tmp_path, base=base, candidate=candidate)
    operation = SetValue(
        ConfigFieldPath(
            (
                FieldSegment("galgame"),
                FieldSegment("reaction_table"),
                MapKeySegment("low"),
                FieldSegment("min_score"),
            )
        ),
        5,
    )

    assert document.status().recovery_only is True
    with pytest.raises(AppDocumentError) as caught:
        document.preview((operation,))

    assert caught.value.code == "RECOVERY_ONLY"
    assert path.read_bytes() == base


def test_app_preview_and_error_repr_do_not_expose_candidate_bytes(tmp_path):
    secretish = "canary-that-must-not-appear"
    base = b"llm:\n  model: old-model\n"
    candidate = f"llm:\n  model: {secretish}\n".encode()
    document, _ = _document(tmp_path, base=base, candidate=candidate)

    preview = document.preview(
        (SetValue(ConfigFieldPath.fields("llm", "model"), secretish),)
    )

    assert secretish not in repr(preview)
    assert "sha256" not in repr(preview)


def test_app_change_and_rollback_repr_do_not_expose_dynamic_field_names(
    tmp_path,
):
    secretish = "enabled"
    base = b"song:\n  enabled: true\n"
    candidate = b"song:\n  enabled: false\n"
    document, _ = _document(tmp_path, base=base, candidate=candidate)
    operation = SetValue(
        ConfigFieldPath(
            (FieldSegment("song"), MapKeySegment(secretish))
        ),
        False,
    )

    preview = document.preview((operation,))
    committed = document.commit(preview.preview_id)
    confirmation = document.prepare_rollback(
        committed.restore_point_id,
        session_id="synthetic-session",
    )

    assert secretish not in repr(preview.changes[0])
    assert secretish not in repr(confirmation.preview)
    assert secretish not in repr(confirmation)
    assert "sha256" not in repr(confirmation)


def test_app_preview_is_immutable_server_stored_and_bound_to_one_session(tmp_path):
    base = b"llm:\n  model: old-model\n"
    candidate = b"llm:\n  model: new-model\n"
    document, path = _document(tmp_path, base=base, candidate=candidate)
    preview = document.preview(
        (SetValue(ConfigFieldPath.fields("llm", "model"), "new-model"),),
        session_id="owner-session",
    )

    assert not hasattr(preview, "_candidate")
    with pytest.raises(FrozenInstanceError):
        preview.changed = False
    with pytest.raises(AppDocumentError) as wrong_session:
        document.commit(preview.preview_id, session_id="other-session")
    assert wrong_session.value.code == "CONFIRMATION_REQUIRED"

    document.commit(preview.preview_id, session_id="owner-session")

    assert path.read_bytes() == candidate


def test_app_rollback_requires_a_session_bound_one_time_semantic_confirmation(tmp_path):
    base = b"# original\nllm:\n  model: old-model\n"
    candidate = b"# updated\nllm:\n  model: new-model\n"
    document, path = _document(tmp_path, base=base, candidate=candidate)
    preview = document.preview(
        (SetValue(ConfigFieldPath.fields("llm", "model"), "new-model"),)
    )
    committed = document.commit(preview.preview_id)

    restore_points = document.restore_points()
    assert [point.id for point in restore_points] == [committed.restore_point_id]
    confirmation = document.prepare_rollback(
        committed.restore_point_id,
        session_id="browser-session",
    )

    assert confirmation.preview.changed_fields == ("llm.model",)
    assert confirmation.preview.effect_policy == "next_spica_launch"
    assert "old-model" not in repr(confirmation)
    assert "new-model" not in repr(confirmation)
    assert "sha256" not in repr(confirmation)

    with pytest.raises(AppDocumentError) as wrong_session:
        document.rollback(
            confirmation.receipt_token,
            session_id="different-session",
        )
    assert wrong_session.value.code == "CONFIRMATION_REQUIRED"

    rolled_back = document.rollback(
        confirmation.receipt_token,
        session_id="browser-session",
    )
    assert path.read_bytes() == base
    assert rolled_back.restore_point_id

    with pytest.raises(AppDocumentError) as reused:
        document.rollback(
            confirmation.receipt_token,
            session_id="browser-session",
        )
    assert reused.value.code == "CONFIRMATION_REQUIRED"


def test_app_rollback_receipt_rechecks_current_document_revision(tmp_path):
    base = b"llm:\n  model: old-model\n"
    candidate = b"llm:\n  model: new-model\n"
    document, path = _document(tmp_path, base=base, candidate=candidate)
    committed = document.commit(
        document.preview(
            (SetValue(ConfigFieldPath.fields("llm", "model"), "new-model"),)
        ).preview_id
    )
    confirmation = document.prepare_rollback(
        committed.restore_point_id,
        session_id="browser-session",
    )
    path.write_bytes(b"llm:\n  model: another-session\n")

    with pytest.raises(AppDocumentError) as caught:
        document.rollback(
            confirmation.receipt_token,
            session_id="browser-session",
        )

    assert caught.value.code == "DOCUMENT_CONFLICT"
    assert path.read_bytes() == b"llm:\n  model: another-session\n"


def test_recovery_only_mode_can_rollback_corrupt_live_yaml_to_a_valid_restore_point(
    tmp_path,
):
    base = b"# valid restore\nllm:\n  model: old-model\n"
    candidate = b"llm:\n  model: new-model\n"
    document, path = _document(tmp_path, base=base, candidate=candidate)
    committed = document.commit(
        document.preview(
            (SetValue(ConfigFieldPath.fields("llm", "model"), "new-model"),)
        ).preview_id
    )
    corrupt = b"llm: [unterminated\n"
    path.write_bytes(corrupt)

    confirmation = document.prepare_rollback(
        committed.restore_point_id,
        session_id="recovery-session",
    )

    assert confirmation.preview.resolution_error_before is True
    assert confirmation.preview.resolution_error_after is False
    assert confirmation.preview.changed_fields == ("<recovery-only-document>",)
    assert path.read_bytes() == corrupt

    document.rollback(
        confirmation.receipt_token,
        session_id="recovery-session",
    )

    assert path.read_bytes() == base


def test_app_rollback_rejects_binary_yaml_containing_a_known_secret(tmp_path):
    secret_canary = "synthetic-binary-secret"
    base = b"llm:\n  model: !!binary c3ludGhldGljLWJpbmFyeS1zZWNyZXQ=\n"
    candidate = b"llm:\n  model: safe-model\n"
    document, path = _document(tmp_path, base=base, candidate=candidate)
    committed = document.commit(
        document.preview(
            (SetValue(ConfigFieldPath.fields("llm", "model"), "safe-model"),)
        ).preview_id
    )

    with pytest.raises(AppDocumentError) as caught:
        document.prepare_rollback(
            committed.restore_point_id,
            session_id="browser-session",
            forbidden_values=(secret_canary,),
        )

    assert caught.value.code == "DOCUMENT_INVALID"
    assert path.read_bytes() == candidate


def test_ruamel_editor_preserves_comments_and_production_yaml_types_when_available():
    pytest.importorskip("ruamel.yaml")
    editor = RuamelRoundTripEditor()
    base = b"# owner note\ntts:\n  enabled: true  # keep inline\n"

    candidate = editor.apply(
        base,
        (SetValue(ConfigFieldPath.fields("tts", "enabled"), False),),
    )

    assert b"# owner note" in candidate
    assert b"# keep inline" in candidate
    assert b"enabled: false" in candidate


def test_ruamel_editor_unset_preserves_unrelated_comments_when_available():
    pytest.importorskip("ruamel.yaml")
    editor = RuamelRoundTripEditor()
    base = (
        b"# owner note\n"
        b"tts:\n"
        b"  enabled: false\n"
        b"  future_owner_key: keep  # keep unrelated\n"
    )

    candidate = editor.apply(
        base,
        (UnsetValue(ConfigFieldPath.fields("tts", "enabled")),),
    )

    assert b"# owner note" in candidate
    assert b"# keep unrelated" in candidate
    assert b"future_owner_key: keep" in candidate
    assert b"enabled:" not in candidate
