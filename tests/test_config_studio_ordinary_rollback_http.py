from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest

from spica.adapters.config_studio.platform import platform_capabilities_for
from spica.config.environment_snapshot import EnvironmentSnapshot
from spica.config_studio.api import create_config_studio_app
from spica.config_studio.app_document import AppConfigDocument
from spica.config_studio.authoring import AuthoringOperation
from spica.config_studio.security import SecurityContext
from spica.config_studio.services import OwnerBackedConfigStudioServices
from ui.config_studio.overlay_document import OverlayConfigDocument


_BOOTSTRAP_TOKEN = "ordinary-rollback-bootstrap-token"


class _FakeRollbackServices:
    def __init__(self, *capabilities: str) -> None:
        self.capabilities = frozenset(capabilities)
        self.app_list_sessions: list[str] = []
        self.app_prepare_requests: list[tuple[str, str]] = []
        self.app_rollback_requests: list[tuple[str, str]] = []
        self.overlay_list_sessions: list[str] = []
        self.overlay_prepare_requests: list[tuple[str, str]] = []
        self.overlay_rollback_requests: list[tuple[str, str]] = []

    def meta(self) -> dict[str, Any]:
        return {"mode": "sandbox"}

    def catalog(self) -> dict[str, Any]:
        return {"fields": []}

    def capability_enabled(self, capability: str) -> bool:
        return capability in self.capabilities

    def self_check_jobs_available(self) -> bool:
        return False

    def list_app_restore_points(self, *, session_id: str) -> list[dict[str, Any]]:
        self.app_list_sessions.append(session_id)
        return [
            {
                "restore_point_id": "A" * 24,
                "created_at_ns": 123,
                "sha256": "must-never-cross-the-api",
                "size": 999,
                "path": "/must/never/cross/the/api",
                "content": "must-never-cross-the-api",
            }
        ]

    def prepare_app_rollback(
        self,
        restore_point_id: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        self.app_prepare_requests.append((restore_point_id, session_id))
        return {
            "confirmation_receipt": "app-rollback-receipt-opaque",
            "restore_point_id": restore_point_id,
            "effect_policy": "next_spica_launch",
            "changed_fields": ["tts.enabled"],
            "next_launch_changed_fields": ["tts.enabled"],
            "unmanaged_content_changed": True,
            "unmanaged_change_count": 1,
            "resolution_error_before": False,
            "resolution_error_after": False,
            "raw_document": "must-never-cross-the-api",
            "path": "/must/never/cross/the/api",
        }

    def rollback_app(
        self,
        confirmation_receipt: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        self.app_rollback_requests.append((confirmation_receipt, session_id))
        return {
            "status": "restored",
            "effect_policy": "next_spica_launch",
            "restore_point_id": "B" * 24,
            "maintenance_code": None,
            "content": "must-never-cross-the-api",
        }

    def list_overlay_restore_points(
        self,
        *,
        session_id: str,
    ) -> list[dict[str, Any]]:
        self.overlay_list_sessions.append(session_id)
        return [
            {
                "restore_point_id": "O" * 24,
                "created_at_ns": 456,
                "hash": "must-never-cross-the-api",
                "length": 999,
                "path": "/must/never/cross/the/api",
            }
        ]

    def prepare_overlay_rollback(
        self,
        restore_point_id: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        self.overlay_prepare_requests.append((restore_point_id, session_id))
        return {
            "confirmation_receipt": "overlay-rollback-receipt-opaque",
            "restore_point_id": restore_point_id,
            "effect_policy": "next_spica_launch",
            "changed_fields": ["spica_voice_volume"],
            "unmanaged_content_changed": False,
            "unmanaged_change_count": 0,
            "resolution_error_before": False,
            "resolution_error_after": False,
            "raw_document": "must-never-cross-the-api",
        }

    def rollback_overlay(
        self,
        confirmation_receipt: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        self.overlay_rollback_requests.append((confirmation_receipt, session_id))
        return {
            "status": "restored",
            "effect_policy": "next_spica_launch",
            "restore_point_id": "P" * 24,
            "maintenance_code": None,
            "content": "must-never-cross-the-api",
        }


def _security_context() -> SecurityContext:
    generated = iter(
        ("ordinary-session-token-opaque", "ordinary-csrf-token-opaque")
    )
    return SecurityContext(
        host="127.0.0.1",
        port=8765,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        clock=lambda: 100.0,
        token_factory=lambda: next(generated),
        bootstrap_ttl_seconds=30.0,
    )


def _write_headers(client: TestClient) -> dict[str, str]:
    bootstrap = client.post(
        "/api/v1/session/bootstrap",
        headers={
            "Origin": "http://127.0.0.1:8765",
            "X-Spica-Bootstrap": _BOOTSTRAP_TOKEN,
        },
    )
    bootstrap.raise_for_status()
    return {
        "Origin": "http://127.0.0.1:8765",
        "X-Spica-CSRF": bootstrap.json()["csrf_token"],
    }


class _FixedAppEditor:
    def __init__(self, candidate: bytes) -> None:
        self.candidate = candidate

    def apply(
        self,
        base: bytes,
        operations: tuple[AuthoringOperation, ...],
    ) -> bytes:
        assert base
        assert operations
        return self.candidate


def _platform(tmp_path: Path):
    return platform_capabilities_for(
        os_family="posix",
        runtime_name="linux",
        user_id=os.getuid(),
        temp_directory=tmp_path / "platform-tmp",
    )


def test_app_rollback_api_returns_only_bounded_semantics_and_opaque_ids() -> None:
    services = _FakeRollbackServices("app_config_write", "rollback")
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _write_headers(client)
        points = client.get("/api/v1/app/restore-points")
        prepared = client.post(
            "/api/v1/app/restore-points/" + "A" * 24 + "/prepare-rollback",
            headers=headers,
        )
        restored = client.post(
            "/api/v1/app/rollbacks",
            headers=headers,
            json={"confirmation_receipt": "app-rollback-receipt-opaque"},
        )

    assert points.status_code == 200
    assert points.json() == {
        "restore_points": [
            {"restore_point_id": "A" * 24, "created_at_ns": 123}
        ]
    }
    assert prepared.status_code == 200
    assert prepared.json() == {
        "confirmation_receipt": "app-rollback-receipt-opaque",
        "restore_point_id": "A" * 24,
        "effect_policy": "next_spica_launch",
        "changed_fields": ["tts.enabled"],
        "next_launch_changed_fields": ["tts.enabled"],
        "unmanaged_content_changed": True,
        "unmanaged_change_count": 1,
        "resolution_error_before": False,
        "resolution_error_after": False,
        "truncation": {
            "truncated": False,
            "changed_fields_omitted": 0,
            "next_launch_changed_fields_omitted": 0,
        },
    }
    assert restored.status_code == 200
    assert restored.json() == {
        "status": "restored",
        "effect_policy": "next_spica_launch",
        "restore_point_id": "B" * 24,
        "maintenance_code": None,
    }
    session_id = services.app_list_sessions[0]
    assert services.app_prepare_requests == [("A" * 24, session_id)]
    assert services.app_rollback_requests == [
        ("app-rollback-receipt-opaque", session_id)
    ]
    for response in (points, prepared, restored):
        assert "must-never-cross-the-api" not in response.text
        assert "/must/never/cross/the/api" not in response.text


def test_overlay_rollback_api_returns_only_bounded_semantics_and_opaque_ids() -> None:
    services = _FakeRollbackServices("overlay_write", "rollback")
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _write_headers(client)
        points = client.get("/api/v1/overlay/restore-points")
        prepared = client.post(
            "/api/v1/overlay/restore-points/" + "O" * 24 + "/prepare-rollback",
            headers=headers,
        )
        restored = client.post(
            "/api/v1/overlay/rollbacks",
            headers=headers,
            json={"confirmation_receipt": "overlay-rollback-receipt-opaque"},
        )

    assert points.status_code == 200
    assert points.json() == {
        "restore_points": [
            {"restore_point_id": "O" * 24, "created_at_ns": 456}
        ]
    }
    assert prepared.status_code == 200
    assert prepared.json() == {
        "confirmation_receipt": "overlay-rollback-receipt-opaque",
        "restore_point_id": "O" * 24,
        "effect_policy": "next_spica_launch",
        "changed_fields": ["spica_voice_volume"],
        "unmanaged_content_changed": False,
        "unmanaged_change_count": 0,
        "resolution_error_before": False,
        "resolution_error_after": False,
        "truncation": {
            "truncated": False,
            "changed_fields_omitted": 0,
        },
    }
    assert restored.status_code == 200
    assert restored.json() == {
        "status": "restored",
        "effect_policy": "next_spica_launch",
        "restore_point_id": "P" * 24,
        "maintenance_code": None,
    }
    session_id = services.overlay_list_sessions[0]
    assert services.overlay_prepare_requests == [("O" * 24, session_id)]
    assert services.overlay_rollback_requests == [
        ("overlay-rollback-receipt-opaque", session_id)
    ]
    for response in (points, prepared, restored):
        assert "must-never-cross-the-api" not in response.text
        assert "/must/never/cross/the/api" not in response.text


def test_real_app_owner_rollback_is_session_bound_one_shot_and_needs_no_sensitive_owner(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    original = b"max_tool_rounds: 2\n"
    app_path.write_bytes(original)
    environment = EnvironmentSnapshot.from_mapping(
        {},
        layer="synthetic_inherited",
    )
    owner = AppConfigDocument(
        app_path,
        backup_root=tmp_path / "sandbox-state" / "backups",
        environment_snapshot=environment,
        round_trip_editor=_FixedAppEditor(b"max_tool_rounds: 3\n"),
        platform_capabilities=_platform(tmp_path),
        token_factory=iter(
            (
                "real-app-preview-token-opaque",
                "real-app-rollback-receipt-opaque",
            )
        ).__next__,
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=environment,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        app_document=owner,
        enabled_write_capabilities=frozenset({"app_config_write", "rollback"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _write_headers(client)
        preview = client.post(
            "/api/v1/app/previews",
            headers=headers,
            json={
                "operations": [
                    {
                        "kind": "set",
                        "path": [
                            {"kind": "field", "name": "max_tool_rounds"}
                        ],
                        "value": 3,
                    }
                ]
            },
        )
        preview.raise_for_status()
        committed = client.post(
            "/api/v1/app/commits",
            headers=headers,
            json={"preview_id": preview.json()["preview_id"]},
        )
        committed.raise_for_status()
        restore_point_id = committed.json()["restore_point_id"]
        assert app_path.read_bytes() == b"max_tool_rounds: 3\n"

        points = client.get("/api/v1/app/restore-points")
        prepared = client.post(
            f"/api/v1/app/restore-points/{restore_point_id}/prepare-rollback",
            headers=headers,
        )
        restored = client.post(
            "/api/v1/app/rollbacks",
            headers=headers,
            json={
                "confirmation_receipt": prepared.json()["confirmation_receipt"]
            },
        )
        reused = client.post(
            "/api/v1/app/rollbacks",
            headers=headers,
            json={
                "confirmation_receipt": prepared.json()["confirmation_receipt"]
            },
        )

    assert services.capability_enabled("rollback") is True
    assert len(points.json()["restore_points"]) == 1
    listed_point = points.json()["restore_points"][0]
    assert set(listed_point) == {"restore_point_id", "created_at_ns"}
    assert listed_point["restore_point_id"] == restore_point_id
    assert type(listed_point["created_at_ns"]) is int
    assert listed_point["created_at_ns"] >= 0
    assert prepared.status_code == 200
    assert prepared.json()["changed_fields"] == ["max_tool_rounds"]
    assert prepared.json()["next_launch_changed_fields"] == ["max_tool_rounds"]
    assert restored.status_code == 200
    assert restored.json()["status"] == "restored"
    assert app_path.read_bytes() == original
    assert reused.status_code == 409
    assert reused.json() == {"error": {"code": "CONFIRMATION_REQUIRED"}}
    for response in (points, prepared, restored, reused):
        assert str(app_path) not in response.text
        assert "max_tool_rounds: 2" not in response.text


def test_app_rollback_is_blocked_when_a_retired_legacy_owner_reappears(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    original = b"plugins:\n  - name: alpha\n    enabled: false\n"
    app_path.write_bytes(original)
    environment = EnvironmentSnapshot.from_mapping({}, layer="synthetic_inherited")
    owner = AppConfigDocument(
        app_path,
        backup_root=tmp_path / "sandbox-state" / "backups",
        environment_snapshot=environment,
        round_trip_editor=_FixedAppEditor(b"plugins: []\n"),
        platform_capabilities=_platform(tmp_path),
        token_factory=iter(
            (
                "legacy-app-preview-token-opaque",
                "legacy-app-rollback-receipt-opaque",
            )
        ).__next__,
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=environment,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        app_document=owner,
        enabled_write_capabilities=frozenset({"app_config_write", "rollback"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _write_headers(client)
        preview = client.post(
            "/api/v1/app/previews",
            headers=headers,
            json={
                "operations": [
                    {
                        "kind": "set",
                        "path": [{"kind": "field", "name": "plugins"}],
                        "value": [],
                    }
                ]
            },
        )
        preview.raise_for_status()
        committed = client.post(
            "/api/v1/app/commits",
            headers=headers,
            json={"preview_id": preview.json()["preview_id"]},
        )
        committed.raise_for_status()
        restore_point_id = committed.json()["restore_point_id"]
        (repo_root / "data" / "config" / "plugins.yaml").write_bytes(b"{}\n")
        prepared = client.post(
            f"/api/v1/app/restore-points/{restore_point_id}/prepare-rollback",
            headers=headers,
        )

    assert prepared.status_code == 409
    assert prepared.json() == {"error": {"code": "DOCUMENT_UNSAFE"}}
    assert app_path.read_bytes() == b"plugins: []\n"


def test_real_overlay_owner_rollback_is_one_shot_and_needs_no_sensitive_owner(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_bytes(b"max_tool_rounds: 2\n")
    overlay_path = repo_root / "ui" / "overlay_config.json"
    overlay_path.parent.mkdir(parents=True)
    original = b'{"spica_voice_volume": 0.5, "future_ui_key": true}\n'
    overlay_path.write_bytes(original)
    environment = EnvironmentSnapshot.from_mapping(
        {},
        layer="synthetic_inherited",
    )
    owner = OverlayConfigDocument(
        overlay_path,
        backup_root=tmp_path / "sandbox-state" / "backups",
        platform_capabilities=_platform(tmp_path),
        token_factory=iter(
            (
                "real-overlay-preview-token-opaque",
                "real-overlay-rollback-receipt-opaque",
            )
        ).__next__,
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=environment,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        overlay_document=owner,
        enabled_write_capabilities=frozenset({"overlay_write", "rollback"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _write_headers(client)
        preview = client.post(
            "/api/v1/overlay/previews",
            headers=headers,
            json={"key": "spica_voice_volume", "value": 0.8},
        )
        preview.raise_for_status()
        committed = client.post(
            "/api/v1/overlay/commits",
            headers=headers,
            json={"preview_id": preview.json()["preview_id"]},
        )
        committed.raise_for_status()
        restore_point_id = committed.json()["restore_point_id"]
        assert json.loads(overlay_path.read_text(encoding="utf-8")) == {
            "spica_voice_volume": 0.8,
            "future_ui_key": True,
        }

        points = client.get("/api/v1/overlay/restore-points")
        prepared = client.post(
            f"/api/v1/overlay/restore-points/{restore_point_id}/prepare-rollback",
            headers=headers,
        )
        restored = client.post(
            "/api/v1/overlay/rollbacks",
            headers=headers,
            json={
                "confirmation_receipt": prepared.json()["confirmation_receipt"]
            },
        )
        reused = client.post(
            "/api/v1/overlay/rollbacks",
            headers=headers,
            json={
                "confirmation_receipt": prepared.json()["confirmation_receipt"]
            },
        )

    assert services.capability_enabled("rollback") is True
    assert points.status_code == 200
    assert points.json()["restore_points"][0]["restore_point_id"] == restore_point_id
    assert prepared.status_code == 200
    assert prepared.json()["changed_fields"] == ["spica_voice_volume"]
    assert prepared.json()["unmanaged_content_changed"] is False
    assert restored.status_code == 200
    assert restored.json()["status"] == "restored"
    assert overlay_path.read_bytes() == original
    assert reused.status_code == 409
    assert reused.json() == {"error": {"code": "CONFIRMATION_REQUIRED"}}
    for response in (points, prepared, restored, reused):
        assert str(overlay_path) not in response.text
        assert "future_ui_key" not in response.text


@pytest.mark.parametrize(
    ("write_capability", "list_path", "prepare_path", "prepare_log_name"),
    [
        (
            "app_config_write",
            "/api/v1/app/restore-points",
            "/api/v1/app/restore-points/" + "A" * 24 + "/prepare-rollback",
            "app_prepare_requests",
        ),
        (
            "overlay_write",
            "/api/v1/overlay/restore-points",
            "/api/v1/overlay/restore-points/" + "O" * 24 + "/prepare-rollback",
            "overlay_prepare_requests",
        ),
    ],
)
def test_ordinary_rollback_routes_require_session_origin_csrf_and_both_capabilities(
    write_capability: str,
    list_path: str,
    prepare_path: str,
    prepare_log_name: str,
) -> None:
    disabled = _FakeRollbackServices(write_capability)
    disabled_app = create_config_studio_app(disabled, _security_context())
    with TestClient(
        disabled_app,
        base_url="http://127.0.0.1:8765",
    ) as client:
        disabled_headers = _write_headers(client)
        capability_gated = client.get(list_path)
        gated_prepare = client.post(prepare_path, headers=disabled_headers)

    assert capability_gated.status_code == 403
    assert gated_prepare.status_code == 403
    assert getattr(disabled, prepare_log_name) == []

    enabled = _FakeRollbackServices(write_capability, "rollback")
    enabled_app = create_config_studio_app(enabled, _security_context())
    with TestClient(
        enabled_app,
        base_url="http://127.0.0.1:8765",
    ) as client:
        session_required = client.get(list_path)
        headers = _write_headers(client)
        missing_csrf = client.post(
            prepare_path,
            headers={"Origin": "http://127.0.0.1:8765"},
        )
        wrong_origin = client.post(
            prepare_path,
            headers={**headers, "Origin": "http://localhost:8765"},
        )
        invalid_id = client.post(
            prepare_path.replace("A" * 24, "bad.id").replace("O" * 24, "bad.id"),
            headers=headers,
        )

    assert session_required.status_code == 401
    assert session_required.json() == {"error": {"code": "SESSION_REQUIRED"}}
    assert missing_csrf.status_code == 403
    assert missing_csrf.json() == {"error": {"code": "CSRF_INVALID"}}
    assert wrong_origin.status_code == 403
    assert wrong_origin.json() == {"error": {"code": "ORIGIN_REJECTED"}}
    assert invalid_id.status_code == 400
    assert invalid_id.json() == {"error": {"code": "RESTORE_POINT_INVALID"}}
    assert getattr(enabled, prepare_log_name) == []


def test_real_app_rollback_conflict_returns_a_stable_error_without_overwrite(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_bytes(b"max_tool_rounds: 2\n")
    environment = EnvironmentSnapshot.from_mapping(
        {},
        layer="synthetic_inherited",
    )
    owner = AppConfigDocument(
        app_path,
        backup_root=tmp_path / "sandbox-state" / "backups",
        environment_snapshot=environment,
        round_trip_editor=_FixedAppEditor(b"max_tool_rounds: 3\n"),
        platform_capabilities=_platform(tmp_path),
        token_factory=iter(
            (
                "conflict-app-preview-token-opaque",
                "conflict-app-rollback-receipt-opaque",
            )
        ).__next__,
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=environment,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        app_document=owner,
        enabled_write_capabilities=frozenset({"app_config_write", "rollback"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _write_headers(client)
        preview = client.post(
            "/api/v1/app/previews",
            headers=headers,
            json={
                "operations": [
                    {
                        "kind": "set",
                        "path": [
                            {"kind": "field", "name": "max_tool_rounds"}
                        ],
                        "value": 3,
                    }
                ]
            },
        )
        committed = client.post(
            "/api/v1/app/commits",
            headers=headers,
            json={"preview_id": preview.json()["preview_id"]},
        )
        restore_point_id = committed.json()["restore_point_id"]
        prepared = client.post(
            f"/api/v1/app/restore-points/{restore_point_id}/prepare-rollback",
            headers=headers,
        )
        external_bytes = b"max_tool_rounds: 4\n"
        app_path.write_bytes(external_bytes)
        conflicted = client.post(
            "/api/v1/app/rollbacks",
            headers=headers,
            json={
                "confirmation_receipt": prepared.json()["confirmation_receipt"]
            },
        )

    assert conflicted.status_code == 409
    assert conflicted.json() == {"error": {"code": "DOCUMENT_CONFLICT"}}
    assert app_path.read_bytes() == external_bytes
    assert str(app_path) not in conflicted.text
    assert "max_tool_rounds: 4" not in conflicted.text


class _OversizedRollbackPreviewServices(_FakeRollbackServices):
    def prepare_app_rollback(
        self,
        restore_point_id: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        preview = super().prepare_app_rollback(
            restore_point_id,
            session_id=session_id,
        )
        preview["changed_fields"] = [f"field_{index}" for index in range(257)]
        return preview

    def prepare_overlay_rollback(
        self,
        restore_point_id: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        preview = super().prepare_overlay_rollback(
            restore_point_id,
            session_id=session_id,
        )
        preview["changed_fields"] = [f"overlay_field_{index}" for index in range(257)]
        return preview


def test_ordinary_rollback_preview_budget_returns_stable_truncation_metadata() -> None:
    services = _OversizedRollbackPreviewServices(
        "app_config_write",
        "overlay_write",
        "rollback",
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _write_headers(client)
        app_response = client.post(
            "/api/v1/app/restore-points/" + "A" * 24 + "/prepare-rollback",
            headers=headers,
        )
        overlay_response = client.post(
            "/api/v1/overlay/restore-points/"
            + "O" * 24
            + "/prepare-rollback",
            headers=headers,
        )

    assert app_response.status_code == 200
    assert len(app_response.json()["changed_fields"]) == 128
    assert app_response.json()["truncation"] == {
        "truncated": True,
        "changed_fields_omitted": 129,
        "next_launch_changed_fields_omitted": 0,
    }
    assert "field_256" not in app_response.text
    assert overlay_response.status_code == 200
    assert len(overlay_response.json()["changed_fields"]) == 128
    assert overlay_response.json()["truncation"] == {
        "truncated": True,
        "changed_fields_omitted": 129,
    }
    assert "overlay_field_256" not in overlay_response.text
