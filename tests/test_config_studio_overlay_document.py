from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
import os

import pytest

from spica.adapters.config_studio.platform import platform_capabilities_for
from spica.config_studio.overlay_document import (
    OverlayConfigDocument,
    OverlayDocumentError,
    OverlaySetValue,
)


def _document(tmp_path, initial: bytes) -> tuple[OverlayConfigDocument, object]:
    path = tmp_path / "repo" / "ui" / "overlay_config.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(initial)
    return (
        OverlayConfigDocument(
            path,
            backup_root=tmp_path / "state" / "backups",
            platform_capabilities=platform_capabilities_for(
                os_family="posix",
                runtime_name="linux",
                user_id=os.getuid(),
                temp_directory=tmp_path / "platform-tmp",
            ),
            token_factory=iter(
                ("preview-token-opaque", "rollback-receipt-opaque")
            ).__next__,
        ),
        path,
    )


def test_overlay_preview_is_typed_server_stored_and_session_bound(tmp_path):
    document, path = _document(
        tmp_path,
        b'{"spica_voice_volume": 0.5, "future_ui_key": {"keep": true}}\n',
    )

    preview = document.preview(
        OverlaySetValue("spica_voice_volume", 0.72),
        session_id="owner-session",
    )

    assert preview.preview_id == "preview-token-opaque"
    assert preview.key == "spica_voice_volume"
    assert preview.file_value_before == 0.5
    assert preview.file_value_after == 0.72
    assert preview.effect_policy == "next_spica_launch"
    assert not hasattr(preview, "_candidate")
    with pytest.raises(FrozenInstanceError):
        preview.changed = False
    with pytest.raises(OverlayDocumentError) as wrong_session:
        document.commit(preview.preview_id, session_id="other-session")
    assert wrong_session.value.code == "CONFIRMATION_REQUIRED"

    committed = document.commit(
        preview.preview_id,
        session_id="owner-session",
    )

    assert committed.restore_point_id
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "spica_voice_volume": 0.72,
        "future_ui_key": {"keep": True},
    }


@pytest.mark.parametrize(
    ("key", "value", "code"),
    [
        ("arbitrary", 1.0, "UNKNOWN_FIELD"),
        ("spica_voice_volume", True, "TYPE_MISMATCH"),
        ("spica_voice_volume", "0.5", "TYPE_MISMATCH"),
        ("spica_voice_volume", 1.1, "VALUE_OUT_OF_RANGE"),
    ],
)
def test_overlay_preview_rejects_unknown_coerced_or_out_of_range_values(
    tmp_path, key, value, code
):
    document, path = _document(tmp_path, b'{"spica_voice_volume": 0.5}\n')

    with pytest.raises(OverlayDocumentError) as rejected:
        document.preview(OverlaySetValue(key, value), session_id="session")

    assert rejected.value.code == code
    assert path.read_bytes() == b'{"spica_voice_volume": 0.5}\n'


def test_overlay_commit_rechecks_live_revision_and_consumes_preview(tmp_path):
    document, path = _document(tmp_path, b'{"spica_voice_volume": 0.5}\n')
    preview = document.preview(
        OverlaySetValue("spica_voice_volume", 0.7),
        session_id="session",
    )
    path.write_bytes(b'{"spica_voice_volume": 0.9}\n')

    with pytest.raises(OverlayDocumentError) as conflict:
        document.commit(preview.preview_id, session_id="session")

    assert conflict.value.code == "DOCUMENT_CONFLICT"
    assert path.read_bytes() == b'{"spica_voice_volume": 0.9}\n'
    with pytest.raises(OverlayDocumentError) as consumed:
        document.commit(preview.preview_id, session_id="session")
    assert consumed.value.code == "CONFIRMATION_REQUIRED"


@pytest.mark.parametrize(
    "invalid_json",
    [
        b'{"spica_voice_volume": 0.4, "spica_voice_volume": 0.6}\n',
        b'{"spica_voice_volume": NaN}\n',
    ],
)
def test_overlay_duplicate_keys_and_nonfinite_json_enter_recovery_only(
    tmp_path, invalid_json
):
    document, path = _document(tmp_path, invalid_json)

    with pytest.raises(OverlayDocumentError) as invalid:
        document.preview(
            OverlaySetValue("spica_voice_volume", 0.7),
            session_id="session",
        )

    assert invalid.value.code == "RECOVERY_ONLY"
    assert path.read_bytes() == invalid_json


def test_overlay_whole_document_rollback_requires_semantic_one_time_receipt(tmp_path):
    document, path = _document(tmp_path, b'{"spica_voice_volume": 0.5}\n')
    preview = document.preview(
        OverlaySetValue("spica_voice_volume", 0.8),
        session_id="session",
    )
    committed = document.commit(preview.preview_id, session_id="session")

    confirmation = document.prepare_rollback(
        committed.restore_point_id,
        session_id="session",
    )

    assert confirmation.preview.changed_fields == ("spica_voice_volume",)
    assert "0.5" not in repr(confirmation)
    document.rollback(confirmation.receipt_token, session_id="session")
    assert json.loads(path.read_text(encoding="utf-8"))["spica_voice_volume"] == 0.5

    with pytest.raises(OverlayDocumentError) as reused:
        document.rollback(confirmation.receipt_token, session_id="session")
    assert reused.value.code == "CONFIRMATION_REQUIRED"
