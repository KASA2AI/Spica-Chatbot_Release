from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import socket
import threading
import time
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import uvicorn

from spica.adapters.config_studio.platform import platform_capabilities_for
from spica.config_studio.api import OverlaySetValueRequest, create_config_studio_app
from spica.config.environment_snapshot import EnvironmentSnapshot
from spica.config.document_transaction import ManagedDocumentTransaction
from spica.config.secrets import LoadedSecrets, Secrets, load_secrets
from spica.config_studio.app_document import AppConfigDocument
from spica.config_studio.authoring import SetValue, UnsetValue
from spica.config_studio.overlay_contract import OverlayOwnerError
from spica.config_studio.paths import ConfigFieldPath, FieldSegment
from spica.config_studio.security import SecurityContext
from spica.config_studio.services import (
    ConfigStudioServiceError,
    OwnerBackedConfigStudioServices,
    ReadOnlyConfigStudioServices,
)
from spica.config_studio.sensitive_env import (
    ClearMappedOverride,
    ClearSecret,
    SensitiveEnvDocument,
    SetSecret,
)
from spica.config_studio.self_check import (
    LIGHT_CHECKS,
    SelfCheckJobError,
    SelfCheckJobManager,
    SelfCheckMode,
    SelfCheckPlanError,
    SelfCheckProcessOutcome,
    SelfCheckStderrSummary,
)
from spica.config_studio.self_check_service import (
    SelfCheckAcknowledgements,
    SelfCheckEnvironmentInputs,
    SelfCheckService,
)
from spica.config_studio.server import LoopbackServer
from spica.config_studio.overlay_document import OverlayConfigDocument


def _platform(tmp_path: Path):
    return platform_capabilities_for(
        os_family="posix",
        runtime_name="linux",
        user_id=os.getuid(),
        temp_directory=tmp_path / "platform-tmp",
    )


class _FakeServices:
    def __init__(
        self,
        *,
        app_write_enabled: bool = False,
        overlay_write_enabled: bool = False,
        sensitive_write_enabled: bool = False,
        rollback_enabled: bool = False,
        self_check_enabled: bool = True,
        self_check_jobs_enabled: bool | None = None,
    ) -> None:
        self.app_write_enabled = app_write_enabled
        self.overlay_write_enabled = overlay_write_enabled
        self.sensitive_write_enabled = sensitive_write_enabled
        self.rollback_enabled = rollback_enabled
        self.self_check_enabled = self_check_enabled
        self.self_check_jobs_enabled = (
            self_check_enabled
            if self_check_jobs_enabled is None
            else self_check_jobs_enabled
        )
        self.app_preview_requests: list[tuple[tuple[SetValue, ...], str]] = []
        self.app_commit_requests: list[tuple[str, str]] = []
        self.overlay_preview_requests: list[tuple[OverlaySetValueRequest, str]] = []
        self.overlay_commit_requests: list[tuple[str, str]] = []
        self.sensitive_status_sessions: list[str] = []
        self.sensitive_preview_requests: list[tuple[object, str]] = []
        self.sensitive_confirm_requests: list[tuple[str, str]] = []
        self.sensitive_commit_requests: list[tuple[str, str | None, str]] = []
        self.sensitive_restore_list_sessions: list[str] = []
        self.sensitive_rollback_prepare_requests: list[tuple[str, str]] = []
        self.sensitive_rollback_requests: list[tuple[str, str]] = []
        self.self_check_requests: list[Any] = []
        self.self_check_confirmation_requests: list[
            tuple[Any, SelfCheckAcknowledgements, str]
        ] = []
        self.self_check_confirmed_requests: list[tuple[Any, str, str]] = []
        self.self_check_jobs: dict[str, dict[str, Any]] = {}

    def meta(self) -> dict[str, Any]:
        return {"status": "sandbox"}

    def catalog(self) -> dict[str, Any]:
        return {"fields": [{"path": "tts.enabled"}]}

    def capability_enabled(self, capability: str) -> bool:
        return (
            capability == "app_config_write" and self.app_write_enabled
        ) or (
            capability == "overlay_write" and self.overlay_write_enabled
        ) or (
            capability == "sensitive_write" and self.sensitive_write_enabled
        ) or (
            capability == "rollback" and self.rollback_enabled
        ) or (
            capability == "self_check" and self.self_check_enabled
        )

    def self_check_jobs_available(self) -> bool:
        return self.self_check_jobs_enabled

    def preview_app(
        self,
        operations: tuple[SetValue, ...],
        *,
        session_id: str,
    ) -> dict[str, Any]:
        self.app_preview_requests.append((operations, session_id))
        return {
            "preview_id": "app_preview_opaque",
            "changed": True,
            "effect_policy": "next_spica_launch",
            "changes": [
                {
                    "path": [
                        {"kind": "field", "name": "tts"},
                        {"kind": "field", "name": "enabled"},
                    ],
                    "display_path": "tts.enabled",
                    "file_value_before": True,
                    "file_value_after": False,
                    "next_launch_value_before": True,
                    "next_launch_value_after": False,
                    "source_before": "file",
                    "source_after": "file",
                    "file_value_shadowed": False,
                    "semantic_warning": None,
                    "raw_candidate": "must-never-cross-the-api",
                }
            ],
            "raw_document": "must-never-cross-the-api",
            "diff": "must-never-cross-the-api",
        }

    def commit_app_preview(
        self,
        preview_id: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        self.app_commit_requests.append((preview_id, session_id))
        return {
            "status": "saved",
            "effect_policy": "next_spica_launch",
            "restore_point_id": "R" * 24,
            "maintenance_code": None,
            "raw_document": "must-never-cross-the-api",
        }

    def preview_overlay(
        self,
        command: OverlaySetValueRequest,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        self.overlay_preview_requests.append((command, session_id))
        return {
            "preview_id": "overlay_preview_opaque",
            "key": command.key,
            "file_value_before": 0.5,
            "file_value_after": command.value,
            "changed": True,
            "effect_policy": "next_spica_launch",
            "raw_document": "must-never-cross-the-api",
        }

    def commit_overlay_preview(
        self,
        preview_id: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        self.overlay_commit_requests.append((preview_id, session_id))
        return {
            "status": "saved",
            "effect_policy": "next_spica_launch",
            "restore_point_id": "O" * 24,
            "maintenance_code": None,
            "raw_document": "must-never-cross-the-api",
        }

    def sensitive_status(self, *, session_id: str) -> dict[str, Any]:
        self.sensitive_status_sessions.append(session_id)
        return {
            "secret_slots": [
                {
                    "slot": "openai_api_key",
                    "configured": True,
                    "value": "must-never-cross-the-api",
                }
            ],
            "permission_health": "PRIVATE",
            "raw_document": "must-never-cross-the-api",
        }

    def preview_sensitive(
        self,
        command: object,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        self.sensitive_preview_requests.append((command, session_id))
        kind = command.__class__.__name__
        command_kind = {
            "SetSecret": "set_secret",
            "ClearSecret": "clear_secret",
            "ClearMappedOverride": "clear_mapped_override",
        }[kind]
        return {
            "preview_id": "sensitive_preview_opaque",
            "command_kind": command_kind,
            "target": getattr(command, "slot", None)
            or getattr(command, "environment_variable", None),
            "affected_fields": (
                ["stream.max_retries"]
                if command_kind == "clear_mapped_override"
                else []
            ),
            "before_next_launch": 4,
            "after_next_launch": 2,
            "winning_source_before": "repo_dotenv",
            "winning_source_after": "file",
            "still_shadowed": False,
            "permission_hardening": True,
            "changed": True,
            "secret_change": (
                "will_clear" if command_kind == "clear_secret" else "will_set"
            ) if command_kind != "clear_mapped_override" else None,
            "resolution_error_before": False,
            "resolution_error_after": False,
            "raw_document": "must-never-cross-the-api",
            "file_diff": "must-never-cross-the-api",
            "secret_value": getattr(command, "value", None),
        }

    def confirm_sensitive_secret_clear(
        self,
        preview_id: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        self.sensitive_confirm_requests.append((preview_id, session_id))
        return {
            "confirmation_receipt": "clear_receipt_opaque",
            "preview_id": preview_id,
            "command_kind": "clear_secret",
            "target": "openai_api_key",
            "secret_change": "will_clear",
            "raw_document": "must-never-cross-the-api",
        }

    def commit_sensitive_preview(
        self,
        preview_id: str,
        confirmation_receipt: str | None,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        self.sensitive_commit_requests.append(
            (preview_id, confirmation_receipt, session_id)
        )
        return {
            "status": "saved",
            "restore_point_id": "S" * 24,
            "permission_health": "PRIVATE",
            "maintenance_code": None,
            "raw_document": "must-never-cross-the-api",
        }

    def list_sensitive_restore_points(
        self,
        *,
        session_id: str,
    ) -> list[dict[str, Any]]:
        self.sensitive_restore_list_sessions.append(session_id)
        return [
            {
                "restore_point_id": "S" * 24,
                "created_at_ns": 123,
                "sha256": "must-never-cross-the-api",
                "size": 999,
                "content": "must-never-cross-the-api",
            }
        ]

    def prepare_sensitive_rollback(
        self,
        restore_point_id: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        self.sensitive_rollback_prepare_requests.append(
            (restore_point_id, session_id)
        )
        return {
            "confirmation_receipt": "rollback_receipt_opaque",
            "restore_point_id": restore_point_id,
            "secret_changes": [
                {"slot": "openai_api_key", "change": "will_replace"}
            ],
            "override_changes": [],
            "unmanaged_content_changed": True,
            "unmanaged_change_count": 1,
            "permission_hardening": False,
            "resolution_error_before": False,
            "resolution_error_after": False,
            "raw_document": "must-never-cross-the-api",
        }

    def rollback_sensitive(
        self,
        confirmation_receipt: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        self.sensitive_rollback_requests.append(
            (confirmation_receipt, session_id)
        )
        return {
            "status": "restored",
            "restore_point_id": "T" * 24,
            "permission_health": "PRIVATE",
            "maintenance_code": None,
            "content": "must-never-cross-the-api",
        }

    def start_self_check(self, command: Any) -> dict[str, Any]:
        self.self_check_requests.append(command)
        job = {
            "job_id": "job_light",
            "mode": command.mode,
            "checks": list(command.only) or ["config", "gpu", "secrets"],
            "status": "QUEUED",
            "duration_s": 0.0,
            "results": [],
            "progress": [],
            "error_code": None,
            "stderr_line_count": 0,
            "stderr_total_line_count": 0,
            "stderr_truncated": False,
            "raw_output": "must-never-cross-the-api",
        }
        self.self_check_jobs[job["job_id"]] = job
        return job

    def prepare_heavy_self_check(
        self,
        command: Any,
        *,
        acknowledgements: SelfCheckAcknowledgements,
        session_id: str,
    ) -> dict[str, Any]:
        self.self_check_confirmation_requests.append(
            (command, acknowledgements, session_id)
        )
        return {
            "confirmation_receipt": "self_check_receipt_opaque",
            "expires_in_s": 120.0,
            "semantic": {
                "mode": "full",
                "checks": list(command.only) or [
                    "tts",
                    "stt",
                    "moondream",
                    "ocr",
                    "song_uvr",
                    "song_rvc",
                    "llm",
                ],
                "llm": command.llm,
                "include_disabled": command.include_disabled,
                "allow_model_downloads": command.allow_model_downloads,
                "argv": ["must-never-cross-the-api"],
            },
            "raw_plan": "must-never-cross-the-api",
        }

    def start_confirmed_self_check(
        self,
        command: Any,
        *,
        session_id: str,
        confirmation_receipt: str,
    ) -> dict[str, Any]:
        self.self_check_confirmed_requests.append(
            (command, session_id, confirmation_receipt)
        )
        return self.start_self_check(command)

    def list_self_checks(self) -> list[dict[str, Any]]:
        return list(self.self_check_jobs.values())

    def get_self_check(self, job_id: str) -> dict[str, Any]:
        return self.self_check_jobs[job_id]

    def cancel_self_check(self, job_id: str) -> dict[str, Any]:
        job = dict(self.self_check_jobs[job_id])
        job["status"] = "CANCELLED"
        self.self_check_jobs[job_id] = job
        return job


def _security_context(
    *,
    now: list[float] | None = None,
    tokens: list[str] | None = None,
) -> SecurityContext:
    current_time = now if now is not None else [100.0]
    generated_tokens = iter(tokens or ["session-token", "csrf-token"])
    return SecurityContext(
        host="127.0.0.1",
        port=8765,
        bootstrap_token="one-time-bootstrap-token",
        clock=lambda: current_time[0],
        token_factory=lambda: next(generated_tokens),
        bootstrap_ttl_seconds=30.0,
    )


class _FixedAppEditor:
    def __init__(self, candidate: bytes) -> None:
        self._candidate = candidate

    def apply(self, base: bytes, operations: tuple[SetValue, ...]) -> bytes:
        assert base
        assert operations
        return self._candidate


def _real_app_http_services(
    tmp_path: Path,
) -> tuple[OwnerBackedConfigStudioServices, AppConfigDocument, Path]:
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_bytes(b"max_tool_rounds: 2\n")
    app_document = AppConfigDocument(
        app_path,
        backup_root=tmp_path / "sandbox-state" / "backups",
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {},
            layer="synthetic_inherited",
        ),
        round_trip_editor=_FixedAppEditor(b"max_tool_rounds: 3\n"),
        token_factory=lambda: "real-owner-preview-token",
        platform_capabilities=_platform(tmp_path),
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {},
            layer="synthetic_inherited",
        ),
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        app_document=app_document,
        enabled_write_capabilities=frozenset({"app_config_write"}),
    )
    return services, app_document, app_path


def _real_overlay_http_services(
    tmp_path: Path,
    *,
    secret_canary: str | None = None,
) -> tuple[OwnerBackedConfigStudioServices, Path]:
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_bytes(b"max_tool_rounds: 2\n")
    overlay_path = repo_root / "ui" / "overlay_config.json"
    overlay_path.parent.mkdir(parents=True)
    overlay_path.write_bytes(b'{"spica_voice_volume": 0.5}\n')
    overlay_document = OverlayConfigDocument(
        overlay_path,
        backup_root=tmp_path / "sandbox-state" / "backups",
        token_factory=lambda: "real-overlay-preview-token",
        platform_capabilities=_platform(tmp_path),
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {},
            layer="synthetic_inherited",
        ),
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        secrets=Secrets(openai_api_key=secret_canary),
        overlay_document=overlay_document,
        enabled_write_capabilities=frozenset({"overlay_write"}),
    )
    return services, overlay_path


def _real_sensitive_http_services(
    tmp_path: Path,
    *,
    enable_writes: bool = True,
    repo_env_content: bytes = b"OPENAI_API_KEY=synthetic-repo-secret\n",
    base_document: dict[str, Any] | None = None,
) -> tuple[OwnerBackedConfigStudioServices, Path]:
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_bytes(b"max_tool_rounds: 2\n")
    sensitive_path = repo_root / "xiaosan.env"
    sensitive_path.write_bytes(repo_env_content)
    sensitive_path.chmod(0o600)
    parent_path = tmp_path / "sandbox-parent" / "xiaosan.env"
    parent_path.parent.mkdir(parents=True)
    parent_path.write_bytes(b"")
    parent_path.chmod(0o600)
    environment_owner = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={},
        repo_env_path=sensitive_path,
        parent_env_path=parent_path,
        prime_process=False,
    )
    assert isinstance(environment_owner, LoadedSecrets)
    sensitive_document = SensitiveEnvDocument(
        sensitive_path,
        backup_root=tmp_path / "sandbox-state" / "sensitive-backups",
        environment_owner=environment_owner,
        base_document=base_document,
        receipt_factory=lambda: "real-sensitive-receipt-token",
        preview_factory=lambda: "real-sensitive-preview-token",
        platform_capabilities=_platform(tmp_path),
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=environment_owner.environment_snapshot,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        secrets=environment_owner.secrets,
        sensitive_document=sensitive_document,
        environment_owner=environment_owner.refresh,
        enabled_write_capabilities=(
            frozenset({"sensitive_write", "rollback"})
            if enable_writes
            else frozenset()
        ),
    )
    return services, sensitive_path


def _bootstrap_write_headers(client: TestClient) -> dict[str, str]:
    bootstrap = client.post(
        "/api/v1/session/bootstrap",
        headers={
            "Origin": "http://127.0.0.1:8765",
            "X-Spica-Bootstrap": "one-time-bootstrap-token",
        },
    )
    bootstrap.raise_for_status()
    return {
        "Origin": "http://127.0.0.1:8765",
        "X-Spica-CSRF": bootstrap.json()["csrf_token"],
    }


def _preview_real_app(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post(
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
    response.raise_for_status()
    return response.json()["preview_id"]


def test_loopback_server_prebinds_an_ephemeral_test_port() -> None:
    with LoopbackServer.bind(port=0, allow_test_port_zero=True) as server:
        assert server.host == "127.0.0.1"
        assert 0 < server.port <= 65535
        assert server.socket.getsockname() == ("127.0.0.1", server.port)

        contender = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(OSError):
                contender.bind((server.host, server.port))
        finally:
            contender.close()


@pytest.mark.parametrize(
    ("host", "port", "allow_test_port_zero"),
    [
        ("0.0.0.0", 8765, False),
        ("localhost", 8765, False),
        ("127.0.0.1", 0, False),
        ("127.0.0.1", 1023, False),
        ("127.0.0.1", 65536, False),
    ],
)
def test_loopback_server_rejects_nonproduction_bind_targets(
    host: str,
    port: int,
    allow_test_port_zero: bool,
) -> None:
    with pytest.raises(ValueError):
        LoopbackServer.bind(
            host=host,
            port=port,
            allow_test_port_zero=allow_test_port_zero,
        )


def test_uvicorn_config_disables_proxy_and_identifying_headers() -> None:
    app = FastAPI()
    with LoopbackServer.bind(port=0, allow_test_port_zero=True) as server:
        config = server.uvicorn_config(app)

    assert config.proxy_headers is False
    assert config.access_log is False
    assert config.server_header is False
    assert config.date_header is False


def test_bootstrap_is_one_time_and_issues_a_hardened_loopback_session() -> None:
    app = create_config_studio_app(_FakeServices(), _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        replay = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"csrf_token": "csrf-token", "clear_fragment": True}
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Secure" not in cookie
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["permissions-policy"] == (
        "camera=(), display-capture=(), geolocation=(), microphone=(), "
        "payment=(), usb=()"
    )
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert "form-action 'none'" in response.headers["content-security-policy"]
    assert "form-action 'self'" not in response.headers["content-security-policy"]
    assert replay.status_code == 401
    assert replay.json() == {"error": {"code": "BOOTSTRAP_INVALID"}}


def test_bootstrap_token_expires_at_its_short_ttl_deadline() -> None:
    now = [100.0]
    app = create_config_studio_app(_FakeServices(), _security_context(now=now))
    now[0] = 130.0

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )

    assert response.status_code == 401
    assert response.json() == {"error": {"code": "BOOTSTRAP_INVALID"}}


def test_bootstrap_grant_has_a_minimum_high_entropy_shape_and_bounded_attempts() -> None:
    with pytest.raises(ValueError, match="bootstrap token"):
        SecurityContext(
            host="127.0.0.1",
            port=8765,
            bootstrap_token="six12",
        )

    token = "high-entropy-bootstrap-token-opaque"
    context = SecurityContext(
        host="127.0.0.1",
        port=8765,
        bootstrap_token=token,
        token_factory=iter(
            ["session-token-opaque", "csrf-token-opaque"]
        ).__next__,
        max_bootstrap_attempts=3,
    )

    assert context.bootstrap_is_pending() is True
    assert context.exchange_bootstrap("wrong-bootstrap-token-1") is None
    assert context.exchange_bootstrap("wrong-bootstrap-token-2") is None
    assert context.exchange_bootstrap("wrong-bootstrap-token-3") is None
    assert context.bootstrap_is_pending() is False
    assert context.exchange_bootstrap(token) is None


def test_authenticated_page_reload_recovers_the_existing_csrf_token() -> None:
    services = _FakeServices(app_write_enabled=True)
    app = create_config_studio_app(
        services,
        _security_context(tokens=["session-token", "initial-csrf"]),
    )

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        recovered = client.get("/api/v1/session/csrf")
        recovered_token_write = client.post(
            "/api/v1/app/previews",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-CSRF": bootstrap.json()["csrf_token"],
            },
            json={
                "operations": [
                    {
                        "kind": "set",
                        "path": [{"kind": "field", "name": "max_tool_rounds"}],
                        "value": 3,
                    }
                ]
            },
        )

    assert recovered.status_code == 200
    assert recovered.json() == {"csrf_token": "initial-csrf"}
    assert recovered.headers["cache-control"] == "no-store"
    assert recovered_token_write.status_code == 200


@pytest.mark.parametrize(
    "cookie_header",
    [
        "spica_config_studio_session=wrong-session",
        (
            "spica_config_studio_session=session-token; "
            "spica_config_studio_session=session-token"
        ),
    ],
)
def test_csrf_recovery_rejects_invalid_or_ambiguous_session_cookie(
    cookie_header: str,
) -> None:
    app = create_config_studio_app(
        _FakeServices(),
        _security_context(tokens=["session-token", "initial-csrf", "reload-csrf"]),
    )

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        ).raise_for_status()
        response = client.get(
            "/api/v1/session/csrf",
            headers={"Cookie": cookie_header},
        )

    assert response.status_code == 401
    assert response.json() == {"error": {"code": "SESSION_REQUIRED"}}


def test_every_nonbootstrap_api_requires_a_session() -> None:
    app = create_config_studio_app(_FakeServices(), _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        meta = client.get("/api/v1/meta")
        catalog = client.get("/api/v1/catalog")
        unknown = client.get("/api/v1/not-a-route")

    for response in (meta, catalog, unknown):
        assert response.status_code == 401
        assert response.json() == {"error": {"code": "SESSION_REQUIRED"}}


def test_authenticated_session_reads_injected_meta_and_catalog_services() -> None:
    app = create_config_studio_app(_FakeServices(), _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        ).raise_for_status()
        meta = client.get("/api/v1/meta")
        catalog = client.get("/api/v1/catalog")

    assert meta.status_code == 200
    assert meta.json() == {"status": "sandbox"}
    assert catalog.status_code == 200
    assert catalog.json() == {"fields": [{"path": "tts.enabled"}]}


def test_environment_refresh_failure_has_a_stable_service_unavailable_response() -> None:
    class UnavailableEnvironmentServices(_FakeServices):
        def catalog(self) -> dict[str, Any]:
            raise ConfigStudioServiceError("ENVIRONMENT_REFRESH_UNAVAILABLE")

    app = create_config_studio_app(
        UnavailableEnvironmentServices(),
        _security_context(),
    )

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        ).raise_for_status()
        response = client.get("/api/v1/catalog")

    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "ENVIRONMENT_REFRESH_UNAVAILABLE"}
    }


def test_catalog_redacts_a_repo_secret_shadowed_by_inherited_environment(
    tmp_path: Path,
) -> None:
    shadowed_secret = "shadowed-repo-secret-canary"
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_text(
        f"llm:\n  model: {shadowed_secret}\n",
        encoding="utf-8",
    )
    repo_env = repo_root / "xiaosan.env"
    repo_env.write_text(
        f"OPENAI_API_KEY={shadowed_secret}\n",
        encoding="utf-8",
    )
    repo_env.chmod(0o600)
    parent_env = tmp_path / "sandbox-parent" / "xiaosan.env"
    parent_env.parent.mkdir()
    parent_env.write_bytes(b"")
    parent_env.chmod(0o600)
    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={
            "OPENAI_API_KEY": "winning-inherited-secret"
        },
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )
    assert isinstance(loaded, LoadedSecrets)
    services = ReadOnlyConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=loaded.environment_snapshot,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        secrets=loaded.secrets,
        legacy_secret_canaries=loaded.legacy_secret_canaries,
        environment_owner=lambda: loaded,
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        ).raise_for_status()
        response = client.get("/api/v1/catalog")

    response.raise_for_status()
    model = next(
        field
        for field in response.json()["fields"]
        if field["display_path"] == "llm.model"
    )
    assert model["file_value"] == "«REDACTED:OPENAI_API_KEY»"
    assert model["next_launch_value"] == "«REDACTED:OPENAI_API_KEY»"
    assert shadowed_secret not in response.text


def test_catalog_redacts_integer_one_matching_owner_secret_material(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_bytes(b"max_tool_rounds: 1\n")
    repo_env = repo_root / "xiaosan.env"
    repo_env.write_bytes(b"OPENAI_API_KEY=1\n")
    repo_env.chmod(0o600)
    parent_env = tmp_path / "sandbox-parent" / "xiaosan.env"
    parent_env.parent.mkdir()
    parent_env.write_bytes(b"")
    parent_env.chmod(0o600)
    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={},
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )
    assert isinstance(loaded, LoadedSecrets)
    services = ReadOnlyConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=loaded.environment_snapshot,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        secrets=loaded.secrets,
        environment_owner=lambda: loaded,
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        ).raise_for_status()
        response = client.get("/api/v1/catalog")

    response.raise_for_status()
    max_tool_rounds = next(
        field
        for field in response.json()["fields"]
        if field["display_path"] == "max_tool_rounds"
    )
    assert max_tool_rounds["file_value"] == "«REDACTED:OPENAI_API_KEY»"
    assert max_tool_rounds["next_launch_value"] == "«REDACTED:OPENAI_API_KEY»"
    assert max_tool_rounds["authoring_complete"] is False
    assert '"file_value":1' not in response.text


def test_catalog_redacts_boolean_secret_data_without_rewriting_schema_metadata(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_bytes(b"tts:\n  enabled: false\n")
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {}, layer="synthetic"
        ),
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        secrets=Secrets(openai_api_key="false"),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        ).raise_for_status()
        response = client.get("/api/v1/catalog")

    response.raise_for_status()
    body = response.json()
    enabled = next(
        field
        for field in body["fields"]
        if field["display_path"] == "tts.enabled"
    )
    assert enabled["file_value"] == "«REDACTED:OPENAI_API_KEY»"
    assert enabled["next_launch_value"] == "«REDACTED:OPENAI_API_KEY»"
    assert enabled["default_value"] is True
    assert enabled["editable"] is True
    assert enabled["authoring_complete"] is False
    assert body["fields_complete"] is True
    assert body["recovery_only"] is False


def test_catalog_redacts_plugin_boolean_data_without_rewriting_status_metadata(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_text(
        "plugins:\n  - name: sample\n    enabled: false\n",
        encoding="utf-8",
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {}, layer="synthetic"
        ),
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        secrets=Secrets(openai_api_key="false"),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        ).raise_for_status()
        response = client.get("/api/v1/catalog")

    response.raise_for_status()
    plugin = response.json()["plugin_statuses"][0]
    assert plugin["next_launch_enabled"] == "«REDACTED:OPENAI_API_KEY»"
    assert plugin["configured"] is True
    assert plugin["package_status"] == "missing"


def test_app_preview_rejects_a_repo_secret_shadowed_by_inherited_environment(
    tmp_path: Path,
) -> None:
    shadowed_secret = "shadowed-repo-candidate-secret"
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    original = b"llm:\n  model: safe-model\n"
    app_path.write_bytes(original)
    repo_env = repo_root / "xiaosan.env"
    repo_env.write_text(
        f"OPENAI_API_KEY={shadowed_secret}\n",
        encoding="utf-8",
    )
    repo_env.chmod(0o600)
    parent_env = tmp_path / "sandbox-parent" / "xiaosan.env"
    parent_env.parent.mkdir()
    parent_env.write_bytes(b"")
    parent_env.chmod(0o600)
    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={
            "OPENAI_API_KEY": "winning-inherited-secret"
        },
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )
    assert isinstance(loaded, LoadedSecrets)
    app_document = AppConfigDocument(
        app_path,
        backup_root=tmp_path / "sandbox-state" / "backups",
        environment_snapshot=loaded.environment_snapshot,
        round_trip_editor=_FixedAppEditor(
            f"llm:\n  model: {shadowed_secret}\n".encode()
        ),
        platform_capabilities=_platform(tmp_path),
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=loaded.environment_snapshot,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        secrets=loaded.secrets,
        legacy_secret_canaries=loaded.legacy_secret_canaries,
        app_document=app_document,
        environment_owner=lambda: loaded,
        enabled_write_capabilities=frozenset({"app_config_write"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/v1/app/previews",
            headers=_bootstrap_write_headers(client),
            json={
                "operations": [
                    {
                        "kind": "set",
                        "path": [
                            {"kind": "field", "name": "llm"},
                            {"kind": "field", "name": "model"},
                        ],
                        "value": shadowed_secret,
                    }
                ]
            },
        )

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "DOCUMENT_INVALID"}}
    assert shadowed_secret not in response.text
    assert app_path.read_bytes() == original


def test_app_unset_preview_redacts_shadowed_numeric_secret_before_value(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    original = b"screen:\n  provider: 123\n"
    app_path.write_bytes(original)
    repo_env = repo_root / "xiaosan.env"
    repo_env.write_bytes(b"OPENAI_API_KEY=123\n")
    repo_env.chmod(0o600)
    parent_env = tmp_path / "sandbox-parent" / "xiaosan.env"
    parent_env.parent.mkdir()
    parent_env.write_bytes(b"")
    parent_env.chmod(0o600)
    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={"OPENAI_API_KEY": "winning-secret"},
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )
    assert isinstance(loaded, LoadedSecrets)
    app_document = AppConfigDocument(
        app_path,
        backup_root=tmp_path / "sandbox-state" / "backups",
        environment_snapshot=loaded.environment_snapshot,
        round_trip_editor=_FixedAppEditor(b"screen: {}\n"),
        platform_capabilities=_platform(tmp_path),
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=loaded.environment_snapshot,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        secrets=loaded.secrets,
        app_document=app_document,
        environment_owner=lambda: loaded,
        enabled_write_capabilities=frozenset({"app_config_write"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/v1/app/previews",
            headers=_bootstrap_write_headers(client),
            json={
                "operations": [
                    {
                        "kind": "unset",
                        "path": [
                            {"kind": "field", "name": "screen"},
                            {"kind": "field", "name": "provider"},
                        ],
                    }
                ]
            },
        )

    response.raise_for_status()
    change = response.json()["changes"][0]
    assert change["file_value_before"] == "«REDACTED:OPENAI_API_KEY»"
    assert change["next_launch_value_before"] == "«REDACTED:OPENAI_API_KEY»"
    assert '"file_value_before":123' not in response.text
    assert app_path.read_bytes() == original


def test_real_catalog_redaction_preserves_fixed_path_segment_kinds(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_bytes(b"max_tool_rounds: 2\n")
    environment = EnvironmentSnapshot.from_mapping({}, layer="synthetic")
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=environment,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        secrets=Secrets(
            openai_api_key="field",
            judge_api_key="enabled",
            bilibili_cookie="type",
            qbittorrent_password="array",
        ),
        legacy_secret_canaries=(("LEGACY_SECRET", "auto"),),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        ).raise_for_status()
        response = client.get("/api/v1/catalog")

    assert response.status_code == 200
    segments = [
        segment
        for field in response.json()["fields"]
        for segment in field["path"]
    ]
    assert segments
    assert {segment["kind"] for segment in segments} <= {
        "field",
        "map_key",
        "list_index",
    }
    assert any(segment["kind"] == "field" for segment in segments)
    assert any(
        segment == {
            "kind": "map_key",
            "key": "«REDACTED:JUDGE_API_KEY»",
        }
        for segment in segments
    )
    assert any(
        "«REDACTED:JUDGE_API_KEY»" in field["display_path"]
        for field in response.json()["fields"]
    )
    plugins = next(
        field
        for field in response.json()["fields"]
        if field["display_path"] == "plugins"
    )
    assert plugins["authoring_complete"] is True
    assert plugins["structured_schema"]["type"] == "array"
    assert plugins["structured_schema"]["items"]["type"] == "object"
    mic_backend = next(
        field
        for field in response.json()["fields"]
        if field["display_path"] == "stt.mic_backend"
    )
    assert mic_backend["literal_choices"] == ["auto", "respeaker", "generic"]


def test_real_catalog_closes_set_when_an_editable_value_is_secret_redacted(
    tmp_path: Path,
) -> None:
    secret_canary = "synthetic-structured-secret"
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_text(
        "anime:\n"
        "  bilibili_spaces:\n"
        "    - alpha\n"
        f"    - {secret_canary}\n"
        "    - omega\n",
        encoding="utf-8",
    )
    environment = EnvironmentSnapshot.from_mapping({}, layer="synthetic")
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=environment,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        secrets=Secrets(openai_api_key=secret_canary),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        ).raise_for_status()
        response = client.get("/api/v1/catalog")

    assert response.status_code == 200
    field = next(
        item
        for item in response.json()["fields"]
        if item["display_path"] == "anime.bilibili_spaces"
    )
    assert field["file_value"] == [
        "alpha",
        "«REDACTED:OPENAI_API_KEY»",
        "omega",
    ]
    assert field["next_launch_value"] == field["file_value"]
    assert field["authoring_complete"] is False
    assert secret_canary not in response.text


def test_write_capability_is_enforced_by_the_api_boundary() -> None:
    services = _FakeServices(app_write_enabled=False)
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        response = client.post(
            "/api/v1/app/previews",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-CSRF": bootstrap.json()["csrf_token"],
            },
            json={
                "operations": [
                    {
                        "kind": "set",
                        "path": [
                            {"kind": "field", "name": "tts"},
                            {"kind": "field", "name": "enabled"},
                        ],
                        "value": False,
                    }
                ]
            },
        )

    assert response.status_code == 403
    assert response.json() == {"error": {"code": "CAPABILITY_UNAVAILABLE"}}
    assert services.app_preview_requests == []


def test_windows_app_write_denial_preserves_platform_capability_reason(
    tmp_path,
) -> None:
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_bytes(b"tts:\n  enabled: true\n")
    services = ReadOnlyConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {},
            layer="synthetic_inherited",
        ),
        background_health_code=None,
        platform_capabilities=platform_capabilities_for(
            os_family="nt",
            runtime_name="win32",
            user_id=None,
            temp_directory=tmp_path / "platform-tmp",
        ),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        response = client.post(
            "/api/v1/app/previews",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-CSRF": bootstrap.json()["csrf_token"],
            },
            json={
                "operations": [
                    {
                        "kind": "set",
                        "path": [
                            {"kind": "field", "name": "tts"},
                            {"kind": "field", "name": "enabled"},
                        ],
                        "value": False,
                    }
                ]
            },
        )

    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "WRITES_UNVERIFIED_ON_WINDOWS"}
    }


@pytest.mark.parametrize(
    ("method", "path", "expected_code"),
    (
        ("POST", "/api/v1/app/commits", "WRITES_UNVERIFIED_ON_WINDOWS"),
        ("GET", "/api/v1/app/restore-points", "WRITES_UNVERIFIED_ON_WINDOWS"),
        (
            "POST",
            "/api/v1/app/restore-points/opaque-id/prepare-rollback",
            "WRITES_UNVERIFIED_ON_WINDOWS",
        ),
        ("POST", "/api/v1/app/rollbacks", "WRITES_UNVERIFIED_ON_WINDOWS"),
        ("POST", "/api/v1/overlay/previews", "WRITES_UNVERIFIED_ON_WINDOWS"),
        ("POST", "/api/v1/overlay/commits", "WRITES_UNVERIFIED_ON_WINDOWS"),
        (
            "GET",
            "/api/v1/overlay/restore-points",
            "WRITES_UNVERIFIED_ON_WINDOWS",
        ),
        (
            "POST",
            "/api/v1/overlay/restore-points/opaque-id/prepare-rollback",
            "WRITES_UNVERIFIED_ON_WINDOWS",
        ),
        ("POST", "/api/v1/overlay/rollbacks", "WRITES_UNVERIFIED_ON_WINDOWS"),
        (
            "POST",
            "/api/v1/sensitive/previews",
            "SENSITIVE_WRITES_UNVERIFIED_ON_WINDOWS",
        ),
        (
            "POST",
            "/api/v1/sensitive/previews/opaque-id/confirm-clear",
            "SENSITIVE_WRITES_UNVERIFIED_ON_WINDOWS",
        ),
        (
            "POST",
            "/api/v1/sensitive/commits",
            "SENSITIVE_WRITES_UNVERIFIED_ON_WINDOWS",
        ),
        (
            "GET",
            "/api/v1/sensitive/restore-points",
            "SENSITIVE_WRITES_UNVERIFIED_ON_WINDOWS",
        ),
        (
            "POST",
            "/api/v1/sensitive/restore-points/opaque-id/prepare-rollback",
            "SENSITIVE_WRITES_UNVERIFIED_ON_WINDOWS",
        ),
        (
            "POST",
            "/api/v1/sensitive/rollbacks",
            "SENSITIVE_WRITES_UNVERIFIED_ON_WINDOWS",
        ),
    ),
)
def test_windows_write_routes_preserve_platform_capability_reason(
    tmp_path,
    method,
    path,
    expected_code,
) -> None:
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_bytes(b"tts:\n  enabled: true\n")
    services = ReadOnlyConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {},
            layer="synthetic_inherited",
        ),
        background_health_code=None,
        platform_capabilities=platform_capabilities_for(
            os_family="nt",
            runtime_name="win32",
            user_id=None,
            temp_directory=tmp_path / "platform-tmp",
        ),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        response = client.request(
            method,
            path,
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-CSRF": bootstrap.json()["csrf_token"],
            },
            json={} if method == "POST" else None,
        )

    assert response.status_code == 503
    assert response.json() == {"error": {"code": expected_code}}


@pytest.mark.parametrize(
    ("path", "content_type", "body", "expected_code"),
    (
        (
            "/api/v1/app/previews",
            "application/json",
            b"{not-valid-json",
            "WRITES_UNVERIFIED_ON_WINDOWS",
        ),
        (
            "/api/v1/sensitive/previews",
            "text/plain",
            b"{}",
            "SENSITIVE_WRITES_UNVERIFIED_ON_WINDOWS",
        ),
    ),
)
def test_windows_write_denial_precedes_body_parsing(
    tmp_path,
    path,
    content_type,
    body,
    expected_code,
) -> None:
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_bytes(b"tts:\n  enabled: true\n")
    services = ReadOnlyConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {},
            layer="synthetic_inherited",
        ),
        background_health_code=None,
        platform_capabilities=platform_capabilities_for(
            os_family="nt",
            runtime_name="win32",
            user_id=None,
            temp_directory=tmp_path / "platform-tmp",
        ),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        response = client.post(
            path,
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-CSRF": bootstrap.json()["csrf_token"],
                "Content-Type": content_type,
            },
            content=body,
        )

    assert response.status_code == 503
    assert response.json() == {"error": {"code": expected_code}}


def test_duplicate_csrf_headers_are_rejected() -> None:
    services = _FakeServices(app_write_enabled=True)
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        ).raise_for_status()
        response = client.post(
            "/api/v1/app/previews",
            headers=[
                ("Origin", "http://127.0.0.1:8765"),
                ("X-Spica-CSRF", "csrf-token"),
                ("X-Spica-CSRF", "attacker-value"),
            ],
            json={
                "operations": [
                    {
                        "kind": "set",
                        "path": [{"kind": "field", "name": "max_tool_rounds"}],
                        "value": 3,
                    }
                ]
            },
        )

    assert response.status_code == 403
    assert response.json() == {"error": {"code": "CSRF_INVALID"}}
    assert services.app_preview_requests == []


def test_enabled_write_requires_exact_origin_and_session_bound_csrf() -> None:
    services = _FakeServices(app_write_enabled=True)
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        csrf = bootstrap.json()["csrf_token"]
        wrong_origin = client.post(
            "/api/v1/app/previews",
            headers={"Origin": "http://localhost:8765", "X-Spica-CSRF": csrf},
            json={"operations": []},
        )
        wrong_csrf = client.post(
            "/api/v1/app/previews",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-CSRF": "wrong-token",
            },
            json={"operations": []},
        )
        previewed = client.post(
            "/api/v1/app/previews",
            headers={"Origin": "http://127.0.0.1:8765", "X-Spica-CSRF": csrf},
            json={
                "operations": [
                    {
                        "kind": "set",
                        "path": [
                            {"kind": "field", "name": "tts"},
                            {"kind": "field", "name": "enabled"},
                        ],
                        "value": False,
                    }
                ]
            },
        )
        accepted = client.post(
            "/api/v1/app/commits",
            headers={"Origin": "http://127.0.0.1:8765", "X-Spica-CSRF": csrf},
            json={"preview_id": "app_preview_opaque"},
        )

    assert wrong_origin.status_code == 403
    assert wrong_origin.json() == {"error": {"code": "ORIGIN_REJECTED"}}
    assert wrong_csrf.status_code == 403
    assert wrong_csrf.json() == {"error": {"code": "CSRF_INVALID"}}
    assert previewed.status_code == 200
    assert previewed.json() == {
        "preview_id": "app_preview_opaque",
        "changed": True,
        "effect_policy": "next_spica_launch",
        "changes": [
            {
                "path": [
                    {"kind": "field", "name": "tts"},
                    {"kind": "field", "name": "enabled"},
                ],
                "display_path": "tts.enabled",
                "file_value_before": True,
                "file_value_after": False,
                "next_launch_value_before": True,
                "next_launch_value_after": False,
                "source_before": "file",
                "source_after": "file",
                "file_value_shadowed": False,
                "semantic_warning": None,
            }
        ],
    }
    assert accepted.status_code == 200
    assert accepted.json() == {
        "status": "saved",
        "effect_policy": "next_spica_launch",
        "restore_point_id": "R" * 24,
        "maintenance_code": None,
    }
    operations, preview_session = services.app_preview_requests[0]
    assert operations == (
        SetValue(
            path=ConfigFieldPath((FieldSegment("tts"), FieldSegment("enabled"))),
            value=False,
        ),
    )
    assert preview_session != "session-token"
    assert services.app_commit_requests == [
        ("app_preview_opaque", preview_session)
    ]
    assert "must-never-cross-the-api" not in previewed.text
    assert "must-never-cross-the-api" not in accepted.text


def test_app_preview_accepts_structured_unset_without_a_value_member() -> None:
    services = _FakeServices(app_write_enabled=True)
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _bootstrap_write_headers(client)
        response = client.post(
            "/api/v1/app/previews",
            headers=headers,
            json={
                "operations": [
                    {
                        "kind": "unset",
                        "path": [
                            {"kind": "field", "name": "tts"},
                            {"kind": "field", "name": "enabled"},
                        ],
                    }
                ]
            },
        )

    assert response.status_code == 200
    operations, session_id = services.app_preview_requests[0]
    assert operations == (UnsetValue(ConfigFieldPath.fields("tts", "enabled")),)
    assert session_id.startswith("session_")


def test_app_unset_rejects_a_value_member_instead_of_ignoring_it() -> None:
    services = _FakeServices(app_write_enabled=True)
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _bootstrap_write_headers(client)
        response = client.post(
            "/api/v1/app/previews",
            headers=headers,
            json={
                "operations": [
                    {
                        "kind": "unset",
                        "path": [
                            {"kind": "field", "name": "tts"},
                            {"kind": "field", "name": "enabled"},
                        ],
                        "value": False,
                    }
                ]
            },
        )

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "DOCUMENT_INVALID"}}
    assert services.app_preview_requests == []


def test_legacy_full_candidate_commit_route_is_not_an_api() -> None:
    services = _FakeServices(app_write_enabled=True)
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        response = client.post(
            "/api/v1/app/commit",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-CSRF": bootstrap.json()["csrf_token"],
            },
            json={"candidate": {"tts": {"enabled": False}}},
        )

    assert response.status_code == 404
    assert services.app_preview_requests == []
    assert services.app_commit_requests == []


def test_real_owner_service_maps_stale_app_revision_to_conflict(
    tmp_path: Path,
) -> None:
    services, _, app_path = _real_app_http_services(tmp_path)
    app = create_config_studio_app(services, _security_context())

    with TestClient(
        app,
        base_url="http://127.0.0.1:8765",
        raise_server_exceptions=False,
    ) as client:
        headers = _bootstrap_write_headers(client)
        preview_id = _preview_real_app(client, headers)
        app_path.write_bytes(b"max_tool_rounds: 4\n")
        response = client.post(
            "/api/v1/app/commits",
            headers=headers,
            json={"preview_id": preview_id},
        )

    assert response.status_code == 409
    assert response.json() == {"error": {"code": "DOCUMENT_CONFLICT"}}
    assert app_path.read_bytes() == b"max_tool_rounds: 4\n"


def test_real_owner_service_maps_lock_timeout_to_document_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services, _, app_path = _real_app_http_services(tmp_path)
    app = create_config_studio_app(services, _security_context())
    competing = ManagedDocumentTransaction(
        app_path,
        backup_root=tmp_path / "competing-state" / "backups",
        lock_root=tmp_path / "sandbox-state" / "locks",
        lock_timeout=1.0,
        platform_capabilities=_platform(tmp_path),
    )
    competing_revision = competing.preview(b"").current.revision
    publication_started = threading.Event()
    release_publication = threading.Event()
    real_replace = os.replace

    def delayed_replace(source: object, target: object) -> None:
        if Path(target) == app_path and threading.current_thread() is worker:
            publication_started.set()
            assert release_publication.wait(5)
        real_replace(source, target)

    monkeypatch.setattr(
        "spica.config.document_transaction.os.replace",
        delayed_replace,
    )
    worker_error: list[BaseException] = []

    def hold_transaction_lock() -> None:
        try:
            competing.commit(
                b"max_tool_rounds: 4\n",
                expected_revision=competing_revision,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            worker_error.append(exc)

    worker = threading.Thread(target=hold_transaction_lock)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8765",
        raise_server_exceptions=False,
    ) as client:
        headers = _bootstrap_write_headers(client)
        preview_id = _preview_real_app(client, headers)
        worker.start()
        assert publication_started.wait(2)
        try:
            response = client.post(
                "/api/v1/app/commits",
                headers=headers,
                json={"preview_id": preview_id},
            )
        finally:
            release_publication.set()
            worker.join(2)

    assert not worker.is_alive()
    assert worker_error == []
    assert response.status_code == 423
    assert response.json() == {"error": {"code": "DOCUMENT_BUSY"}}


def test_real_owner_service_maps_unsafe_managed_path_without_leaking_it(
    tmp_path: Path,
) -> None:
    services, _, app_path = _real_app_http_services(tmp_path)
    outside = tmp_path / "outside.yaml"
    outside.write_bytes(b"private-path-canary: true\n")
    app_path.unlink()
    app_path.symlink_to(outside)
    app = create_config_studio_app(services, _security_context())

    with TestClient(
        app,
        base_url="http://127.0.0.1:8765",
        raise_server_exceptions=False,
    ) as client:
        headers = _bootstrap_write_headers(client)
        response = client.post(
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

    assert response.status_code == 409
    assert response.json() == {"error": {"code": "DOCUMENT_UNSAFE"}}
    assert str(outside) not in response.text
    assert "private-path-canary" not in response.text


def test_real_app_preview_redacts_only_schema_declared_absolute_path_values(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    skill_before = str(tmp_path / "external-skill-before")
    skill_after = str(tmp_path / "external-skill-after")
    package_before = str(tmp_path / "external-package-before")
    package_after = str(tmp_path / "external-package-after")
    profile_before = "/semantic-profile-before"
    profile_after = "/semantic-profile-after"
    app_path.write_text(
        "character:\n"
        f"  skill_dir: {skill_before}\n"
        f"  package_dir: {package_before}\n"
        f"  profile_override: {profile_before}\n",
        encoding="utf-8",
    )
    candidate = (
        "character:\n"
        f"  skill_dir: {skill_after}\n"
        f"  package_dir: {package_after}\n"
        f"  profile_override: {profile_after}\n"
    ).encode()
    environment = EnvironmentSnapshot.from_mapping(
        {},
        layer="synthetic_inherited",
    )
    app_document = AppConfigDocument(
        app_path,
        backup_root=tmp_path / "sandbox-state" / "backups",
        environment_snapshot=environment,
        round_trip_editor=_FixedAppEditor(candidate),
        token_factory=lambda: "real-path-preview-token",
        platform_capabilities=_platform(tmp_path),
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=environment,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        app_document=app_document,
        enabled_write_capabilities=frozenset({"app_config_write"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/v1/app/previews",
            headers=_bootstrap_write_headers(client),
            json={
                "operations": [
                    {
                        "kind": "set",
                        "path": [
                            {"kind": "field", "name": "character"},
                            {"kind": "field", "name": "skill_dir"},
                        ],
                        "value": skill_after,
                    },
                    {
                        "kind": "set",
                        "path": [
                            {"kind": "field", "name": "character"},
                            {"kind": "field", "name": "package_dir"},
                        ],
                        "value": package_after,
                    },
                    {
                        "kind": "set",
                        "path": [
                            {"kind": "field", "name": "character"},
                            {"kind": "field", "name": "profile_override"},
                        ],
                        "value": profile_after,
                    },
                ]
            },
        )

    response.raise_for_status()
    changes = {
        change["display_path"]: change for change in response.json()["changes"]
    }
    for field_name in ("character.skill_dir", "character.package_dir"):
        for value_name in (
            "file_value_before",
            "file_value_after",
            "next_launch_value_before",
            "next_launch_value_after",
        ):
            assert changes[field_name][value_name] == "<external-path>"
    assert changes["character.profile_override"]["file_value_before"] == (
        profile_before
    )
    assert (
        changes["character.profile_override"]["file_value_after"]
        == profile_after
    )
    assert changes["character.profile_override"]["next_launch_value_before"] == (
        profile_before
    )
    assert changes["character.profile_override"]["next_launch_value_after"] == (
        profile_after
    )
    for private_path in (skill_before, skill_after, package_before, package_after):
        assert private_path not in response.text


def test_real_app_preview_redacts_known_secret_canaries_from_semantic_values(
    tmp_path: Path,
) -> None:
    secret_canary = "synthetic-app-preview-secret-canary"
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_text(f"llm:\n  model: {secret_canary}\n", encoding="utf-8")
    environment = EnvironmentSnapshot.from_mapping({}, layer="synthetic")
    app_document = AppConfigDocument(
        app_path,
        backup_root=tmp_path / "sandbox-state" / "backups",
        environment_snapshot=environment,
        round_trip_editor=_FixedAppEditor(b"llm:\n  model: safe-model\n"),
        platform_capabilities=_platform(tmp_path),
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=environment,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        secrets=Secrets(openai_api_key=secret_canary),
        app_document=app_document,
        enabled_write_capabilities=frozenset({"app_config_write"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/v1/app/previews",
            headers=_bootstrap_write_headers(client),
            json={
                "operations": [
                    {
                        "kind": "set",
                        "path": [
                            {"kind": "field", "name": "llm"},
                            {"kind": "field", "name": "model"},
                        ],
                        "value": "safe-model",
                    }
                ]
            },
        )

    assert response.status_code == 200
    assert secret_canary not in response.text
    assert response.json()["changes"][0]["file_value_before"] == (
        "«REDACTED:OPENAI_API_KEY»"
    )


def test_real_app_preview_rejects_a_candidate_containing_a_known_secret(
    tmp_path: Path,
) -> None:
    secret_canary = "synthetic-app-candidate-secret"
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    original = b"llm:\n  model: safe-model\n"
    app_path.write_bytes(original)
    environment = EnvironmentSnapshot.from_mapping({}, layer="synthetic")
    app_document = AppConfigDocument(
        app_path,
        backup_root=tmp_path / "sandbox-state" / "backups",
        environment_snapshot=environment,
        round_trip_editor=_FixedAppEditor(
            f"llm:\n  model: {secret_canary}\n".encode("utf-8")
        ),
        platform_capabilities=_platform(tmp_path),
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=environment,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        secrets=Secrets(openai_api_key=secret_canary),
        app_document=app_document,
        enabled_write_capabilities=frozenset({"app_config_write"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/v1/app/previews",
            headers=_bootstrap_write_headers(client),
            json={
                "operations": [
                    {
                        "kind": "set",
                        "path": [
                            {"kind": "field", "name": "llm"},
                            {"kind": "field", "name": "model"},
                        ],
                        "value": secret_canary,
                    }
                ]
            },
        )

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "DOCUMENT_INVALID"}}
    assert secret_canary not in response.text
    assert app_path.read_bytes() == original


def test_real_app_commit_rechecks_secrets_that_changed_after_preview(
    tmp_path: Path,
) -> None:
    late_secret = "synthetic-secret-created-after-preview"
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    original = b"llm:\n  model: safe-model\n"
    app_path.write_bytes(original)
    environment = EnvironmentSnapshot.from_mapping({}, layer="synthetic")
    app_document = AppConfigDocument(
        app_path,
        backup_root=tmp_path / "sandbox-state" / "backups",
        environment_snapshot=environment,
        round_trip_editor=_FixedAppEditor(
            f"llm:\n  model: {late_secret}\n".encode("utf-8")
        ),
        platform_capabilities=_platform(tmp_path),
    )
    owner_snapshots = iter(
        (
            LoadedSecrets(secrets=Secrets(), environment_snapshot=environment),
            LoadedSecrets(secrets=Secrets(), environment_snapshot=environment),
            LoadedSecrets(
                secrets=Secrets(openai_api_key=late_secret),
                environment_snapshot=environment,
            ),
        )
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=environment,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        app_document=app_document,
        environment_owner=lambda: next(owner_snapshots),
        enabled_write_capabilities=frozenset({"app_config_write"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _bootstrap_write_headers(client)
        preview = client.post(
            "/api/v1/app/previews",
            headers=headers,
            json={
                "operations": [
                    {
                        "kind": "set",
                        "path": [
                            {"kind": "field", "name": "llm"},
                            {"kind": "field", "name": "model"},
                        ],
                        "value": late_secret,
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

    assert committed.status_code == 400
    assert committed.json() == {"error": {"code": "DOCUMENT_INVALID"}}
    assert late_secret not in committed.text
    assert app_path.read_bytes() == original


def test_real_app_preview_fails_closed_when_secret_owner_rotates_during_preview(
    tmp_path: Path,
) -> None:
    rotated_secret = "newly-rotated-secret-canary"
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    original = (
        f"llm:\n  model: {rotated_secret}\nmax_tool_rounds: 2\n".encode()
    )
    app_path.write_bytes(original)
    candidate = b"llm:\n  model: safe-model\nmax_tool_rounds: 2\n"
    environment = EnvironmentSnapshot.from_mapping({}, layer="synthetic")
    loaded = iter(
        (
            LoadedSecrets(
                secrets=Secrets(openai_api_key="old-secret"),
                environment_snapshot=environment,
            ),
            LoadedSecrets(
                secrets=Secrets(openai_api_key=rotated_secret),
                environment_snapshot=environment,
            ),
        )
    )

    def latest_environment() -> LoadedSecrets:
        return next(loaded)

    app_document = AppConfigDocument(
        app_path,
        backup_root=tmp_path / "sandbox-state" / "backups",
        environment_snapshot=environment,
        environment_snapshot_owner=(
            lambda: latest_environment().environment_snapshot
        ),
        round_trip_editor=_FixedAppEditor(candidate),
        platform_capabilities=_platform(tmp_path),
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=environment,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        app_document=app_document,
        environment_owner=latest_environment,
        enabled_write_capabilities=frozenset({"app_config_write"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/v1/app/previews",
            headers=_bootstrap_write_headers(client),
            json={
                "operations": [
                    {
                        "kind": "set",
                        "path": [
                            {"kind": "field", "name": "llm"},
                            {"kind": "field", "name": "model"},
                        ],
                        "value": "safe-model",
                    }
                ]
            },
        )

    assert response.status_code == 409
    assert response.json() == {"error": {"code": "CONFIRMATION_REQUIRED"}}
    assert rotated_secret not in response.text
    assert app_path.read_bytes() == original


def test_real_app_commit_fails_closed_when_secret_owner_rotates_before_publish(
    tmp_path: Path,
) -> None:
    rotated_secret = "rotated-after-service-refresh"
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    original = b"llm:\n  model: safe-before\n"
    app_path.write_bytes(original)
    environment = EnvironmentSnapshot.from_mapping({}, layer="synthetic")
    old = LoadedSecrets(
        secrets=Secrets(openai_api_key="old-secret"),
        environment_snapshot=environment,
    )
    new = LoadedSecrets(
        secrets=Secrets(openai_api_key=rotated_secret),
        environment_snapshot=environment,
    )
    loaded = iter((old, old, old, new))

    def latest_environment() -> LoadedSecrets:
        return next(loaded)

    app_document = AppConfigDocument(
        app_path,
        backup_root=tmp_path / "sandbox-state" / "backups",
        environment_snapshot=environment,
        environment_snapshot_owner=(
            lambda: latest_environment().environment_snapshot
        ),
        round_trip_editor=_FixedAppEditor(
            f"llm:\n  model: {rotated_secret}\n".encode()
        ),
        platform_capabilities=_platform(tmp_path),
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=environment,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        app_document=app_document,
        environment_owner=latest_environment,
        enabled_write_capabilities=frozenset({"app_config_write"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _bootstrap_write_headers(client)
        preview = client.post(
            "/api/v1/app/previews",
            headers=headers,
            json={
                "operations": [
                    {
                        "kind": "set",
                        "path": [
                            {"kind": "field", "name": "llm"},
                            {"kind": "field", "name": "model"},
                        ],
                        "value": rotated_secret,
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

    assert committed.status_code == 400
    assert committed.json() == {"error": {"code": "DOCUMENT_INVALID"}}
    assert rotated_secret not in committed.text
    assert app_path.read_bytes() == original


def test_real_app_preview_rejects_binary_yaml_containing_a_known_secret(
    tmp_path: Path,
) -> None:
    secret_canary = "synthetic-binary-secret"
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    original = (
        b"llm:\n  model: old-model\n"
        b"song:\n  binary_owner_value: "
        b"!!binary c3ludGhldGljLWJpbmFyeS1zZWNyZXQ=\n"
    )
    app_path.write_bytes(original)
    environment = EnvironmentSnapshot.from_mapping({}, layer="synthetic")
    app_document = AppConfigDocument(
        app_path,
        backup_root=tmp_path / "sandbox-state" / "backups",
        environment_snapshot=environment,
        round_trip_editor=_FixedAppEditor(
            b"llm:\n  model: safe-model\n"
            b"song:\n  binary_owner_value: "
            b"!!binary c3ludGhldGljLWJpbmFyeS1zZWNyZXQ=\n"
        ),
        platform_capabilities=_platform(tmp_path),
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=environment,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        secrets=Secrets(openai_api_key=secret_canary),
        app_document=app_document,
        enabled_write_capabilities=frozenset({"app_config_write"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/v1/app/previews",
            headers=_bootstrap_write_headers(client),
            json={
                "operations": [
                    {
                        "kind": "set",
                        "path": [
                            {"kind": "field", "name": "llm"},
                            {"kind": "field", "name": "model"},
                        ],
                        "value": "safe-model",
                    }
                ]
            },
        )

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "DOCUMENT_INVALID"}}
    assert secret_canary not in response.text
    assert app_path.read_bytes() == original


@pytest.mark.parametrize(
    ("legacy_relative", "original", "candidate", "wire_path", "value"),
    (
        (
            "data/config/plugins.yaml",
            b"plugins:\n  - name: alpha\n    enabled: false\n",
            b"plugins: []\n",
            [{"kind": "field", "name": "plugins"}],
            [],
        ),
        (
            "config/screen_vision_config.json",
            b"screen:\n  enabled: true\n",
            b"screen:\n  enabled: false\n",
            [
                {"kind": "field", "name": "screen"},
                {"kind": "field", "name": "enabled"},
            ],
            False,
        ),
        (
            "agent_tools/function_tools/song/song_config.json",
            b"song:\n  enabled: true\n",
            b"song:\n  enabled: false\n",
            [
                {"kind": "field", "name": "song"},
                {"kind": "map_key", "key": "enabled"},
            ],
            False,
        ),
    ),
)
def test_app_preview_rejects_sections_owned_by_a_retired_legacy_document(
    tmp_path: Path,
    legacy_relative: str,
    original: bytes,
    candidate: bytes,
    wire_path: list[dict[str, Any]],
    value: Any,
) -> None:
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_bytes(original)
    legacy_path = repo_root / legacy_relative
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_bytes(b"{}\n")
    environment = EnvironmentSnapshot.from_mapping({}, layer="synthetic")
    app_document = AppConfigDocument(
        app_path,
        backup_root=tmp_path / "sandbox-state" / "backups",
        environment_snapshot=environment,
        round_trip_editor=_FixedAppEditor(candidate),
        platform_capabilities=_platform(tmp_path),
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=environment,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        app_document=app_document,
        enabled_write_capabilities=frozenset({"app_config_write"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/v1/app/previews",
            headers=_bootstrap_write_headers(client),
            json={
                "operations": [
                    {"kind": "set", "path": wire_path, "value": value}
                ]
            },
        )

    assert response.status_code == 409
    assert response.json() == {"error": {"code": "DOCUMENT_UNSAFE"}}
    assert app_path.read_bytes() == original


@pytest.mark.parametrize(
    ("original", "candidate", "operation"),
    (
        (
            b"character:\n  package_dir: characters/spica\n",
            b"character:\n  character_id: forged-character\n",
            {
                "kind": "set",
                "path": [{"kind": "field", "name": "character"}],
                "value": {"character_id": "forged-character"},
            },
        ),
        (
            b"character:\n  character_id: forged-character\n",
            b"{}\n",
            {
                "kind": "unset",
                "path": [{"kind": "field", "name": "character"}],
            },
        ),
        (
            b"screen:\n  max_side: 768\n",
            b"screen:\n  max_side: 5000\n",
            {
                "kind": "set",
                "path": [{"kind": "field", "name": "screen"}],
                "value": {"max_side": 5000},
            },
        ),
        (
            b"screen:\n  infer_timeout_sec: 30.0\n",
            b"screen:\n  infer_timeout_sec: -1.0\n",
            {
                "kind": "set",
                "path": [{"kind": "field", "name": "screen"}],
                "value": {"infer_timeout_sec": -1.0},
            },
        ),
    ),
)
def test_real_app_preview_rejects_read_only_or_nested_model_parent_operations(
    tmp_path: Path,
    original: bytes,
    candidate: bytes,
    operation: dict[str, Any],
) -> None:
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_bytes(original)
    environment = EnvironmentSnapshot.from_mapping({}, layer="synthetic")
    app_document = AppConfigDocument(
        app_path,
        backup_root=tmp_path / "sandbox-state" / "backups",
        environment_snapshot=environment,
        round_trip_editor=_FixedAppEditor(candidate),
        platform_capabilities=_platform(tmp_path),
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=environment,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        app_document=app_document,
        enabled_write_capabilities=frozenset({"app_config_write"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/v1/app/previews",
            headers=_bootstrap_write_headers(client),
            json={"operations": [operation]},
        )

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "DOCUMENT_INVALID"}}
    assert app_path.read_bytes() == original


def test_app_commit_rechecks_legacy_owner_presence_after_preview(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    original = b"plugins:\n  - name: alpha\n    enabled: false\n"
    app_path.write_bytes(original)
    environment = EnvironmentSnapshot.from_mapping({}, layer="synthetic")
    app_document = AppConfigDocument(
        app_path,
        backup_root=tmp_path / "sandbox-state" / "backups",
        environment_snapshot=environment,
        round_trip_editor=_FixedAppEditor(b"plugins: []\n"),
        platform_capabilities=_platform(tmp_path),
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=environment,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        app_document=app_document,
        enabled_write_capabilities=frozenset({"app_config_write"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _bootstrap_write_headers(client)
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
        legacy_path = repo_root / "data" / "config" / "plugins.yaml"
        legacy_path.write_bytes(b"{}\n")
        committed = client.post(
            "/api/v1/app/commits",
            headers=headers,
            json={"preview_id": preview.json()["preview_id"]},
        )

    assert committed.status_code == 409
    assert committed.json() == {"error": {"code": "DOCUMENT_UNSAFE"}}
    assert app_path.read_bytes() == original


def test_real_app_preview_redacts_short_canary_without_changing_wire_keys(
    tmp_path: Path,
) -> None:
    secret_canary = "enabled"
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_text("tts:\n  enabled: true\n", encoding="utf-8")
    environment = EnvironmentSnapshot.from_mapping({}, layer="synthetic")
    app_document = AppConfigDocument(
        app_path,
        backup_root=tmp_path / "sandbox-state" / "backups",
        environment_snapshot=environment,
        round_trip_editor=_FixedAppEditor(b"tts:\n  enabled: false\n"),
        platform_capabilities=_platform(tmp_path),
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=environment,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        secrets=Secrets(openai_api_key=secret_canary),
        app_document=app_document,
        enabled_write_capabilities=frozenset({"app_config_write"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/v1/app/previews",
            headers=_bootstrap_write_headers(client),
            json={
                "operations": [
                    {
                        "kind": "set",
                        "path": [
                            {"kind": "field", "name": "tts"},
                            {"kind": "field", "name": "enabled"},
                        ],
                        "value": False,
                    }
                ]
            },
        )

    assert response.status_code == 200
    change = response.json()["changes"][0]
    assert set(change) >= {
        "path",
        "display_path",
        "file_value_before",
        "file_value_after",
    }
    assert change["path"] == [
        {"kind": "field", "name": "tts"},
        {"kind": "field", "name": "enabled"},
    ]
    assert change["display_path"] == "tts.«REDACTED:OPENAI_API_KEY»"


def test_real_app_preview_rejects_a_known_secret_in_a_dynamic_map_key(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    original = b"song:\n  enabled: true\n"
    app_path.write_bytes(original)
    environment = EnvironmentSnapshot.from_mapping({}, layer="synthetic")
    app_document = AppConfigDocument(
        app_path,
        backup_root=tmp_path / "sandbox-state" / "backups",
        environment_snapshot=environment,
        round_trip_editor=_FixedAppEditor(b"song:\n  enabled: false\n"),
        platform_capabilities=_platform(tmp_path),
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=environment,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        secrets=Secrets(openai_api_key="enabled"),
        app_document=app_document,
        enabled_write_capabilities=frozenset({"app_config_write"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/v1/app/previews",
            headers=_bootstrap_write_headers(client),
            json={
                "operations": [
                    {
                        "kind": "set",
                        "path": [
                            {"kind": "field", "name": "song"},
                            {"kind": "map_key", "key": "enabled"},
                        ],
                        "value": False,
                    }
                ]
            },
        )

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "DOCUMENT_INVALID"}}
    assert app_path.read_bytes() == original


def test_real_app_rollback_rejects_a_restore_point_containing_a_known_secret(
    tmp_path: Path,
) -> None:
    secret_canary = "synthetic-rollback-map-key-canary"
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_text(
        f"song:\n  {secret_canary}:\n    enabled: true\n",
        encoding="utf-8",
    )
    backup_root = tmp_path / "sandbox-state" / "backups"
    platform = _platform(tmp_path)
    transaction = ManagedDocumentTransaction(
        app_path,
        backup_root=backup_root,
        lock_root=backup_root.parent / "locks",
        platform_capabilities=platform,
    )
    committed = transaction.commit(
        b"song: {}\n",
        expected_revision=transaction.preview(b"").current.revision,
    )
    assert committed.restore_point is not None
    environment = EnvironmentSnapshot.from_mapping({}, layer="synthetic")
    app_document = AppConfigDocument(
        app_path,
        backup_root=backup_root,
        environment_snapshot=environment,
        round_trip_editor=_FixedAppEditor(b"song: {}\n"),
        platform_capabilities=platform,
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=environment,
        background_health_code=None,
        platform_capabilities=platform,
        secrets=Secrets(openai_api_key=secret_canary),
        app_document=app_document,
        enabled_write_capabilities=frozenset({"app_config_write", "rollback"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/v1/app/restore-points/"
            + committed.restore_point.id
            + "/prepare-rollback",
            headers=_bootstrap_write_headers(client),
        )

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "DOCUMENT_INVALID"}}
    assert secret_canary not in response.text
    assert app_path.read_bytes() == b"song: {}\n"


def test_real_app_rollback_rechecks_secrets_that_changed_after_prepare(
    tmp_path: Path,
) -> None:
    late_secret = "synthetic-rollback-secret-created-after-prepare"
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_text(f"llm:\n  model: {late_secret}\n", encoding="utf-8")
    backup_root = tmp_path / "sandbox-state" / "backups"
    platform = _platform(tmp_path)
    transaction = ManagedDocumentTransaction(
        app_path,
        backup_root=backup_root,
        lock_root=backup_root.parent / "locks",
        platform_capabilities=platform,
    )
    committed = transaction.commit(
        b"llm:\n  model: safe-model\n",
        expected_revision=transaction.preview(b"").current.revision,
    )
    assert committed.restore_point is not None
    environment = EnvironmentSnapshot.from_mapping({}, layer="synthetic")
    app_document = AppConfigDocument(
        app_path,
        backup_root=backup_root,
        environment_snapshot=environment,
        round_trip_editor=_FixedAppEditor(b"llm:\n  model: safe-model\n"),
        platform_capabilities=platform,
    )
    owner_snapshots = iter(
        (
            LoadedSecrets(secrets=Secrets(), environment_snapshot=environment),
            LoadedSecrets(secrets=Secrets(), environment_snapshot=environment),
            LoadedSecrets(
                secrets=Secrets(openai_api_key=late_secret),
                environment_snapshot=environment,
            ),
        )
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=environment,
        background_health_code=None,
        platform_capabilities=platform,
        app_document=app_document,
        environment_owner=lambda: next(owner_snapshots),
        enabled_write_capabilities=frozenset({"app_config_write", "rollback"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _bootstrap_write_headers(client)
        prepared = client.post(
            "/api/v1/app/restore-points/"
            + committed.restore_point.id
            + "/prepare-rollback",
            headers=headers,
        )
        prepared.raise_for_status()
        restored = client.post(
            "/api/v1/app/rollbacks",
            headers=headers,
            json={
                "confirmation_receipt": prepared.json()["confirmation_receipt"]
            },
        )

    assert restored.status_code == 400
    assert restored.json() == {"error": {"code": "DOCUMENT_INVALID"}}
    assert late_secret not in restored.text
    assert app_path.read_bytes() == b"llm:\n  model: safe-model\n"


def test_real_sensitive_override_preview_redacts_only_schema_path_values(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_bytes(b"max_tool_rounds: 2\n")
    skill_before = str(tmp_path / "external-repo-skill")
    skill_after = str(tmp_path / "external-file-skill")
    model_before = "/semantic-model-before"
    model_after = "/semantic-model-after"
    sensitive_path = repo_root / "xiaosan.env"
    sensitive_path.write_text(
        f"SPICA_SKILL_DIR={skill_before}\nMODEL={model_before}\n",
        encoding="utf-8",
    )
    sensitive_path.chmod(0o600)
    parent_path = tmp_path / "sandbox-parent" / "xiaosan.env"
    parent_path.parent.mkdir(parents=True)
    parent_path.write_bytes(b"")
    parent_path.chmod(0o600)
    environment_owner = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={},
        repo_env_path=sensitive_path,
        parent_env_path=parent_path,
        prime_process=False,
    )
    assert isinstance(environment_owner, LoadedSecrets)
    preview_tokens = iter(
        ("real-path-override-preview", "real-model-override-preview")
    )
    sensitive_document = SensitiveEnvDocument(
        sensitive_path,
        backup_root=tmp_path / "sandbox-state" / "sensitive-backups",
        environment_owner=environment_owner,
        base_document={
            "character": {"skill_dir": skill_after},
            "llm": {"model": model_after},
        },
        preview_factory=preview_tokens.__next__,
        platform_capabilities=_platform(tmp_path),
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=environment_owner.environment_snapshot,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        secrets=environment_owner.secrets,
        sensitive_document=sensitive_document,
        enabled_write_capabilities=frozenset({"sensitive_write"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _bootstrap_write_headers(client)
        path_preview = client.post(
            "/api/v1/sensitive/previews",
            headers=headers,
            json={
                "command": {
                    "kind": "clear_mapped_override",
                    "environment_variable": "SPICA_SKILL_DIR",
                }
            },
        )
        semantic_preview = client.post(
            "/api/v1/sensitive/previews",
            headers=headers,
            json={
                "command": {
                    "kind": "clear_mapped_override",
                    "environment_variable": "MODEL",
                }
            },
        )

    path_preview.raise_for_status()
    semantic_preview.raise_for_status()
    assert path_preview.json()["before_next_launch"] == "<external-path>"
    assert path_preview.json()["after_next_launch"] == "<external-path>"
    assert skill_before not in path_preview.text
    assert skill_after not in path_preview.text
    assert semantic_preview.json()["before_next_launch"] == model_before
    assert semantic_preview.json()["after_next_launch"] == model_after


def test_sensitive_mapped_override_preview_redacts_a_secret_file_fallback(
    tmp_path: Path,
) -> None:
    secret_canary = "synthetic-mapped-fallback-secret"
    repo_root = tmp_path / "sandbox-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_text(f"llm:\n  model: {secret_canary}\n", encoding="utf-8")
    sensitive_path = repo_root / "xiaosan.env"
    sensitive_path.write_text(
        f"OPENAI_API_KEY={secret_canary}\nMODEL=repo-model\n",
        encoding="utf-8",
    )
    sensitive_path.chmod(0o600)
    parent_path = tmp_path / "sandbox-parent" / "xiaosan.env"
    parent_path.parent.mkdir(parents=True)
    parent_path.write_bytes(b"")
    parent_path.chmod(0o600)
    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={},
        repo_env_path=sensitive_path,
        parent_env_path=parent_path,
        prime_process=False,
    )
    assert isinstance(loaded, LoadedSecrets)
    sensitive_document = SensitiveEnvDocument(
        sensitive_path,
        backup_root=tmp_path / "sandbox-state" / "sensitive-backups",
        environment_owner=loaded,
        base_document={"llm": {"model": secret_canary}},
        platform_capabilities=_platform(tmp_path),
    )
    services = OwnerBackedConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=loaded.environment_snapshot,
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        secrets=loaded.secrets,
        sensitive_document=sensitive_document,
        enabled_write_capabilities=frozenset({"sensitive_write"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/v1/sensitive/previews",
            headers=_bootstrap_write_headers(client),
            json={
                "command": {
                    "kind": "clear_mapped_override",
                    "environment_variable": "MODEL",
                }
            },
        )

    response.raise_for_status()
    assert secret_canary not in response.text
    assert response.json()["after_next_launch"] == "«REDACTED:OPENAI_API_KEY»"


def test_real_owner_service_maps_invalid_or_expired_preview_to_confirmation(
    tmp_path: Path,
) -> None:
    services, _, _ = _real_app_http_services(tmp_path)
    app = create_config_studio_app(services, _security_context())

    with TestClient(
        app,
        base_url="http://127.0.0.1:8765",
        raise_server_exceptions=False,
    ) as client:
        headers = _bootstrap_write_headers(client)
        response = client.post(
            "/api/v1/app/commits",
            headers=headers,
            json={"preview_id": "unknown-preview-token"},
        )

    assert response.status_code == 409
    assert response.json() == {"error": {"code": "CONFIRMATION_REQUIRED"}}


def test_real_overlay_owner_maps_validation_and_stale_preview_errors(
    tmp_path: Path,
) -> None:
    services, overlay_path = _real_overlay_http_services(tmp_path)
    app = create_config_studio_app(services, _security_context())

    with TestClient(
        app,
        base_url="http://127.0.0.1:8765",
        raise_server_exceptions=False,
    ) as client:
        headers = _bootstrap_write_headers(client)
        invalid = client.post(
            "/api/v1/overlay/previews",
            headers=headers,
            json={"key": "spica_voice_volume", "value": 2.0},
        )
        preview = client.post(
            "/api/v1/overlay/previews",
            headers=headers,
            json={"key": "spica_voice_volume", "value": 0.7},
        )
        preview.raise_for_status()
        overlay_path.write_bytes(b'{"spica_voice_volume": 0.9}\n')
        conflict = client.post(
            "/api/v1/overlay/commits",
            headers=headers,
            json={"preview_id": preview.json()["preview_id"]},
        )

    assert invalid.status_code == 400
    assert invalid.json() == {"error": {"code": "DOCUMENT_INVALID"}}
    assert conflict.status_code == 409
    assert conflict.json() == {"error": {"code": "DOCUMENT_CONFLICT"}}
    assert overlay_path.read_bytes() == b'{"spica_voice_volume": 0.9}\n'


@pytest.mark.parametrize(
    ("existing_value", "requested_value", "expected_before", "expected_after"),
    (
        (None, 1.5, 1.0, "«REDACTED:OPENAI_API_KEY»"),
        (1.5, 1.2, "«REDACTED:OPENAI_API_KEY»", 1.2),
    ),
)
def test_real_overlay_preview_redacts_canonical_float_data_slots(
    tmp_path: Path,
    existing_value: float | None,
    requested_value: float,
    expected_before: float | str,
    expected_after: float | str,
) -> None:
    services, overlay_path = _real_overlay_http_services(
        tmp_path,
        secret_canary="1.5",
    )
    if existing_value is not None:
        overlay_path.write_text(
            json.dumps({"default_ui_scale": existing_value}) + "\n",
            encoding="utf-8",
        )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/v1/overlay/previews",
            headers=_bootstrap_write_headers(client),
            json={"key": "default_ui_scale", "value": requested_value},
        )

    response.raise_for_status()
    assert response.json() == {
        "preview_id": "real-overlay-preview-token",
        "key": "default_ui_scale",
        "file_value_before": expected_before,
        "file_value_after": expected_after,
        "changed": True,
        "effect_policy": "next_spica_launch",
    }


def test_real_sensitive_owner_sets_secret_then_get_returns_only_configuration(
    tmp_path: Path,
) -> None:
    services, sensitive_path = _real_sensitive_http_services(
        tmp_path,
        repo_env_content=b"",
    )
    app = create_config_studio_app(services, _security_context())
    canary = "synthetic-set-secret-canary"

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _bootstrap_write_headers(client)
        before = client.get("/api/v1/sensitive/status")
        preview = client.post(
            "/api/v1/sensitive/previews",
            headers=headers,
            json={
                "command": {
                    "kind": "set_secret",
                    "slot": "openai_api_key",
                    "value": canary,
                }
            },
        )
        preview.raise_for_status()
        committed = client.post(
            "/api/v1/sensitive/commits",
            headers=headers,
            json={"preview_id": preview.json()["preview_id"]},
        )
        after = client.get("/api/v1/sensitive/status")

    assert before.json()["secret_slots"][0] == {
        "slot": "openai_api_key",
        "configured": False,
    }
    assert preview.json()["secret_change"] == "will_set"
    assert committed.status_code == 200
    assert committed.json()["permission_health"] == "PRIVATE"
    assert after.json()["secret_slots"][0] == {
        "slot": "openai_api_key",
        "configured": True,
    }
    assert canary.encode() in sensitive_path.read_bytes()
    for response in (before, preview, committed, after):
        assert canary not in response.text


def test_real_sensitive_owner_clear_receipt_is_bound_and_consumed_once(
    tmp_path: Path,
) -> None:
    services, sensitive_path = _real_sensitive_http_services(tmp_path)
    app = create_config_studio_app(services, _security_context())
    canary = "synthetic-repo-secret"

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _bootstrap_write_headers(client)
        preview = client.post(
            "/api/v1/sensitive/previews",
            headers=headers,
            json={
                "command": {
                    "kind": "clear_secret",
                    "slot": "openai_api_key",
                }
            },
        )
        preview.raise_for_status()
        confirmation = client.post(
            "/api/v1/sensitive/previews/"
            + preview.json()["preview_id"]
            + "/confirm-clear",
            headers=headers,
        )
        confirmation.raise_for_status()
        commit_payload = {
            "preview_id": preview.json()["preview_id"],
            "confirmation_receipt": confirmation.json()["confirmation_receipt"],
        }
        committed = client.post(
            "/api/v1/sensitive/commits",
            headers=headers,
            json=commit_payload,
        )
        reused = client.post(
            "/api/v1/sensitive/commits",
            headers=headers,
            json=commit_payload,
        )
        status = client.get("/api/v1/sensitive/status")

    assert preview.json()["secret_change"] == "will_clear"
    assert confirmation.json() == {
        "confirmation_receipt": "real-sensitive-receipt-token",
        "preview_id": "real-sensitive-preview-token",
        "command_kind": "clear_secret",
        "target": "openai_api_key",
        "secret_change": "will_clear",
    }
    assert committed.status_code == 200
    assert reused.status_code == 409
    assert reused.json() == {"error": {"code": "CONFIRMATION_REQUIRED"}}
    assert status.json()["secret_slots"][0] == {
        "slot": "openai_api_key",
        "configured": False,
    }
    assert canary.encode() not in sensitive_path.read_bytes()
    for response in (preview, confirmation, committed, reused, status):
        assert canary not in response.text


def test_real_sensitive_owner_clears_mapped_override_and_refreshes_catalog(
    tmp_path: Path,
) -> None:
    canary = "synthetic-unrelated-secret-canary"
    services, sensitive_path = _real_sensitive_http_services(
        tmp_path,
        repo_env_content=(
            f"OPENAI_API_KEY={canary}\nMODEL=synthetic-repo-model\n".encode()
        ),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _bootstrap_write_headers(client)
        preview = client.post(
            "/api/v1/sensitive/previews",
            headers=headers,
            json={
                "command": {
                    "kind": "clear_mapped_override",
                    "environment_variable": "MODEL",
                }
            },
        )
        preview.raise_for_status()
        committed = client.post(
            "/api/v1/sensitive/commits",
            headers=headers,
            json={"preview_id": preview.json()["preview_id"]},
        )
        catalog = client.get("/api/v1/catalog")

    assert preview.json()["before_next_launch"] == "synthetic-repo-model"
    assert preview.json()["after_next_launch"] == "gpt-4.1-mini"
    assert preview.json()["winning_source_before"] == "repo_dotenv"
    assert preview.json()["winning_source_after"] == "default"
    assert committed.status_code == 200
    model = next(
        field
        for field in catalog.json()["fields"]
        if field["display_path"] == "llm.model"
    )
    assert model["next_launch_value"] == "gpt-4.1-mini"
    assert model["source_kind"] == "default"
    assert b"MODEL=" not in sensitive_path.read_bytes()
    assert canary.encode() in sensitive_path.read_bytes()
    for response in (preview, committed, catalog):
        assert canary not in response.text


def test_real_sensitive_owner_rollback_restores_whole_document_once(
    tmp_path: Path,
) -> None:
    canary = "synthetic-rollback-secret-canary"
    original = (
        f"OPENAI_API_KEY={canary}\nMODEL=synthetic-original-model\n".encode()
    )
    services, sensitive_path = _real_sensitive_http_services(
        tmp_path,
        repo_env_content=original,
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _bootstrap_write_headers(client)
        preview = client.post(
            "/api/v1/sensitive/previews",
            headers=headers,
            json={
                "command": {
                    "kind": "clear_mapped_override",
                    "environment_variable": "MODEL",
                }
            },
        )
        preview.raise_for_status()
        committed = client.post(
            "/api/v1/sensitive/commits",
            headers=headers,
            json={"preview_id": preview.json()["preview_id"]},
        )
        committed.raise_for_status()
        restore_point_id = committed.json()["restore_point_id"]
        points = client.get("/api/v1/sensitive/restore-points")
        prepared = client.post(
            f"/api/v1/sensitive/restore-points/{restore_point_id}/prepare-rollback",
            headers=headers,
        )
        prepared.raise_for_status()
        rollback_payload = {
            "confirmation_receipt": prepared.json()["confirmation_receipt"]
        }
        restored = client.post(
            "/api/v1/sensitive/rollbacks",
            headers=headers,
            json=rollback_payload,
        )
        reused = client.post(
            "/api/v1/sensitive/rollbacks",
            headers=headers,
            json=rollback_payload,
        )
        catalog = client.get("/api/v1/catalog")

    assert points.json()["restore_points"][0]["restore_point_id"] == (
        restore_point_id
    )
    assert prepared.json()["restore_point_id"] == restore_point_id
    assert prepared.json()["unmanaged_content_changed"] is False
    assert prepared.json()["override_changes"] == [
        {
            "environment_variable": "MODEL",
            "affected_fields": ["llm.model"],
            "before_next_launch": "gpt-4.1-mini",
            "after_next_launch": "synthetic-original-model",
            "winning_source_before": "default",
            "winning_source_after": "repo_dotenv",
            "still_shadowed": False,
        }
    ]
    assert restored.status_code == 200
    assert restored.json()["status"] == "restored"
    assert reused.status_code == 409
    assert reused.json() == {
        "error": {"code": "ROLLBACK_CONFIRMATION_INVALID"}
    }
    model = next(
        field
        for field in catalog.json()["fields"]
        if field["display_path"] == "llm.model"
    )
    assert model["next_launch_value"] == "synthetic-original-model"
    assert model["source_kind"] == "env_override"
    assert sensitive_path.read_bytes() == original
    for response in (points, prepared, restored, reused, catalog):
        assert canary not in response.text


def test_real_sensitive_rollback_preserves_owner_invalid_code_when_parent_changes(
    tmp_path: Path,
) -> None:
    canary = "synthetic-owner-change-secret-canary"
    services, sensitive_path = _real_sensitive_http_services(
        tmp_path,
        repo_env_content=(
            f"OPENAI_API_KEY={canary}\nMODEL=repo-model\n".encode("utf-8")
        ),
    )
    parent_path = tmp_path / "sandbox-parent" / "xiaosan.env"
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _bootstrap_write_headers(client)
        preview = client.post(
            "/api/v1/sensitive/previews",
            headers=headers,
            json={
                "command": {
                    "kind": "clear_mapped_override",
                    "environment_variable": "MODEL",
                }
            },
        )
        preview.raise_for_status()
        committed = client.post(
            "/api/v1/sensitive/commits",
            headers=headers,
            json={"preview_id": preview.json()["preview_id"]},
        )
        committed.raise_for_status()
        current = sensitive_path.read_bytes()
        prepared = client.post(
            "/api/v1/sensitive/restore-points/"
            + committed.json()["restore_point_id"]
            + "/prepare-rollback",
            headers=headers,
        )
        prepared.raise_for_status()
        parent_path.write_bytes(b"MODEL=parent-model\n")
        rejected = client.post(
            "/api/v1/sensitive/rollbacks",
            headers=headers,
            json={
                "confirmation_receipt": prepared.json()[
                    "confirmation_receipt"
                ]
            },
        )

    assert rejected.status_code == 409
    assert rejected.json() == {
        "error": {"code": "ROLLBACK_CONFIRMATION_INVALID"}
    }
    assert sensitive_path.read_bytes() == current
    assert canary not in rejected.text


def test_sensitive_rollback_preview_redacts_a_secret_next_launch_fallback(
    tmp_path: Path,
) -> None:
    secret_canary = "synthetic-sensitive-rollback-fallback-secret"
    original = (
        f"OPENAI_API_KEY={secret_canary}\nMODEL=repo-model\n".encode("utf-8")
    )
    services, _ = _real_sensitive_http_services(
        tmp_path,
        repo_env_content=original,
        base_document={"llm": {"model": secret_canary}},
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _bootstrap_write_headers(client)
        preview = client.post(
            "/api/v1/sensitive/previews",
            headers=headers,
            json={
                "command": {
                    "kind": "clear_mapped_override",
                    "environment_variable": "MODEL",
                }
            },
        )
        preview.raise_for_status()
        committed = client.post(
            "/api/v1/sensitive/commits",
            headers=headers,
            json={"preview_id": preview.json()["preview_id"]},
        )
        committed.raise_for_status()
        prepared = client.post(
            "/api/v1/sensitive/restore-points/"
            + committed.json()["restore_point_id"]
            + "/prepare-rollback",
            headers=headers,
        )

    prepared.raise_for_status()
    assert secret_canary not in prepared.text
    change = prepared.json()["override_changes"][0]
    assert change["before_next_launch"] == "«REDACTED:OPENAI_API_KEY»"
    assert change["after_next_launch"] == "repo-model"


def test_real_sensitive_owner_maps_invalid_receipt_without_secret_disclosure(
    tmp_path: Path,
) -> None:
    services, sensitive_path = _real_sensitive_http_services(tmp_path)
    app = create_config_studio_app(services, _security_context())

    with TestClient(
        app,
        base_url="http://127.0.0.1:8765",
        raise_server_exceptions=False,
    ) as client:
        headers = _bootstrap_write_headers(client)
        preview = client.post(
            "/api/v1/sensitive/previews",
            headers=headers,
            json={
                "command": {
                    "kind": "clear_secret",
                    "slot": "openai_api_key",
                }
            },
        )
        preview.raise_for_status()
        response = client.post(
            "/api/v1/sensitive/commits",
            headers=headers,
            json={
                "preview_id": preview.json()["preview_id"],
                "confirmation_receipt": "wrong-sensitive-receipt",
            },
        )

    assert response.status_code == 409
    assert response.json() == {"error": {"code": "CONFIRMATION_REQUIRED"}}
    assert "synthetic-repo-secret" not in response.text
    assert sensitive_path.read_bytes() == (
        b"OPENAI_API_KEY=synthetic-repo-secret\n"
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_real_sensitive_permission_failure_maps_to_503_and_restores_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services, sensitive_path = _real_sensitive_http_services(tmp_path)
    original = sensitive_path.read_bytes()
    app = create_config_studio_app(services, _security_context())
    real_replace = os.replace

    def replace_then_relax_permissions(source: object, target: object) -> None:
        real_replace(source, target)
        if Path(target) == sensitive_path:
            sensitive_path.chmod(0o640)

    monkeypatch.setattr(
        "spica.config.document_transaction.os.replace",
        replace_then_relax_permissions,
    )
    candidate_canary = "synthetic-new-secret-never-on-wire"

    with TestClient(
        app,
        base_url="http://127.0.0.1:8765",
        raise_server_exceptions=False,
    ) as client:
        headers = _bootstrap_write_headers(client)
        preview = client.post(
            "/api/v1/sensitive/previews",
            headers=headers,
            json={
                "command": {
                    "kind": "set_secret",
                    "slot": "openai_api_key",
                    "value": candidate_canary,
                }
            },
        )
        preview.raise_for_status()
        response = client.post(
            "/api/v1/sensitive/commits",
            headers=headers,
            json={"preview_id": preview.json()["preview_id"]},
        )

    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "PERMISSION_HARDENING_FAILED"}
    }
    assert candidate_canary not in preview.text + response.text
    assert sensitive_path.read_bytes() == original
    assert (sensitive_path.stat().st_mode & 0o777) == 0o600


def test_mutating_json_rejects_duplicate_members_and_streamed_oversize_body() -> None:
    services = _FakeServices(app_write_enabled=True)
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        headers = {
            "Origin": "http://127.0.0.1:8765",
            "X-Spica-CSRF": bootstrap.json()["csrf_token"],
            "Content-Type": "application/json",
        }
        duplicate = client.post(
            "/api/v1/app/previews",
            headers=headers,
            content=b'{"operations":[],"operations":[]}',
        )

        def oversized_chunks():
            yield b'{"operations":[{"kind":"set","path":['
            for _ in range(70):
                yield b"x" * 1024

        oversized = client.post(
            "/api/v1/app/previews",
            headers=headers,
            content=oversized_chunks(),
        )

    for response in (duplicate, oversized):
        assert response.status_code == 400
        assert response.json() == {"error": {"code": "DOCUMENT_INVALID"}}
    assert services.app_preview_requests == []


@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "text/plain",
        "application/problem+json",
        "application/json; charset=iso-8859-1",
    ],
)
def test_json_body_routes_require_explicit_utf8_application_json(
    content_type: str | None,
) -> None:
    services = _FakeServices(app_write_enabled=True)
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _bootstrap_write_headers(client)
        if content_type is not None:
            headers["Content-Type"] = content_type
        response = client.post(
            "/api/v1/app/previews",
            headers=headers,
            content=(
                b'{"operations":[{"kind":"set","path":'
                b'[{"kind":"field","name":"max_tool_rounds"}],"value":3}]}'
            ),
        )

    assert response.status_code == 415
    assert response.json() == {"error": {"code": "JSON_CONTENT_TYPE_REQUIRED"}}
    assert services.app_preview_requests == []


def test_json_body_routes_accept_utf8_application_json_with_case_insensitive_mime() -> None:
    services = _FakeServices(app_write_enabled=True)
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _bootstrap_write_headers(client)
        headers["Content-Type"] = "Application/JSON; Charset=UTF-8"
        response = client.post(
            "/api/v1/app/previews",
            headers=headers,
            content=(
                b'{"operations":[{"kind":"set","path":'
                b'[{"kind":"field","name":"max_tool_rounds"}],"value":3}]}'
            ),
        )

    assert response.status_code == 200
    assert len(services.app_preview_requests) == 1


def test_overlay_write_uses_one_typed_fixed_key_preview_then_opaque_commit() -> None:
    disabled = _FakeServices(overlay_write_enabled=False)
    disabled_app = create_config_studio_app(disabled, _security_context())

    with TestClient(disabled_app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        gated = client.post(
            "/api/v1/overlay/previews",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-CSRF": bootstrap.json()["csrf_token"],
            },
            json={"key": "spica_voice_volume", "value": 0.72},
        )

    assert gated.status_code == 403
    assert disabled.overlay_preview_requests == []

    services = _FakeServices(overlay_write_enabled=True)
    app = create_config_studio_app(services, _security_context())
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        headers = {
            "Origin": "http://127.0.0.1:8765",
            "X-Spica-CSRF": bootstrap.json()["csrf_token"],
        }
        preview = client.post(
            "/api/v1/overlay/previews",
            headers=headers,
            json={"key": "spica_voice_volume", "value": 0.72},
        )
        committed = client.post(
            "/api/v1/overlay/commits",
            headers=headers,
            json={"preview_id": "overlay_preview_opaque"},
        )
        raw_candidate = client.post(
            "/api/v1/overlay/previews",
            headers=headers,
            json={"candidate": {"spica_voice_volume": 0.1}},
        )

    assert preview.status_code == 200
    assert preview.json() == {
        "preview_id": "overlay_preview_opaque",
        "key": "spica_voice_volume",
        "file_value_before": 0.5,
        "file_value_after": 0.72,
        "changed": True,
        "effect_policy": "next_spica_launch",
    }
    assert committed.json() == {
        "status": "saved",
        "effect_policy": "next_spica_launch",
        "restore_point_id": "O" * 24,
        "maintenance_code": None,
    }
    assert raw_candidate.status_code == 400
    command, session_id = services.overlay_preview_requests[0]
    assert command == OverlaySetValueRequest("spica_voice_volume", 0.72)
    assert services.overlay_commit_requests == [
        ("overlay_preview_opaque", session_id)
    ]
    assert "must-never-cross-the-api" not in preview.text
    assert "must-never-cross-the-api" not in committed.text


def test_unknown_overlay_owner_error_fails_closed_without_returning_details(
    tmp_path: Path,
) -> None:
    canary = "synthetic-private-path-and-secret-canary"

    class FutureOverlayOwner:
        def preview(self, _command: object, *, session_id: str) -> object:
            assert session_id
            raise OverlayOwnerError(
                f"FUTURE_OWNER_CODE_{canary}",
                f"/outside/private/{canary}",
            )

        def commit(self, _preview_id: str, *, session_id: str) -> object:
            raise AssertionError(f"unexpected commit for {session_id}")

    services = OwnerBackedConfigStudioServices(
        repo_root=tmp_path / "sandbox-repo",
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {}, layer="synthetic"
        ),
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        overlay_document=FutureOverlayOwner(),
        enabled_write_capabilities=frozenset({"overlay_write"}),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(
        app,
        base_url="http://127.0.0.1:8765",
        raise_server_exceptions=False,
    ) as client:
        response = client.post(
            "/api/v1/overlay/previews",
            headers=_bootstrap_write_headers(client),
            json={"key": "spica_voice_volume", "value": 0.72},
        )

    assert response.status_code == 500
    assert response.json() == {"error": {"code": "INTERNAL_ERROR"}}
    assert canary not in response.text
    assert "/outside/private" not in response.text


def test_sensitive_writes_are_gated_but_authenticated_status_remains_readable() -> None:
    services = _FakeServices()
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        headers = {
            "Origin": "http://127.0.0.1:8765",
            "X-Spica-CSRF": bootstrap.json()["csrf_token"],
        }
        status = client.get("/api/v1/sensitive/status")
        responses = (
            client.post(
                "/api/v1/sensitive/previews",
                headers=headers,
                json={
                    "command": {
                        "kind": "set_secret",
                        "slot": "openai_api_key",
                        "value": "synthetic-secret-canary",
                    }
                },
            ),
            client.post(
                "/api/v1/sensitive/previews/opaque/confirm-clear",
                headers=headers,
            ),
            client.post(
                "/api/v1/sensitive/commits",
                headers=headers,
                json={"preview_id": "opaque"},
            ),
            client.get("/api/v1/sensitive/restore-points"),
            client.post(
                "/api/v1/sensitive/restore-points/" + "S" * 24 + "/prepare-rollback",
                headers=headers,
            ),
            client.post(
                "/api/v1/sensitive/rollbacks",
                headers=headers,
                json={"confirmation_receipt": "rollback_receipt"},
            ),
        )

    assert status.status_code == 200
    assert status.json()["permission_health"] == "PRIVATE"
    assert services.sensitive_status_sessions
    for response in responses:
        assert response.status_code == 403
        assert response.json() == {"error": {"code": "CAPABILITY_UNAVAILABLE"}}
    assert services.sensitive_preview_requests == []
    assert services.sensitive_confirm_requests == []
    assert services.sensitive_commit_requests == []
    assert services.sensitive_restore_list_sessions == []
    assert services.sensitive_rollback_prepare_requests == []
    assert services.sensitive_rollback_requests == []


def test_real_sensitive_status_is_available_when_write_capability_is_disabled(
    tmp_path: Path,
) -> None:
    services, _ = _real_sensitive_http_services(tmp_path, enable_writes=False)
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        _bootstrap_write_headers(client)
        response = client.get("/api/v1/sensitive/status")

    assert services.capability_enabled("sensitive_write") is False
    assert response.status_code == 200
    assert response.json() == {
        "secret_slots": [
            {"slot": "openai_api_key", "configured": True},
            {"slot": "judge_api_key", "configured": False},
            {"slot": "bilibili_cookie", "configured": False},
            {"slot": "qbittorrent_password", "configured": False},
        ],
        "permission_health": "PRIVATE",
    }
    assert "synthetic-repo-secret" not in response.text


def test_real_sensitive_status_maps_an_unsafe_environment_refresh_to_503(
    tmp_path: Path,
) -> None:
    services, _ = _real_sensitive_http_services(tmp_path, enable_writes=False)
    parent_path = tmp_path / "sandbox-parent" / "xiaosan.env"
    os.link(parent_path, tmp_path / "sandbox-parent" / "parent-hardlink")
    app = create_config_studio_app(services, _security_context())

    with TestClient(
        app,
        base_url="http://127.0.0.1:8765",
        raise_server_exceptions=False,
    ) as client:
        _bootstrap_write_headers(client)
        response = client.get("/api/v1/sensitive/status")

    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "ENVIRONMENT_REFRESH_UNAVAILABLE"}
    }
    assert "synthetic-repo-secret" not in response.text


def test_real_sensitive_preview_maps_parent_refresh_failure_to_503(
    tmp_path: Path,
) -> None:
    services, _ = _real_sensitive_http_services(tmp_path)
    parent_path = tmp_path / "sandbox-parent" / "xiaosan.env"
    os.link(parent_path, tmp_path / "sandbox-parent" / "parent-hardlink")
    app = create_config_studio_app(services, _security_context())

    with TestClient(
        app,
        base_url="http://127.0.0.1:8765",
        raise_server_exceptions=False,
    ) as client:
        headers = _bootstrap_write_headers(client)
        response = client.post(
            "/api/v1/sensitive/previews",
            headers=headers,
            json={
                "command": {
                    "kind": "clear_mapped_override",
                    "environment_variable": "MODEL",
                }
            },
        )

    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "ENVIRONMENT_REFRESH_UNAVAILABLE"}
    }
    assert "synthetic-repo-secret" not in response.text


@pytest.mark.parametrize(
    "writer_entry",
    (
        "confirm_clear",
        "commit",
        "restore_points",
        "prepare_rollback",
        "rollback",
    ),
)
def test_every_real_sensitive_writer_maps_parent_refresh_failure_to_503(
    tmp_path: Path,
    writer_entry: str,
) -> None:
    secret_canary = "synthetic-refresh-failure-secret"
    services, _ = _real_sensitive_http_services(
        tmp_path,
        repo_env_content=(
            f"OPENAI_API_KEY={secret_canary}\nMODEL=synthetic-model\n".encode()
        ),
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(
        app,
        base_url="http://127.0.0.1:8765",
        raise_server_exceptions=False,
    ) as client:
        headers = _bootstrap_write_headers(client)
        if writer_entry == "confirm_clear":
            preview = client.post(
                "/api/v1/sensitive/previews",
                headers=headers,
                json={
                    "command": {
                        "kind": "clear_secret",
                        "slot": "openai_api_key",
                    }
                },
            )
            preview.raise_for_status()
            request_path = (
                "/api/v1/sensitive/previews/"
                + preview.json()["preview_id"]
                + "/confirm-clear"
            )
            request_payload = None
        elif writer_entry == "commit":
            preview = client.post(
                "/api/v1/sensitive/previews",
                headers=headers,
                json={
                    "command": {
                        "kind": "set_secret",
                        "slot": "judge_api_key",
                        "value": "synthetic-new-secret",
                    }
                },
            )
            preview.raise_for_status()
            request_path = "/api/v1/sensitive/commits"
            request_payload = {"preview_id": preview.json()["preview_id"]}
        elif writer_entry == "restore_points":
            request_path = "/api/v1/sensitive/restore-points"
            request_payload = None
        else:
            preview = client.post(
                "/api/v1/sensitive/previews",
                headers=headers,
                json={
                    "command": {
                        "kind": "clear_mapped_override",
                        "environment_variable": "MODEL",
                    }
                },
            )
            preview.raise_for_status()
            committed = client.post(
                "/api/v1/sensitive/commits",
                headers=headers,
                json={"preview_id": preview.json()["preview_id"]},
            )
            committed.raise_for_status()
            restore_point_id = committed.json()["restore_point_id"]
            request_path = (
                "/api/v1/sensitive/restore-points/"
                + restore_point_id
                + "/prepare-rollback"
            )
            request_payload = None
            if writer_entry == "rollback":
                prepared = client.post(request_path, headers=headers)
                prepared.raise_for_status()
                request_path = "/api/v1/sensitive/rollbacks"
                request_payload = {
                    "confirmation_receipt": prepared.json()[
                        "confirmation_receipt"
                    ]
                }

        parent_path = tmp_path / "sandbox-parent" / "xiaosan.env"
        os.link(parent_path, tmp_path / "sandbox-parent" / "parent-hardlink")
        if writer_entry == "restore_points":
            response = client.get(request_path)
        else:
            response = client.post(
                request_path,
                headers=headers,
                json=request_payload,
            )

    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "ENVIRONMENT_REFRESH_UNAVAILABLE"}
    }
    assert secret_canary not in response.text


def test_sensitive_status_and_set_secret_are_write_only_and_session_bound() -> None:
    secret_canary = "synthetic-secret-canary"
    services = _FakeServices(sensitive_write_enabled=True)
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        status = client.get("/api/v1/sensitive/status")
        preview = client.post(
            "/api/v1/sensitive/previews",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-CSRF": bootstrap.json()["csrf_token"],
            },
            json={
                "command": {
                    "kind": "set_secret",
                    "slot": "openai_api_key",
                    "value": secret_canary,
                }
            },
        )

    assert status.status_code == 200
    assert status.json() == {
        "secret_slots": [{"slot": "openai_api_key", "configured": True}],
        "permission_health": "PRIVATE",
    }
    assert preview.status_code == 200
    assert preview.json() == {
        "preview_id": "sensitive_preview_opaque",
        "command_kind": "set_secret",
        "target": "openai_api_key",
        "affected_fields": [],
        "winning_source_before": "repo_dotenv",
        "winning_source_after": "file",
        "still_shadowed": False,
        "permission_hardening": True,
        "changed": True,
        "secret_change": "will_set",
        "resolution_error_before": False,
        "resolution_error_after": False,
    }
    assert secret_canary not in status.text
    assert secret_canary not in preview.text
    command, preview_session = services.sensitive_preview_requests[0]
    assert command == SetSecret(slot="openai_api_key", value=secret_canary)
    assert secret_canary not in repr(command)
    assert preview_session != "session-token"
    assert services.sensitive_status_sessions == [preview_session]


def test_sensitive_clear_requires_confirmation_before_opaque_preview_commit() -> None:
    services = _FakeServices(sensitive_write_enabled=True)
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        headers = {
            "Origin": "http://127.0.0.1:8765",
            "X-Spica-CSRF": bootstrap.json()["csrf_token"],
        }
        preview = client.post(
            "/api/v1/sensitive/previews",
            headers=headers,
            json={
                "command": {
                    "kind": "clear_secret",
                    "slot": "openai_api_key",
                }
            },
        )
        confirmation = client.post(
            "/api/v1/sensitive/previews/sensitive_preview_opaque/confirm-clear",
            headers=headers,
        )
        committed = client.post(
            "/api/v1/sensitive/commits",
            headers=headers,
            json={
                "preview_id": "sensitive_preview_opaque",
                "confirmation_receipt": "clear_receipt_opaque",
            },
        )

    assert preview.status_code == 200
    assert confirmation.status_code == 200
    assert confirmation.json() == {
        "confirmation_receipt": "clear_receipt_opaque",
        "preview_id": "sensitive_preview_opaque",
        "command_kind": "clear_secret",
        "target": "openai_api_key",
        "secret_change": "will_clear",
    }
    assert committed.status_code == 200
    assert committed.json() == {
        "status": "saved",
        "restore_point_id": "S" * 24,
        "permission_health": "PRIVATE",
        "maintenance_code": None,
    }
    command, session_id = services.sensitive_preview_requests[0]
    assert command == ClearSecret(slot="openai_api_key")
    assert services.sensitive_confirm_requests == [
        ("sensitive_preview_opaque", session_id)
    ]
    assert services.sensitive_commit_requests == [
        ("sensitive_preview_opaque", "clear_receipt_opaque", session_id)
    ]
    assert "must-never-cross-the-api" not in confirmation.text
    assert "must-never-cross-the-api" not in committed.text


def test_mapped_override_clear_and_sensitive_rollback_expose_only_semantics() -> None:
    services = _FakeServices(
        sensitive_write_enabled=True,
        rollback_enabled=True,
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        headers = {
            "Origin": "http://127.0.0.1:8765",
            "X-Spica-CSRF": bootstrap.json()["csrf_token"],
        }
        preview = client.post(
            "/api/v1/sensitive/previews",
            headers=headers,
            json={
                "command": {
                    "kind": "clear_mapped_override",
                    "environment_variable": "RECENT_MEMORY_TURNS",
                }
            },
        )
        restore_points = client.get("/api/v1/sensitive/restore-points")
        prepared = client.post(
            "/api/v1/sensitive/restore-points/" + "S" * 24 + "/prepare-rollback",
            headers=headers,
        )
        rolled_back = client.post(
            "/api/v1/sensitive/rollbacks",
            headers=headers,
            json={"confirmation_receipt": "rollback_receipt_opaque"},
        )

    assert preview.status_code == 200
    assert preview.json()["before_next_launch"] == 4
    assert preview.json()["after_next_launch"] == 2
    assert restore_points.json() == {
        "restore_points": [
            {"restore_point_id": "S" * 24, "created_at_ns": 123}
        ]
    }
    assert prepared.status_code == 200
    assert prepared.json() == {
        "confirmation_receipt": "rollback_receipt_opaque",
        "restore_point_id": "S" * 24,
        "secret_changes": [
            {"slot": "openai_api_key", "change": "will_replace"}
        ],
        "override_changes": [],
        "unmanaged_content_changed": True,
        "unmanaged_change_count": 1,
        "permission_hardening": False,
        "resolution_error_before": False,
        "resolution_error_after": False,
    }
    assert rolled_back.json() == {
        "status": "restored",
        "restore_point_id": "T" * 24,
        "permission_health": "PRIVATE",
        "maintenance_code": None,
    }
    command, session_id = services.sensitive_preview_requests[0]
    assert command == ClearMappedOverride("RECENT_MEMORY_TURNS")
    assert services.sensitive_restore_list_sessions == [session_id]
    assert services.sensitive_rollback_prepare_requests == [
        ("S" * 24, session_id)
    ]
    assert services.sensitive_rollback_requests == [
        ("rollback_receipt_opaque", session_id)
    ]
    for response in (preview, restore_points, prepared, rolled_back):
        assert "must-never-cross-the-api" not in response.text


def test_dns_rebinding_host_alias_is_rejected_before_api_dispatch() -> None:
    app = create_config_studio_app(_FakeServices(), _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Host": "localhost:8765",
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )

    assert response.status_code == 403
    assert response.json() == {"error": {"code": "ORIGIN_REJECTED"}}


def test_bootstrap_token_is_consumed_once_under_concurrent_exchange() -> None:
    start = threading.Barrier(9)
    token_number = iter(range(100))

    def slow_token_factory() -> str:
        time.sleep(0.01)
        return f"synthetic-{next(token_number)}"

    context = SecurityContext(
        host="127.0.0.1",
        port=8765,
        bootstrap_token="one-time-bootstrap-token",
        clock=lambda: 100.0,
        token_factory=slow_token_factory,
    )
    results: list[object] = []

    def exchange() -> None:
        start.wait(timeout=2)
        results.append(context.exchange_bootstrap("one-time-bootstrap-token"))

    threads = [threading.Thread(target=exchange) for _ in range(8)]
    for thread in threads:
        thread.start()
    start.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert sum(result is not None for result in results) == 1


def test_prebound_socket_serves_security_policy_without_server_fingerprint() -> None:
    with LoopbackServer.bind(port=0, allow_test_port_zero=True) as bound:
        security = SecurityContext(
            host=bound.host,
            port=bound.port,
            bootstrap_token="one-time-bootstrap-token",
            clock=lambda: 100.0,
            token_factory=iter(["session-token", "csrf-token"]).__next__,
        )
        app = create_config_studio_app(_FakeServices(), security)
        uvicorn_server = uvicorn.Server(bound.uvicorn_config(app))
        thread = threading.Thread(
            target=uvicorn_server.run,
            kwargs={"sockets": [bound.socket]},
            daemon=True,
        )
        thread.start()
        try:
            deadline = time.monotonic() + 3.0
            while not uvicorn_server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            assert uvicorn_server.started

            connection = http.client.HTTPConnection(
                bound.host,
                bound.port,
                timeout=2,
            )
            try:
                connection.request("GET", "/api/v1/meta")
                response = connection.getresponse()
                response.read()
                headers = {
                    name.lower(): value for name, value in response.getheaders()
                }
            finally:
                connection.close()
        finally:
            uvicorn_server.should_exit = True
            thread.join(timeout=3)

    assert response.status == 401
    assert headers["cache-control"] == "no-store"
    assert "server" not in headers
    assert "date" not in headers
    assert not thread.is_alive()


def test_ambiguous_duplicate_session_cookie_is_rejected() -> None:
    app = create_config_studio_app(_FakeServices(), _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        ).raise_for_status()
        response = client.get(
            "/api/v1/meta",
            headers={
                "Cookie": (
                    "spica_config_studio_session=attacker-value; "
                    "spica_config_studio_session=session-token"
                )
            },
        )

    assert response.status_code == 401
    assert response.json() == {"error": {"code": "SESSION_REQUIRED"}}


def test_security_context_generates_redacted_session_tokens_by_default() -> None:
    bootstrap_token = "synthetic-bootstrap-token-opaque"
    context = SecurityContext(
        host="127.0.0.1",
        port=8765,
        bootstrap_token=bootstrap_token,
        clock=lambda: 100.0,
    )

    credentials = context.exchange_bootstrap(bootstrap_token)

    assert credentials is not None
    assert credentials.session_token
    assert credentials.csrf_token
    assert credentials.session_token != credentials.csrf_token
    assert credentials.session_token not in repr(credentials)
    assert credentials.csrf_token not in repr(credentials)
    assert bootstrap_token not in repr(context)


def test_security_context_issue_generates_the_startup_bootstrap_token() -> None:
    generated = iter(
        [
            "generated-bootstrap-token-opaque",
            "generated-session-token-opaque",
            "generated-csrf-token-opaque",
        ]
    )

    grant = SecurityContext.issue(
        host="127.0.0.1",
        port=8765,
        clock=lambda: 100.0,
        token_factory=generated.__next__,
    )
    credentials = grant.security_context.exchange_bootstrap(grant.bootstrap_token)

    assert grant.bootstrap_token == "generated-bootstrap-token-opaque"
    assert grant.bootstrap_token not in repr(grant)
    assert credentials is not None
    assert credentials.session_token == "generated-session-token-opaque"
    assert credentials.csrf_token == "generated-csrf-token-opaque"


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_interactive_api_documentation_is_not_served(path: str) -> None:
    app = create_config_studio_app(_FakeServices(), _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get(path)

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


def test_owner_exception_text_is_not_returned_by_the_api() -> None:
    class ExplodingServices(_FakeServices):
        def meta(self) -> dict[str, Any]:
            raise RuntimeError("synthetic-secret-canary")

    app = create_config_studio_app(ExplodingServices(), _security_context())

    with TestClient(
        app,
        base_url="http://127.0.0.1:8765",
        raise_server_exceptions=False,
    ) as client:
        client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        ).raise_for_status()
        response = client.get("/api/v1/meta")

    assert response.status_code == 500
    assert response.json() == {"error": {"code": "INTERNAL_ERROR"}}
    assert "synthetic-secret-canary" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_fixed_local_ui_and_background_load_without_a_session() -> None:
    app = create_config_studio_app(_FakeServices(), _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        index = client.get("/")
        stylesheet = client.get("/assets/studio.css")
        javascript = client.get("/assets/studio.js")
        background = client.get("/assets/background.png")
        arbitrary = client.get("/assets/../../data/config/app.yaml")

    assert index.status_code == 200
    assert "Spica Config Studio" in index.text
    assert stylesheet.status_code == 200
    assert 'url("/assets/background.png")' in stylesheet.text
    assert javascript.status_code == 200
    assert "/api/v1/session/bootstrap" in javascript.text
    assert background.status_code == 200
    assert len(background.content) == 10_281_151
    assert arbitrary.status_code == 404
    for response in (index, stylesheet, javascript, background):
        assert response.headers["cache-control"] == "no-store"


def test_light_self_check_start_builds_a_bounded_service_command() -> None:
    services = _FakeServices()
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        response = client.post(
            "/api/v1/self-check/jobs",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-CSRF": bootstrap.json()["csrf_token"],
            },
            json={"mode": "light"},
        )

    assert response.status_code == 202
    assert response.json() == {
        "job_id": "job_light",
        "mode": "light",
        "checks": ["config", "gpu", "secrets"],
        "status": "QUEUED",
        "duration_s": 0.0,
        "results": [],
        "progress": [],
        "error_code": None,
        "stderr_line_count": 0,
        "stderr_total_line_count": 0,
        "stderr_truncated": False,
    }
    assert len(services.self_check_requests) == 1
    command = services.self_check_requests[0]
    assert command.mode is SelfCheckMode.LIGHT
    assert command.only == ()
    assert command.llm is False
    assert command.include_disabled is False
    assert command.allow_model_downloads is False
    assert "must-never-cross-the-api" not in response.text


def test_self_check_capability_is_enforced_for_all_job_routes() -> None:
    services = _FakeServices(self_check_enabled=False)
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        headers = {
            "Origin": "http://127.0.0.1:8765",
            "X-Spica-CSRF": bootstrap.json()["csrf_token"],
        }
        responses = (
            client.post(
                "/api/v1/self-check/jobs",
                headers=headers,
                json={"mode": "light"},
            ),
            client.get("/api/v1/self-check/jobs"),
            client.get("/api/v1/self-check/jobs/opaque"),
            client.post(
                "/api/v1/self-check/jobs/opaque/cancel",
                headers=headers,
            ),
        )

    for response in responses:
        assert response.status_code == 403
        assert response.json() == {"error": {"code": "CAPABILITY_UNAVAILABLE"}}
    assert services.self_check_requests == []


def test_self_check_unsafe_latch_blocks_new_work_but_preserves_job_access() -> None:
    services = _FakeServices(
        self_check_enabled=False,
        self_check_jobs_enabled=True,
    )
    services.self_check_jobs["retained_terminal"] = {
        "job_id": "retained_terminal",
        "mode": "light",
        "checks": ["config"],
        "status": "CANCELLED",
        "duration_s": 0.25,
        "results": [],
        "progress": [],
        "error_code": None,
        "stderr_line_count": 0,
        "stderr_total_line_count": 0,
        "stderr_truncated": False,
    }
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        headers = {
            "Origin": "http://127.0.0.1:8765",
            "X-Spica-CSRF": bootstrap.json()["csrf_token"],
        }
        confirmation = client.post(
            "/api/v1/self-check/confirm",
            headers=headers,
            json={"mode": "full", "only": ["ocr"]},
        )
        start = client.post(
            "/api/v1/self-check/jobs",
            headers=headers,
            json={"mode": "light"},
        )
        collection = client.get("/api/v1/self-check/jobs")
        job = client.get("/api/v1/self-check/jobs/retained_terminal")
        cancelled = client.post(
            "/api/v1/self-check/jobs/retained_terminal/cancel",
            headers=headers,
        )

    for response in (confirmation, start):
        assert response.status_code == 403
        assert response.json() == {
            "error": {"code": "CAPABILITY_UNAVAILABLE"}
        }
    assert collection.status_code == 200
    assert collection.json()["jobs"][0]["job_id"] == "retained_terminal"
    assert job.status_code == 200
    assert job.json()["status"] == "CANCELLED"
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    assert services.self_check_requests == []


def test_self_check_http_projects_external_paths_before_start_get_and_list(
    tmp_path: Path,
) -> None:
    script = tmp_path / "sandbox-repo" / "scripts" / "self_check.py"
    script.parent.mkdir(parents=True)
    script.write_text("# synthetic self-check owner\n", encoding="utf-8")
    model_id = "Systran/faster-whisper-large-v3"
    ordinary_message = "ordinary diagnostic text"
    windows_path = r"C:\Users\synthetic-user\models\private\model.bin"
    unc_path = r"\\private-server\private-share\models\voice.pth"
    posix_path = "/opt/private-owner/models/ocr.bin"
    result_detail = {
        "model_id": model_id,
        "message": ordinary_message,
        "nested": {
            "locations": [
                f"model_path:{posix_path}",
                f"model_path:{windows_path}",
                f"model_path:{unc_path}",
            ]
        },
    }
    outcome = SelfCheckProcessOutcome(
        returncode=0,
        stdout=json.dumps(
            {
                "mode": "light",
                "results": [
                    {
                        "name": name,
                        "status": "PASS",
                        "reason": (
                            f"model_path:{windows_path}"
                            if name == "config"
                            else ""
                        ),
                        "detail": result_detail if name == "config" else {},
                        "duration_s": 0.0,
                    }
                    for name in LIGHT_CHECKS
                ],
                "exit_code": 0,
            }
        ),
        stderr="",
        cleanup_confirmed=True,
    )

    class _Process:
        containment_established = True

        def wait(self, _timeout_s: float) -> SelfCheckProcessOutcome:
            return outcome

        def cancel(self) -> bool:
            return True

        def stderr_snapshot(self) -> SelfCheckStderrSummary:
            return SelfCheckStderrSummary()

    class _Runner:
        def start(self, _argv: tuple[str, ...], _environment: Any) -> _Process:
            return _Process()

    service = SelfCheckService(
        script_path=script,
        job_manager=SelfCheckJobManager(runner=_Runner()),
        environment_inputs=lambda: SelfCheckEnvironmentInputs(
            environment_snapshot=EnvironmentSnapshot.from_mapping(
                {}, layer="synthetic"
            ),
            secrets=Secrets(),
        ),
    )
    services = ReadOnlyConfigStudioServices(
        repo_root=script.parents[2],
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {}, layer="synthetic"
        ),
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        self_check_service=service,
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        started = client.post(
            "/api/v1/self-check/jobs",
            headers=_bootstrap_write_headers(client),
            json={"mode": "light"},
        )
        assert started.status_code == 202
        job_id = started.json()["job_id"]
        deadline = time.monotonic() + 1.0
        while True:
            job = client.get(f"/api/v1/self-check/jobs/{job_id}")
            assert job.status_code == 200
            if job.json()["status"] == "PASS":
                break
            assert time.monotonic() < deadline
            time.sleep(0.001)
        collection = client.get("/api/v1/self-check/jobs")

    assert collection.status_code == 200
    expected_detail = {
        "model_id": model_id,
        "message": ordinary_message,
        "nested": {
            "locations": [
                "model_path:<external-path>",
                "model_path:<external-path>",
                "model_path:<external-path>",
            ]
        },
    }
    expected_reason = "model_path:<external-path>"
    get_result = job.json()["results"][0]
    list_result = collection.json()["jobs"][0]["results"][0]
    for result in (get_result, list_result):
        assert result["reason"] == expected_reason
        assert result["detail"] == expected_detail
    rendered = "\n".join((started.text, job.text, collection.text))
    for private_fragment in (
        windows_path,
        unc_path,
        posix_path,
        "synthetic-user",
        "private-server",
        "private-share",
        "model.bin",
        "voice.pth",
        "ocr.bin",
    ):
        assert private_fragment not in rendered
    assert model_id in rendered
    assert ordinary_message in rendered


def test_real_self_check_collection_returns_active_plus_twenty_terminals(
    tmp_path: Path,
) -> None:
    script = tmp_path / "sandbox-repo" / "scripts" / "self_check.py"
    script.parent.mkdir(parents=True)
    script.write_text("# synthetic self-check owner\n", encoding="utf-8")
    release_active = threading.Event()
    outcome = SelfCheckProcessOutcome(
        returncode=0,
        stdout=json.dumps(
            {
                "mode": "light",
                "results": [
                    {
                        "name": name,
                        "status": "PASS",
                        "reason": "",
                        "detail": {},
                        "duration_s": 0.0,
                    }
                    for name in LIGHT_CHECKS
                ],
                "exit_code": 0,
            }
        ),
        stderr="",
        cleanup_confirmed=True,
    )

    class _Process:
        containment_established = True

        def __init__(self, *, blocked: bool) -> None:
            self._blocked = blocked

        def wait(self, timeout_s: float) -> SelfCheckProcessOutcome:
            if self._blocked and not release_active.wait(timeout_s):
                raise TimeoutError
            return outcome

        def cancel(self) -> bool:
            release_active.set()
            return True

        def stderr_snapshot(self) -> SelfCheckStderrSummary:
            return SelfCheckStderrSummary()

    class _TwentyThenActiveRunner:
        def __init__(self) -> None:
            self.started = 0

        def start(self, _argv: tuple[str, ...], _environment: Any) -> _Process:
            self.started += 1
            return _Process(blocked=self.started == 21)

    runner = _TwentyThenActiveRunner()
    service = SelfCheckService(
        script_path=script,
        job_manager=SelfCheckJobManager(
            runner=runner,
            max_terminal_jobs=20,
        ),
        environment_inputs=lambda: SelfCheckEnvironmentInputs(
            environment_snapshot=EnvironmentSnapshot.from_mapping(
                {}, layer="synthetic"
            ),
            secrets=Secrets(),
        ),
    )
    services = ReadOnlyConfigStudioServices(
        repo_root=script.parents[2],
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {}, layer="synthetic"
        ),
        background_health_code=None,
        platform_capabilities=_platform(tmp_path),
        self_check_service=service,
    )
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = _bootstrap_write_headers(client)
        for _ in range(20):
            started = client.post(
                "/api/v1/self-check/jobs",
                headers=headers,
                json={"mode": "light"},
            )
            assert started.status_code == 202
            deadline = time.monotonic() + 1.0
            while service.get(started.json()["job_id"])["status"] != "PASS":
                assert time.monotonic() < deadline
                time.sleep(0.001)

        active = client.post(
            "/api/v1/self-check/jobs",
            headers=headers,
            json={"mode": "light"},
        )
        assert active.status_code == 202
        active_id = active.json()["job_id"]
        deadline = time.monotonic() + 1.0
        while service.get(active_id)["status"] != "RUNNING":
            assert time.monotonic() < deadline
            time.sleep(0.001)

        collection = client.get("/api/v1/self-check/jobs")
        cancelled = client.post(
            f"/api/v1/self-check/jobs/{active_id}/cancel",
            headers=headers,
        )

    assert collection.status_code == 200
    jobs = collection.json()["jobs"]
    assert len(jobs) == 21
    assert jobs[0]["job_id"] == active_id
    assert jobs[0]["status"] == "RUNNING"
    assert len(collection.content) <= 256 * 1024
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"


def test_heavy_self_check_rejects_client_consents_and_requires_server_receipt() -> None:
    services = _FakeServices()
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        missing_receipt = client.post(
            "/api/v1/self-check/jobs",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-CSRF": bootstrap.json()["csrf_token"],
            },
            json={"mode": "full", "only": ["ocr"]},
        )
        self_reported = client.post(
            "/api/v1/self-check/jobs",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-CSRF": bootstrap.json()["csrf_token"],
            },
            json={
                "mode": "full",
                "only": ["ocr"],
                "consents": ["full"],
            },
        )

    assert missing_receipt.status_code == 409
    assert missing_receipt.json() == {
        "error": {"code": "CONFIRMATION_REQUIRED"}
    }
    assert self_reported.status_code == 400
    assert self_reported.json() == {
        "error": {"code": "SELF_CHECK_PLAN_INVALID"}
    }
    assert services.self_check_requests == []
    assert services.self_check_confirmed_requests == []


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "light", "argv": ["python", "--force"]},
        {"mode": "light", "path": "/tmp/arbitrary"},
        {"mode": "light", "timeout": 1},
        {"mode": "light", "unknown": True},
        {"mode": "full", "llm": "false"},
        {"mode": "full", "only": "ocr"},
        {"mode": "full", "consents": "full"},
        {"mode": "arbitrary"},
        {"mode": "full", "consents": ["arbitrary"]},
    ],
)
def test_self_check_start_rejects_unknown_or_untyped_wire_fields(
    payload: dict[str, Any],
) -> None:
    services = _FakeServices()
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        response = client.post(
            "/api/v1/self-check/jobs",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-CSRF": bootstrap.json()["csrf_token"],
            },
            json=payload,
        )

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "SELF_CHECK_PLAN_INVALID"}}
    assert services.self_check_requests == []


def test_self_check_jobs_can_be_listed_read_and_cancelled_by_opaque_id() -> None:
    services = _FakeServices()
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        csrf_headers = {
            "Origin": "http://127.0.0.1:8765",
            "X-Spica-CSRF": bootstrap.json()["csrf_token"],
        }
        started = client.post(
            "/api/v1/self-check/jobs",
            headers=csrf_headers,
            json={"mode": "light"},
        )
        collection = client.get("/api/v1/self-check/jobs")
        job = client.get("/api/v1/self-check/jobs/job_light")
        cancelled = client.post(
            "/api/v1/self-check/jobs/job_light/cancel",
            headers=csrf_headers,
        )

    assert started.status_code == 202
    assert collection.status_code == 200
    assert collection.json()["jobs"][0]["job_id"] == "job_light"
    assert job.status_code == 200
    assert job.json()["job_id"] == "job_light"
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    for response in (collection, job, cancelled):
        assert "must-never-cross-the-api" not in response.text


def test_missing_self_check_job_maps_to_bounded_not_found() -> None:
    class MissingJobServices(_FakeServices):
        def get_self_check(self, job_id: str) -> dict[str, Any]:
            raise SelfCheckJobError("SELF_CHECK_JOB_NOT_FOUND")

    app = create_config_studio_app(MissingJobServices(), _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        response = client.get("/api/v1/self-check/jobs/missing_job")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "SELF_CHECK_JOB_NOT_FOUND"}
    }


def test_confirmed_heavy_self_check_uses_session_bound_server_receipt() -> None:
    services = _FakeServices()
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        command = {
            "mode": "full",
            "only": ["llm"],
            "llm": True,
            "include_disabled": True,
            "allow_model_downloads": True,
        }
        confirmation = client.post(
            "/api/v1/self-check/confirm",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-CSRF": bootstrap.json()["csrf_token"],
            },
            json={
                **command,
                "acknowledgements": {
                    "full": True,
                    "llm": True,
                    "include_disabled": True,
                    "model_downloads": True,
                },
            },
        )
        response = client.post(
            "/api/v1/self-check/jobs",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-CSRF": bootstrap.json()["csrf_token"],
            },
            json={**command, "confirmation_receipt": "self_check_receipt_opaque"},
        )

    assert confirmation.status_code == 200
    assert confirmation.json() == {
        "confirmation_receipt": "self_check_receipt_opaque",
        "expires_in_s": 120.0,
        "semantic": {
            "mode": "full",
            "checks": ["llm"],
            "llm": True,
            "include_disabled": True,
            "allow_model_downloads": True,
        },
    }
    assert "must-never-cross-the-api" not in confirmation.text
    assert response.status_code == 202
    (
        prepared_command,
        acknowledgements,
        session_id,
    ) = services.self_check_confirmation_requests[0]
    assert prepared_command.mode is SelfCheckMode.FULL
    assert prepared_command.only == ("llm",)
    assert prepared_command.llm is True
    assert prepared_command.include_disabled is True
    assert prepared_command.allow_model_downloads is True
    assert acknowledgements == SelfCheckAcknowledgements(
        full=True,
        llm=True,
        include_disabled=True,
        model_downloads=True,
    )
    confirmed_command, confirmed_session, receipt = (
        services.self_check_confirmed_requests[0]
    )
    assert confirmed_command == prepared_command
    assert confirmed_session == session_id
    assert session_id != "session-token"
    assert receipt == "self_check_receipt_opaque"


@pytest.mark.parametrize(
    ("failure", "status_code", "stable_code"),
    [
        (SelfCheckJobError("SELF_CHECK_BUSY"), 409, "SELF_CHECK_BUSY"),
        (
            SelfCheckJobError("SELF_CHECK_PLAN_INVALID"),
            400,
            "SELF_CHECK_PLAN_INVALID",
        ),
        (
            SelfCheckJobError("SELF_CHECK_MANAGER_UNSAFE"),
            503,
            "SELF_CHECK_UNAVAILABLE",
        ),
        (
            SelfCheckJobError("SELF_CHECK_MANAGER_SHUTDOWN"),
            503,
            "SELF_CHECK_UNAVAILABLE",
        ),
        (
            SelfCheckJobError("SELF_CHECK_JOB_ID_UNAVAILABLE"),
            503,
            "SELF_CHECK_UNAVAILABLE",
        ),
        (
            SelfCheckJobError("INVALID_CHILD_ENVIRONMENT"),
            503,
            "SELF_CHECK_UNAVAILABLE",
        ),
        (
            SelfCheckPlanError("CHECK_NOT_ALLOWLISTED"),
            400,
            "SELF_CHECK_PLAN_INVALID",
        ),
        (RuntimeError("raw-exception-canary"), 500, "INTERNAL_ERROR"),
    ],
)
def test_self_check_service_failures_map_to_bounded_stable_errors(
    failure: Exception,
    status_code: int,
    stable_code: str,
) -> None:
    class FailingServices(_FakeServices):
        def start_self_check(self, command: Any) -> dict[str, Any]:
            raise failure

    app = create_config_studio_app(FailingServices(), _security_context())

    with TestClient(
        app,
        base_url="http://127.0.0.1:8765",
        raise_server_exceptions=False,
    ) as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        response = client.post(
            "/api/v1/self-check/jobs",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-CSRF": bootstrap.json()["csrf_token"],
            },
            json={"mode": "light"},
        )

    assert response.status_code == status_code
    assert response.json() == {"error": {"code": stable_code}}
    assert "raw-exception-canary" not in response.text


def test_self_check_dto_strips_nested_process_output_fields() -> None:
    class RawOutputServices(_FakeServices):
        def start_self_check(self, command: Any) -> dict[str, Any]:
            job = super().start_self_check(command)
            job["results"] = [
                {
                    "name": "config",
                    "status": "PASS",
                    "detail": {"validated": True},
                    "reason": "",
                    "duration_s": 0.01,
                    "stdout": "nested-stdout-canary",
                    "stderr": "nested-stderr-canary",
                }
            ]
            job["progress"] = [
                {
                    "name": "config",
                    "status": "RUNNING",
                    "raw_line": "raw-progress-canary",
                }
            ]
            return job

    app = create_config_studio_app(RawOutputServices(), _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        response = client.post(
            "/api/v1/self-check/jobs",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-CSRF": bootstrap.json()["csrf_token"],
            },
            json={"mode": "light"},
        )

    assert response.status_code == 202
    assert response.json()["results"] == [
        {
            "name": "config",
            "status": "PASS",
            "detail": {"validated": True},
            "reason": "",
            "duration_s": 0.01,
        }
    ]
    assert response.json()["progress"] == [
        {"name": "config", "status": "RUNNING"}
    ]
    for canary in (
        "nested-stdout-canary",
        "nested-stderr-canary",
        "raw-progress-canary",
    ):
        assert canary not in response.text


def test_self_check_detail_applies_secret_key_fallback_redaction() -> None:
    canary = "synthetic-detail-secret-canary"

    class SecretDetailServices(_FakeServices):
        def start_self_check(self, command: Any) -> dict[str, Any]:
            job = super().start_self_check(command)
            job["results"] = [
                {
                    "name": "config",
                    "status": "PASS",
                    "detail": {
                        "validated": True,
                        "api_key": canary,
                        "nested": {"password": canary},
                    },
                    "reason": "",
                    "duration_s": 0.01,
                }
            ]
            return job

    app = create_config_studio_app(SecretDetailServices(), _security_context())
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        response = client.post(
            "/api/v1/self-check/jobs",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-CSRF": bootstrap.json()["csrf_token"],
            },
            json={"mode": "light"},
        )

    assert response.status_code == 202
    assert response.json()["results"][0]["detail"] == {
        "validated": True,
        "api_key": "<redacted>",
        "nested": {"password": "<redacted>"},
    }
    assert canary not in response.text


@pytest.mark.parametrize(
    "unsafe_detail",
    [
        object(),
        {"nested": {"a": {"b": {"c": {"d": {"e": "too-deep"}}}}}},
        {"items": list(range(200))},
        {"message": "x" * 4096},
    ],
)
def test_self_check_rejects_unbounded_or_non_json_service_detail(
    unsafe_detail: object,
) -> None:
    class UnsafeDetailServices(_FakeServices):
        def start_self_check(self, command: Any) -> dict[str, Any]:
            job = super().start_self_check(command)
            job["results"] = [
                {
                    "name": "config",
                    "status": "PASS",
                    "detail": unsafe_detail,
                    "reason": "",
                    "duration_s": 0.01,
                }
            ]
            return job

    app = create_config_studio_app(UnsafeDetailServices(), _security_context())
    with TestClient(
        app,
        base_url="http://127.0.0.1:8765",
        raise_server_exceptions=False,
    ) as client:
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        response = client.post(
            "/api/v1/self-check/jobs",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-CSRF": bootstrap.json()["csrf_token"],
            },
            json={"mode": "light"},
        )

    assert response.status_code == 500
    assert response.json() == {"error": {"code": "INTERNAL_ERROR"}}
    assert "too-deep" not in response.text


def test_self_check_routes_require_session_origin_and_csrf() -> None:
    services = _FakeServices()
    services.self_check_jobs["job_light"] = {
        "job_id": "job_light",
        "mode": "light",
        "checks": ["config"],
        "status": "RUNNING",
        "duration_s": 0.1,
        "results": [],
        "progress": [],
        "error_code": None,
        "stderr_line_count": 0,
        "stderr_total_line_count": 0,
        "stderr_truncated": False,
    }
    app = create_config_studio_app(services, _security_context())

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        unauthenticated = client.get("/api/v1/self-check/jobs")
        bootstrap = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "one-time-bootstrap-token",
            },
        )
        wrong_origin = client.post(
            "/api/v1/self-check/jobs/job_light/cancel",
            headers={
                "Origin": "http://localhost:8765",
                "X-Spica-CSRF": bootstrap.json()["csrf_token"],
            },
        )
        missing_csrf = client.post(
            "/api/v1/self-check/jobs/job_light/cancel",
            headers={"Origin": "http://127.0.0.1:8765"},
        )

    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == {"error": {"code": "SESSION_REQUIRED"}}
    assert wrong_origin.status_code == 403
    assert wrong_origin.json() == {"error": {"code": "ORIGIN_REJECTED"}}
    assert missing_csrf.status_code == 403
    assert missing_csrf.json() == {"error": {"code": "CSRF_INVALID"}}
    assert services.self_check_jobs["job_light"]["status"] == "RUNNING"
