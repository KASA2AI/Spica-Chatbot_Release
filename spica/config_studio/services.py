"""Production-owner composition for the independent Config Studio sidecar.

Authoring capabilities remain explicit constructor inputs backed by fixed
owners; callers cannot unlock a route merely by naming an unknown capability.
"""

from __future__ import annotations

import hmac
import json
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping, Protocol

from spica.config.document_transaction import DocumentTransactionError
from spica.config.env_roster import SECRETS_ENV_MAP
from spica.config.environment_snapshot import EnvironmentSnapshot
from spica.config.manager import ConfigManager
from spica.config.schema import AppConfig
from spica.config.secrets import EnvironmentRefreshError, LoadedSecrets, Secrets
from spica.ports.config_studio_platform import PlatformCapabilities
from spica.config_studio.app_document import (
    AppConfigDocument,
    AppDocumentError,
)
from spica.config_studio.authoring import AuthoringOperation
from spica.config_studio.catalog import ConfigCatalog
from spica.config_studio.managed_catalog import (
    ManagedDocumentCatalog,
    active_legacy_owner_prefixes,
    environment_only_settings,
    plugin_statuses,
    read_fixed_regular_file,
)
from spica.config_studio.overlay_contract import OverlayOwnerError, OverlaySetValue
from spica.config_studio.redaction import (
    enforce_catalog_wire_budget,
    redact_catalog_payload,
    redact_wire_value,
)
from spica.config_studio.schema_metadata import redact_external_schema_path
from spica.config_studio.self_check_service import (
    SelfCheckAcknowledgements,
    SelfCheckService,
)
from spica.config_studio.sensitive_env import (
    SensitiveEnvDocument,
    SensitiveEnvError,
)
from spica.config_studio.sensitive_status import (
    inspect_readonly_env_status,
    inspect_sensitive_env_status,
)
from spica.config_studio.yaml_owner import load_yaml_mapping
from spica.config_studio.paths import (
    FieldSegment,
    PathSegment,
)


_EMPTY_TRUNCATION = {
    "strings": 0,
    "collections": 0,
    "depth": 0,
    "unsupported": 0,
    "total_bytes": 0,
}
_CAPABILITIES = {
    "app_config_write": False,
    "overlay_write": False,
    "sensitive_write": False,
    "rollback": False,
    "self_check": False,
    "self_check_jobs": False,
}


class _OverlayDocumentOwner(Protocol):
    def preview(self, command: OverlaySetValue, *, session_id: str) -> Any: ...

    def commit(self, preview_id: str, *, session_id: str) -> Any: ...

    def restore_points(self) -> tuple[Any, ...]: ...

    def prepare_rollback(
        self,
        restore_point_id: str,
        *,
        session_id: str,
    ) -> Any: ...

    def rollback(self, receipt_token: str, *, session_id: str) -> Any: ...


class ConfigStudioServiceError(RuntimeError):
    """A bounded service failure whose message is a stable public code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ReadOnlyConfigStudioServices:
    """Resolve the fixed app document without constructing runtime services."""

    __slots__ = (
        "_background_health_code",
        "_config_path",
        "_environment_snapshot",
        "_environment_owner",
        "_manager",
        "_platform_capabilities",
        "_legacy_secret_canaries",
        "_repo_root",
        "_secrets",
        "_self_check_service",
        "_tainted_environment_names",
    )

    def __init__(
        self,
        *,
        repo_root: Path,
        environment_snapshot: EnvironmentSnapshot,
        background_health_code: str | None,
        platform_capabilities: PlatformCapabilities,
        secrets: Secrets | None = None,
        tainted_environment_names: tuple[str, ...] = (),
        legacy_secret_canaries: tuple[tuple[str, str], ...] = (),
        self_check_service: SelfCheckService | None = None,
        environment_owner: Callable[[], LoadedSecrets] | None = None,
    ) -> None:
        root = Path(repo_root).resolve()
        self._repo_root = root
        self._config_path = root / "data" / "config" / "app.yaml"
        self._manager = ConfigManager(config_path=self._config_path)
        if not isinstance(platform_capabilities, PlatformCapabilities):
            raise TypeError("platform_capabilities must be PlatformCapabilities")
        self._platform_capabilities = platform_capabilities
        self._environment_snapshot = environment_snapshot
        if environment_owner is not None and not callable(environment_owner):
            raise TypeError("environment_owner must be callable")
        self._environment_owner = environment_owner
        self._background_health_code = background_health_code
        self._secrets = secrets or Secrets()
        self._tainted_environment_names = tuple(tainted_environment_names)
        self._legacy_secret_canaries = tuple(legacy_secret_canaries)
        if self_check_service is not None and not isinstance(
            self_check_service, SelfCheckService
        ):
            raise TypeError("self_check_service must be SelfCheckService")
        self._self_check_service = self_check_service

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<fixed production owners>)"

    def meta(self) -> dict[str, Any]:
        (
            environment_snapshot,
            secrets,
            tainted_environment_names,
            _,
            secret_sources,
        ) = self._latest_environment()
        _, resolution_failed, managed_issues = self._catalog_state(
            environment_snapshot
        )
        sensitive_document = inspect_sensitive_env_status(
            self._repo_root,
            secrets,
            platform_capabilities=self._platform_capabilities,
        ).to_wire()
        sensitive_document["secret_sources"] = dict(secret_sources)
        parent_environment_document = inspect_readonly_env_status(
            self._repo_root.parent / "xiaosan.env",
            platform_capabilities=self._platform_capabilities,
        ).to_wire()
        issues: list[dict[str, str]] = []
        if self._background_health_code == "BACKGROUND_ASSET_INVALID":
            issues.append(
                {
                    "code": "BACKGROUND_ASSET_INVALID",
                    "message": "Decorative background failed integrity validation.",
                }
            )
        if resolution_failed:
            issues.append(
                {
                    "code": "CONFIG_RESOLUTION_ERROR",
                    "message": (
                        "app.yaml cannot be resolved; only recovery is available."
                    ),
                }
            )
        if tainted_environment_names:
            issues.append(
                {
                    "code": "ENVIRONMENT_VALUE_TAINTED",
                    "message": (
                        "A non-secret override resolved from secret material and "
                        "was quarantined."
                    ),
                }
            )
        permission_health = sensitive_document["permission_health"]
        if permission_health == "TOO_PERMISSIVE":
            issues.append(
                {
                    "code": "SENSITIVE_DOCUMENT_PERMISSION_TOO_PERMISSIVE",
                    "message": (
                        "The repository secret document is not owner-private."
                    ),
                }
            )
        elif permission_health not in {"PRIVATE", "MISSING"}:
            issues.append(
                {
                    "code": "SENSITIVE_DOCUMENT_PERMISSION_UNSAFE",
                    "message": (
                        "The repository secret document failed path or owner checks."
                    ),
                }
            )
        parse_health = sensitive_document["parse_health"]
        if parse_health == "INVALID":
            issues.append(
                {
                    "code": "SENSITIVE_DOCUMENT_PARSE_INVALID",
                    "message": "The repository secret document cannot be parsed.",
                }
            )
        elif parse_health == "UNAVAILABLE":
            issues.append(
                {
                    "code": "SENSITIVE_DOCUMENT_PARSE_UNAVAILABLE",
                    "message": (
                        "The repository secret document is unavailable for inspection."
                    ),
                }
            )
        if sensitive_document["legacy_entries"]:
            issues.append(
                {
                    "code": "LEGACY_ENV_ENTRY_PRESENT",
                    "message": (
                        "Retired environment entries remain in the repository document."
                    ),
                }
            )
        parent_permission = parent_environment_document["permission_health"]
        if parent_permission == "TOO_PERMISSIVE":
            issues.append(
                {
                    "code": "PARENT_ENV_PERMISSION_TOO_PERMISSIVE",
                    "message": "The parent environment document is not owner-private.",
                }
            )
        elif parent_permission not in {"PRIVATE", "MISSING"}:
            issues.append(
                {
                    "code": "PARENT_ENV_PERMISSION_UNSAFE",
                    "message": "The parent environment document failed safety checks.",
                }
            )
        parent_parse = parent_environment_document["parse_health"]
        if parent_parse == "INVALID":
            issues.append(
                {
                    "code": "PARENT_ENV_PARSE_INVALID",
                    "message": "The parent environment document cannot be parsed.",
                }
            )
        elif parent_parse == "UNAVAILABLE":
            issues.append(
                {
                    "code": "PARENT_ENV_PARSE_UNAVAILABLE",
                    "message": "The parent environment document is unavailable.",
                }
            )
        if parent_environment_document["legacy_entries"]:
            issues.append(
                {
                    "code": "PARENT_LEGACY_ENV_ENTRY_PRESENT",
                    "message": "Retired entries remain in the parent environment document.",
                }
            )
        issues.extend(managed_issues)
        return {
            "service": "spica-config-studio",
            "mode": "read_only",
            "runtime_truth": "unavailable",
            "effect_policy": "next_spica_launch",
            "capabilities": self._capabilities(),
            "sensitive_document": sensitive_document,
            "parent_environment_document": parent_environment_document,
            "health": {
                "recovery_only": resolution_failed,
                "issues": issues,
            },
        }

    def catalog(self) -> dict[str, Any]:
        loaded = self._latest_loaded_environment()
        environment_snapshot = loaded.environment_snapshot
        secrets = loaded.secrets
        legacy_secret_canaries = self._legacy_canaries_for(loaded)
        catalog, resolution_failed, _ = self._catalog_state(
            environment_snapshot
        )
        if resolution_failed:
            return {
                "fields": [],
                "truncation": dict(_EMPTY_TRUNCATION),
                "recovery_only": True,
            }
        catalog["recovery_only"] = False
        return enforce_catalog_wire_budget(
            redact_catalog_payload(
                catalog,
                secrets,
                legacy_secret_canaries,
                text_sanitizer=loaded.sanitize_secret_material,
            )
        )

    def capability_enabled(self, capability: str) -> bool:
        return bool(self._capabilities().get(capability, False))

    def capability_denial_code(self, capability: str) -> str | None:
        if self.capability_enabled(capability):
            return None
        if (
            capability in {"app_config_write", "overlay_write"}
            and self._platform_capabilities.os_family == "nt"
            and not self._platform_capabilities.managed_document_writes
        ):
            return "WRITES_UNVERIFIED_ON_WINDOWS"
        if (
            capability == "sensitive_write"
            and self._platform_capabilities.os_family == "nt"
            and not self._platform_capabilities.sensitive_document_writes
        ):
            return "SENSITIVE_WRITES_UNVERIFIED_ON_WINDOWS"
        return "CAPABILITY_UNAVAILABLE"

    def self_check_jobs_available(self) -> bool:
        """Keep retained jobs queryable after the new-start safety latch trips."""

        return self.capability_enabled("self_check_jobs")

    def preview_app(
        self,
        operations: tuple[AuthoringOperation, ...],
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")

    def commit_app_preview(
        self,
        preview_id: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")

    def list_app_restore_points(
        self,
        *,
        session_id: str,
    ) -> list[Mapping[str, Any]]:
        raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")

    def prepare_app_rollback(
        self,
        restore_point_id: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")

    def rollback_app(
        self,
        confirmation_receipt: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")

    def preview_overlay(
        self,
        command: object,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")

    def commit_overlay_preview(
        self,
        preview_id: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")

    def list_overlay_restore_points(
        self,
        *,
        session_id: str,
    ) -> list[Mapping[str, Any]]:
        raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")

    def prepare_overlay_rollback(
        self,
        restore_point_id: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")

    def rollback_overlay(
        self,
        confirmation_receipt: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")

    def sensitive_status(self, *, session_id: str) -> Mapping[str, Any]:
        inspected = inspect_sensitive_env_status(
            self._repo_root,
            self._secrets,
            platform_capabilities=self._platform_capabilities,
        ).to_wire()
        if inspected.get("permission_health") in {
            "DOCUMENT_UNSAFE",
            "WRONG_OWNER",
            "MULTIPLE_LINKS",
        }:
            return self._sensitive_status_wire(inspected)
        try:
            _, secrets, _, _, _ = self._latest_environment()
        except ConfigStudioServiceError:
            inspected = inspect_sensitive_env_status(
                self._repo_root,
                self._secrets,
                platform_capabilities=self._platform_capabilities,
            ).to_wire()
            if inspected.get("permission_health") in {
                "DOCUMENT_UNSAFE",
                "WRONG_OWNER",
                "MULTIPLE_LINKS",
            }:
                return self._sensitive_status_wire(inspected)
            raise
        status = inspect_sensitive_env_status(
            self._repo_root,
            secrets,
            platform_capabilities=self._platform_capabilities,
        ).to_wire()
        return self._sensitive_status_wire(status)

    @staticmethod
    def _sensitive_status_wire(status: Mapping[str, Any]) -> dict[str, Any]:
        slots = status.get("secret_slots", {})
        if not isinstance(slots, Mapping):
            raise ConfigStudioServiceError("INTERNAL_ERROR")
        return {
            "secret_slots": [
                {"slot": slot, "configured": bool(slots.get(slot, False))}
                for slot in SECRETS_ENV_MAP
            ],
            "permission_health": status["permission_health"],
        }

    def preview_sensitive(
        self,
        command: object,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")

    def confirm_sensitive_secret_clear(
        self,
        preview_id: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")

    def commit_sensitive_preview(
        self,
        preview_id: str,
        confirmation_receipt: str | None,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")

    def list_sensitive_restore_points(
        self,
        *,
        session_id: str,
    ) -> list[Mapping[str, Any]]:
        raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")

    def prepare_sensitive_rollback(
        self,
        restore_point_id: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")

    def rollback_sensitive(
        self,
        confirmation_receipt: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")

    def start_self_check(self, command: object) -> Mapping[str, Any]:
        if not self.capability_enabled("self_check"):
            raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")
        assert self._self_check_service is not None
        return self._self_check_service.start(command)

    def list_self_checks(self) -> list[Mapping[str, Any]]:
        if self._self_check_service is None:
            return []
        return self._self_check_service.list()

    def get_self_check(self, job_id: str) -> Mapping[str, Any]:
        if self._self_check_service is None:
            raise ConfigStudioServiceError("SELF_CHECK_JOB_NOT_FOUND")
        return self._self_check_service.get(job_id)

    def cancel_self_check(self, job_id: str) -> Mapping[str, Any]:
        if self._self_check_service is None:
            raise ConfigStudioServiceError("SELF_CHECK_JOB_NOT_FOUND")
        return self._self_check_service.cancel(job_id)

    def prepare_heavy_self_check(
        self,
        command: object,
        *,
        acknowledgements: SelfCheckAcknowledgements,
        session_id: str,
    ) -> Mapping[str, Any]:
        if not self.capability_enabled("self_check"):
            raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")
        assert self._self_check_service is not None
        return self._self_check_service.prepare_heavy(
            command,
            acknowledgements=acknowledgements,
            session_id=session_id,
        )

    def start_confirmed_self_check(
        self,
        command: object,
        *,
        session_id: str,
        confirmation_receipt: str,
    ) -> Mapping[str, Any]:
        if not self.capability_enabled("self_check"):
            raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")
        assert self._self_check_service is not None
        return self._self_check_service.start(
            command,
            session_id=session_id,
            confirmation_receipt=confirmation_receipt,
        )

    def shutdown(self) -> list[Mapping[str, Any]]:
        if self._self_check_service is None:
            return []
        return self._self_check_service.shutdown()

    def _capabilities(self) -> dict[str, bool]:
        capabilities = dict(_CAPABILITIES)
        capabilities["self_check"] = bool(
            self._self_check_service is not None
            and self._self_check_service.available
        )
        capabilities["self_check_jobs"] = self._self_check_service is not None
        return capabilities

    def _catalog_state(
        self,
        environment_snapshot: EnvironmentSnapshot,
    ) -> tuple[dict[str, Any], bool, list[dict[str, str]]]:
        try:
            raw_document = self._read_document()
            resolution = self._manager.resolve_snapshot(
                raw_document,
                environment_snapshot,
            )
            managed_snapshot = ManagedDocumentCatalog(
                repo_root=self._repo_root,
                resolution=resolution,
                platform_capabilities=self._platform_capabilities,
            ).snapshot()
            snapshot = ConfigCatalog(
                model_type=AppConfig,
                raw_document=raw_document,
                resolution=resolution,
                repo_root=self._repo_root,
                song_legacy_path=managed_snapshot.song_legacy_path,
                readonly_reasons=managed_snapshot.readonly_reasons,
            ).snapshot()
            payload = snapshot.to_wire(max_total_bytes=320 * 1024)
            payload["managed_documents"] = managed_snapshot.to_wire()
            payload["environment_only_settings"] = environment_only_settings(
                environment_snapshot
            )
            payload["plugin_statuses"] = plugin_statuses(
                repo_root=self._repo_root,
                resolution=resolution,
                legacy_owner_active=("plugins",)
                in managed_snapshot.readonly_reasons,
            )
            return (
                payload,
                False,
                [dict(issue) for issue in managed_snapshot.issues],
            )
        except (
            OSError,
            TypeError,
            ValueError,
            UnicodeError,
            RecursionError,
        ):
            return {}, True, []

    def _latest_loaded_environment(self) -> LoadedSecrets:
        if self._environment_owner is None:
            return LoadedSecrets(
                secrets=self._secrets,
                environment_snapshot=self._environment_snapshot,
                tainted_environment_names=self._tainted_environment_names,
            )
        try:
            loaded = self._environment_owner()
        except Exception as exc:  # noqa: BLE001 -- owner errors stay bounded
            raise ConfigStudioServiceError(
                "ENVIRONMENT_REFRESH_UNAVAILABLE"
            ) from None
        if not isinstance(loaded, LoadedSecrets):
            raise ConfigStudioServiceError("ENVIRONMENT_REFRESH_UNAVAILABLE")
        return loaded

    def _latest_environment(
        self,
    ) -> tuple[
        EnvironmentSnapshot,
        Secrets,
        tuple[str, ...],
        tuple[tuple[str, str], ...],
        tuple[tuple[str, str | None], ...],
    ]:
        loaded = self._latest_loaded_environment()
        return (
            loaded.environment_snapshot,
            loaded.secrets,
            loaded.tainted_environment_names,
            self._legacy_canaries_for(loaded),
            tuple(
                (slot, loaded.secret_source(slot))
                for slot in SECRETS_ENV_MAP
            ),
        )

    def _latest_app_environment(
        self,
    ) -> LoadedSecrets:
        return self._latest_loaded_environment()

    def _active_legacy_owner_prefixes(self) -> frozenset[str]:
        return active_legacy_owner_prefixes(self._repo_root)

    def _legacy_canaries_for(
        self,
        loaded: LoadedSecrets,
    ) -> tuple[tuple[str, str], ...]:
        if self._environment_owner is None:
            return self._legacy_secret_canaries
        return loaded.legacy_secret_canaries

    def _read_document(self) -> dict[str, Any]:
        read = read_fixed_regular_file(
            self._config_path,
            platform_capabilities=self._platform_capabilities,
        )
        if read.status == "missing":
            return {}
        if read.content is None:
            raise ValueError("managed app document is unavailable")
        return load_yaml_mapping(read.content, reject_aliases=False)


_OWNER_ERROR_NORMALIZATION = {
    "COMMAND_UNSUPPORTED": "DOCUMENT_INVALID",
    "CONFIRMATION_REQUIRED": "CONFIRMATION_REQUIRED",
    "DACL_UNVERIFIED": "DOCUMENT_UNSAFE",
    "DOCUMENT_BUSY": "DOCUMENT_BUSY",
    "DOCUMENT_CONFLICT": "DOCUMENT_CONFLICT",
    "DOCUMENT_INVALID": "DOCUMENT_INVALID",
    "DOCUMENT_UNSAFE": "DOCUMENT_UNSAFE",
    "DOTENV_INVALID": "DOTENV_INVALID",
    "MULTIPLE_LINKS": "DOCUMENT_UNSAFE",
    "NO_VALID_RESTORE_POINT": "NO_VALID_RESTORE_POINT",
    "OVERRIDE_NOT_MANAGED": "DOCUMENT_INVALID",
    "PERMISSION_HARDENING_FAILED": "PERMISSION_HARDENING_FAILED",
    "PREVIEW_EXPIRED": "CONFIRMATION_REQUIRED",
    "PREVIEW_INVALID": "CONFIRMATION_REQUIRED",
    "PREVIEW_UNAVAILABLE": "PREVIEW_UNAVAILABLE",
    "RECOVERY_ONLY": "RECOVERY_ONLY",
    "ROLLBACK_CONFIRMATION_EXPIRED": "CONFIRMATION_REQUIRED",
    "ROLLBACK_CONFIRMATION_INVALID": "ROLLBACK_CONFIRMATION_INVALID",
    "ROLLBACK_CONFIRMATION_UNAVAILABLE": "CONFIRMATION_UNAVAILABLE",
    "SECRET_CLEAR_CONFIRMATION_EXPIRED": "CONFIRMATION_REQUIRED",
    "SECRET_CLEAR_CONFIRMATION_INVALID": "CONFIRMATION_REQUIRED",
    "SECRET_CLEAR_CONFIRMATION_REQUIRED": "CONFIRMATION_REQUIRED",
    "SECRET_CLEAR_CONFIRMATION_UNAVAILABLE": "CONFIRMATION_UNAVAILABLE",
    "SECRET_SLOT_INVALID": "DOCUMENT_INVALID",
    "SECRET_VALUE_INVALID": "DOCUMENT_INVALID",
    "SECRET_VALUE_UNREPRESENTABLE": "DOCUMENT_INVALID",
    "SENSITIVE_BACKUP_UNSAFE": "DOCUMENT_UNSAFE",
    "SENSITIVE_WRITES_UNVERIFIED_ON_WINDOWS": (
        "SENSITIVE_WRITES_UNVERIFIED_ON_WINDOWS"
    ),
    "TYPE_MISMATCH": "DOCUMENT_INVALID",
    "UNKNOWN_FIELD": "UNKNOWN_FIELD",
    "VALUE_OUT_OF_RANGE": "DOCUMENT_INVALID",
    "WRITES_UNVERIFIED_ON_WINDOWS": "WRITES_UNVERIFIED_ON_WINDOWS",
    "WRONG_OWNER": "DOCUMENT_UNSAFE",
}


class OwnerBackedConfigStudioServices(ReadOnlyConfigStudioServices):
    """Explicitly gated composition of production managed-document owners.

    The fixed-path sidecar composition may inject verified owners here.  Each
    write surface still requires both its owner and an explicit capability, so
    merely constructing a document cannot unlock a route.
    """

    __slots__ = (
        "_app_document",
        "_enabled_write_capabilities",
        "_overlay_document",
        "_sensitive_document",
        "_sensitive_preview_clock",
        "_sensitive_preview_lock",
        "_sensitive_preview_ttl_seconds",
        "_sensitive_previews",
    )

    def __init__(
        self,
        *,
        repo_root: Path,
        environment_snapshot: EnvironmentSnapshot,
        background_health_code: str | None,
        platform_capabilities: PlatformCapabilities,
        app_document: AppConfigDocument | None = None,
        overlay_document: _OverlayDocumentOwner | None = None,
        sensitive_document: SensitiveEnvDocument | None = None,
        enabled_write_capabilities: frozenset[str] = frozenset(),
        sensitive_preview_clock: Callable[[], float] = time.monotonic,
        sensitive_preview_ttl_seconds: float = 60.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            repo_root=repo_root,
            environment_snapshot=environment_snapshot,
            background_health_code=background_health_code,
            platform_capabilities=platform_capabilities,
            **kwargs,
        )
        if app_document is not None and not isinstance(
            app_document, AppConfigDocument
        ):
            raise TypeError("app_document must be AppConfigDocument")
        if overlay_document is not None and not all(
            callable(getattr(overlay_document, name, None))
            for name in ("preview", "commit")
        ):
            raise TypeError("overlay_document must implement preview and commit")
        if sensitive_document is not None and not isinstance(
            sensitive_document, SensitiveEnvDocument
        ):
            raise TypeError("sensitive_document must be SensitiveEnvDocument")
        allowed = frozenset(
            {"app_config_write", "overlay_write", "sensitive_write", "rollback"}
        )
        enabled = frozenset(enabled_write_capabilities)
        if enabled - allowed:
            raise ValueError("unsupported owner-backed write capability")
        if "app_config_write" in enabled and app_document is None:
            raise ValueError("app_config_write requires an app document owner")
        if "overlay_write" in enabled and overlay_document is None:
            raise ValueError("overlay_write requires an overlay document owner")
        if "sensitive_write" in enabled and sensitive_document is None:
            raise ValueError("sensitive_write requires a sensitive document owner")
        rollback_lanes = (
            app_document is not None and "app_config_write" in enabled,
            overlay_document is not None and "overlay_write" in enabled,
            sensitive_document is not None and "sensitive_write" in enabled,
        )
        if "rollback" in enabled and not any(rollback_lanes):
            raise ValueError("rollback requires an enabled managed document owner")
        if (
            "rollback" in enabled
            and "overlay_write" in enabled
            and overlay_document is not None
            and not all(
                callable(getattr(overlay_document, name, None))
                for name in ("restore_points", "prepare_rollback", "rollback")
            )
        ):
            raise TypeError("overlay rollback owner is incomplete")
        if sensitive_preview_ttl_seconds <= 0:
            raise ValueError("sensitive preview TTL must be positive")
        self._app_document = app_document
        self._overlay_document = overlay_document
        self._sensitive_document = sensitive_document
        self._enabled_write_capabilities = enabled
        self._sensitive_preview_clock = sensitive_preview_clock
        self._sensitive_preview_lock = threading.Lock()
        self._sensitive_preview_ttl_seconds = sensitive_preview_ttl_seconds
        self._sensitive_previews: dict[str, tuple[str, float, Any]] = {}

    def _capabilities(self) -> dict[str, bool]:
        capabilities = super()._capabilities()
        capabilities["app_config_write"] = bool(
            self._app_document is not None
            and "app_config_write" in self._enabled_write_capabilities
        )
        capabilities["overlay_write"] = bool(
            self._overlay_document is not None
            and "overlay_write" in self._enabled_write_capabilities
        )
        capabilities["sensitive_write"] = bool(
            self._sensitive_document is not None
            and "sensitive_write" in self._enabled_write_capabilities
        )
        capabilities["rollback"] = bool(
            "rollback" in self._enabled_write_capabilities
            and (
                capabilities["app_config_write"]
                or capabilities["overlay_write"]
                or capabilities["sensitive_write"]
            )
        )
        return capabilities

    def meta(self) -> dict[str, Any]:
        payload = super().meta()
        if any(
            payload["capabilities"].get(capability) is True
            for capability in (
                "app_config_write",
                "overlay_write",
                "sensitive_write",
            )
        ):
            payload["mode"] = "owner_backed"
        return payload

    def preview_app(
        self,
        operations: tuple[AuthoringOperation, ...],
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        owner = self._require_app_owner()
        loaded = self._latest_loaded_environment()
        environment = loaded.environment_snapshot
        secrets = loaded.secrets
        legacy_canaries = self._legacy_canaries_for(loaded)
        forbidden_values = _secret_values(secrets, legacy_canaries)
        try:
            preview = owner.preview(
                operations,
                session_id=session_id,
                forbidden_values=forbidden_values,
                environment_snapshot=environment,
                loaded_environment=loaded,
                environment_guard=self._latest_app_environment,
                legacy_owner_guard=self._active_legacy_owner_prefixes,
            )
        except (AppDocumentError, DocumentTransactionError) as exc:
            _raise_bounded_owner_error(exc)
        payload = {
            "preview_id": preview.preview_id,
            "changed": preview.changed,
            "effect_policy": preview.effect_policy,
            "changes": [_app_change_wire(change) for change in preview.changes],
        }
        return _redact_app_preview_wire(
            payload,
            secrets=secrets,
            legacy_canaries=legacy_canaries,
            loaded_environment=loaded,
        )

    def commit_app_preview(
        self,
        preview_id: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        owner = self._require_app_owner()
        loaded = self._latest_loaded_environment()
        environment = loaded.environment_snapshot
        secrets = loaded.secrets
        legacy_canaries = self._legacy_canaries_for(loaded)
        try:
            committed = owner.commit(
                preview_id,
                session_id=session_id,
                forbidden_values=_secret_values(secrets, legacy_canaries),
                environment_snapshot=environment,
                loaded_environment=loaded,
                environment_guard=self._latest_app_environment,
                legacy_owner_guard=self._active_legacy_owner_prefixes,
            )
        except (AppDocumentError, DocumentTransactionError) as exc:
            _raise_bounded_owner_error(exc)
        return {
            "status": (
                "saved" if committed.restore_point_id is not None else "unchanged"
            ),
            "effect_policy": committed.effect_policy,
            "restore_point_id": committed.restore_point_id,
            "maintenance_code": committed.maintenance_code,
        }

    def list_app_restore_points(
        self,
        *,
        session_id: str,
    ) -> list[Mapping[str, Any]]:
        owner = self._require_app_rollback_owner()
        try:
            points = owner.restore_points()
        except (AppDocumentError, DocumentTransactionError) as exc:
            _raise_bounded_owner_error(exc)
        return _restore_points_wire(points)

    def prepare_app_rollback(
        self,
        restore_point_id: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        owner = self._require_app_rollback_owner()
        loaded = self._latest_loaded_environment()
        environment = loaded.environment_snapshot
        secrets = loaded.secrets
        legacy_canaries = self._legacy_canaries_for(loaded)
        try:
            confirmation = owner.prepare_rollback(
                restore_point_id,
                session_id=session_id,
                forbidden_values=_secret_values(secrets, legacy_canaries),
                environment_snapshot=environment,
                loaded_environment=loaded,
                environment_guard=self._latest_app_environment,
                legacy_owner_guard=self._active_legacy_owner_prefixes,
            )
        except (AppDocumentError, DocumentTransactionError) as exc:
            _raise_bounded_owner_error(exc)
        payload = _app_rollback_confirmation_wire(confirmation)
        return _redact_app_rollback_wire(
            payload,
            secrets=secrets,
            legacy_canaries=legacy_canaries,
            loaded_environment=loaded,
        )

    def rollback_app(
        self,
        confirmation_receipt: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        owner = self._require_app_rollback_owner()
        loaded = self._latest_loaded_environment()
        environment = loaded.environment_snapshot
        secrets = loaded.secrets
        legacy_canaries = self._legacy_canaries_for(loaded)
        try:
            committed = owner.rollback(
                confirmation_receipt,
                session_id=session_id,
                forbidden_values=_secret_values(secrets, legacy_canaries),
                environment_snapshot=environment,
                loaded_environment=loaded,
                environment_guard=self._latest_app_environment,
                legacy_owner_guard=self._active_legacy_owner_prefixes,
            )
        except (AppDocumentError, DocumentTransactionError) as exc:
            _raise_bounded_owner_error(exc)
        return _ordinary_rollback_commit_wire(committed)

    def preview_overlay(
        self,
        command: object,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        owner = self._require_overlay_owner()
        loaded = self._latest_loaded_environment()
        if not isinstance(command, OverlaySetValue):
            raise ConfigStudioServiceError("DOCUMENT_INVALID")
        try:
            preview = owner.preview(command, session_id=session_id)
        except (OverlayOwnerError, DocumentTransactionError) as exc:
            _raise_bounded_owner_error(exc)
        return {
            "preview_id": preview.preview_id,
            "key": preview.key,
            "file_value_before": _sanitize_owner_wire_value(
                preview.file_value_before,
                loaded,
            ),
            "file_value_after": _sanitize_owner_wire_value(
                preview.file_value_after,
                loaded,
            ),
            "changed": preview.changed,
            "effect_policy": preview.effect_policy,
        }

    def commit_overlay_preview(
        self,
        preview_id: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        owner = self._require_overlay_owner()
        try:
            committed = owner.commit(preview_id, session_id=session_id)
        except (OverlayOwnerError, DocumentTransactionError) as exc:
            _raise_bounded_owner_error(exc)
        return _commit_wire(committed)

    def list_overlay_restore_points(
        self,
        *,
        session_id: str,
    ) -> list[Mapping[str, Any]]:
        owner = self._require_overlay_rollback_owner()
        try:
            points = owner.restore_points()
        except (OverlayOwnerError, DocumentTransactionError) as exc:
            _raise_bounded_owner_error(exc)
        return _restore_points_wire(points)

    def prepare_overlay_rollback(
        self,
        restore_point_id: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        owner = self._require_overlay_rollback_owner()
        try:
            confirmation = owner.prepare_rollback(
                restore_point_id,
                session_id=session_id,
            )
        except (OverlayOwnerError, DocumentTransactionError) as exc:
            _raise_bounded_owner_error(exc)
        return _overlay_rollback_confirmation_wire(confirmation)

    def rollback_overlay(
        self,
        confirmation_receipt: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        owner = self._require_overlay_rollback_owner()
        try:
            committed = owner.rollback(
                confirmation_receipt,
                session_id=session_id,
            )
        except (OverlayOwnerError, DocumentTransactionError) as exc:
            _raise_bounded_owner_error(exc)
        return _ordinary_rollback_commit_wire(committed)

    def sensitive_status(self, *, session_id: str) -> Mapping[str, Any]:
        if self._sensitive_document is None:
            return super().sensitive_status(session_id=session_id)
        owner = self._require_sensitive_owner(write_required=False)
        try:
            status = owner.status()
        except EnvironmentRefreshError:
            raise ConfigStudioServiceError(
                "ENVIRONMENT_REFRESH_UNAVAILABLE"
            ) from None
        except (SensitiveEnvError, DocumentTransactionError) as exc:
            _raise_bounded_owner_error(exc)
        return {
            "secret_slots": [
                {"slot": item.slot, "configured": item.configured}
                for item in status.secret_slots
            ],
            "permission_health": status.permission_health,
        }

    def preview_sensitive(
        self,
        command: object,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        owner = self._require_sensitive_owner(write_required=True)
        loaded = self._latest_loaded_environment()
        try:
            preview = owner.preview(command, session_id=session_id)
        except (
            EnvironmentRefreshError,
            SensitiveEnvError,
            DocumentTransactionError,
        ) as exc:
            _raise_bounded_sensitive_owner_error(exc)
        with self._sensitive_preview_lock:
            now = self._sensitive_preview_clock()
            self._prune_sensitive_previews_locked(now)
            if len(self._sensitive_previews) >= 64:
                raise ConfigStudioServiceError("PREVIEW_UNAVAILABLE")
            self._sensitive_previews[preview.preview_id] = (
                session_id,
                now + self._sensitive_preview_ttl_seconds,
                preview,
            )
        payload = _sensitive_preview_wire(preview)
        return _redact_sensitive_preview_wire(
            payload,
            secrets=loaded.secrets,
            legacy_canaries=self._legacy_canaries_for(loaded),
        )

    def confirm_sensitive_secret_clear(
        self,
        preview_id: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        owner = self._require_sensitive_owner(write_required=True)
        self._latest_loaded_environment()
        preview = self._sensitive_preview(preview_id, session_id=session_id)
        try:
            confirmation = owner.prepare_secret_clear(
                preview,
                session_id=session_id,
            )
        except (
            EnvironmentRefreshError,
            SensitiveEnvError,
            DocumentTransactionError,
        ) as exc:
            _raise_bounded_sensitive_owner_error(exc)
        return {
            "confirmation_receipt": confirmation.receipt_token,
            "preview_id": preview.preview_id,
            "command_kind": preview.command_kind,
            "target": preview.target,
            "secret_change": preview.secret_change,
        }

    def commit_sensitive_preview(
        self,
        preview_id: str,
        confirmation_receipt: str | None,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        owner = self._require_sensitive_owner(write_required=True)
        self._latest_loaded_environment()
        preview = self._sensitive_preview(preview_id, session_id=session_id)
        try:
            committed = owner.commit(
                preview,
                session_id=session_id,
                confirmation_token=confirmation_receipt,
            )
        except (
            EnvironmentRefreshError,
            SensitiveEnvError,
            DocumentTransactionError,
        ) as exc:
            _raise_bounded_sensitive_owner_error(exc)
        with self._sensitive_preview_lock:
            self._sensitive_previews.pop(preview_id, None)
        return _sensitive_commit_wire(committed, status="saved")

    def list_sensitive_restore_points(
        self,
        *,
        session_id: str,
    ) -> list[Mapping[str, Any]]:
        owner = self._require_sensitive_rollback_owner()
        self._latest_loaded_environment()
        try:
            points = owner.restore_points()
        except (
            EnvironmentRefreshError,
            SensitiveEnvError,
            DocumentTransactionError,
        ) as exc:
            _raise_bounded_sensitive_owner_error(exc)
        return [
            {
                "restore_point_id": point.id,
                "created_at_ns": point.created_at_ns,
            }
            for point in points
        ]

    def prepare_sensitive_rollback(
        self,
        restore_point_id: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        owner = self._require_sensitive_rollback_owner()
        loaded = self._latest_loaded_environment()
        try:
            confirmation = owner.prepare_rollback(
                restore_point_id,
                session_id=session_id,
            )
        except (
            EnvironmentRefreshError,
            SensitiveEnvError,
            DocumentTransactionError,
        ) as exc:
            _raise_bounded_sensitive_owner_error(exc)
        payload = _sensitive_rollback_confirmation_wire(confirmation)
        return _redact_sensitive_rollback_wire(
            payload,
            secrets=loaded.secrets,
            legacy_canaries=self._legacy_canaries_for(loaded),
        )

    def rollback_sensitive(
        self,
        confirmation_receipt: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]:
        owner = self._require_sensitive_rollback_owner()
        self._latest_loaded_environment()
        try:
            committed = owner.rollback(
                confirmation_receipt,
                session_id=session_id,
            )
        except (
            EnvironmentRefreshError,
            SensitiveEnvError,
            DocumentTransactionError,
        ) as exc:
            _raise_bounded_sensitive_owner_error(exc)
        return _sensitive_commit_wire(committed, status="restored")

    def _require_app_owner(self) -> AppConfigDocument:
        if not self.capability_enabled("app_config_write"):
            raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")
        assert self._app_document is not None
        return self._app_document

    def _require_app_rollback_owner(self) -> AppConfigDocument:
        if not (
            self.capability_enabled("app_config_write")
            and self.capability_enabled("rollback")
        ):
            raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")
        assert self._app_document is not None
        return self._app_document

    def _require_overlay_owner(self) -> _OverlayDocumentOwner:
        if not self.capability_enabled("overlay_write"):
            raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")
        assert self._overlay_document is not None
        return self._overlay_document

    def _require_overlay_rollback_owner(self) -> _OverlayDocumentOwner:
        if not (
            self.capability_enabled("overlay_write")
            and self.capability_enabled("rollback")
        ):
            raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")
        assert self._overlay_document is not None
        return self._overlay_document

    def _require_sensitive_owner(
        self,
        *,
        write_required: bool,
    ) -> SensitiveEnvDocument:
        if write_required and not self.capability_enabled("sensitive_write"):
            raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")
        if self._sensitive_document is None:
            raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")
        return self._sensitive_document

    def _require_sensitive_rollback_owner(self) -> SensitiveEnvDocument:
        if not (
            self.capability_enabled("sensitive_write")
            and self.capability_enabled("rollback")
        ):
            raise ConfigStudioServiceError("CAPABILITY_UNAVAILABLE")
        assert self._sensitive_document is not None
        return self._sensitive_document

    def _sensitive_preview(self, preview_id: str, *, session_id: str) -> Any:
        with self._sensitive_preview_lock:
            self._prune_sensitive_previews_locked(self._sensitive_preview_clock())
            stored = self._sensitive_previews.get(preview_id)
        if (
            stored is None
            or not hmac.compare_digest(stored[0], session_id)
        ):
            raise ConfigStudioServiceError("CONFIRMATION_REQUIRED")
        return stored[2]

    def _prune_sensitive_previews_locked(self, now: float) -> None:
        self._sensitive_previews = {
            preview_id: stored
            for preview_id, stored in self._sensitive_previews.items()
            if stored[1] > now
        }


def _field_path_wire(segments: tuple[PathSegment, ...]) -> list[dict[str, Any]]:
    try:
        return [segment.to_wire() for segment in segments]
    except (AttributeError, TypeError):
        raise ConfigStudioServiceError("INTERNAL_ERROR") from None


def _secret_values(
    secrets: Secrets,
    legacy_canaries: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    values = (
        secrets.openai_api_key,
        secrets.judge_api_key,
        secrets.bilibili_cookie,
        secrets.qbittorrent_password,
        *(value for _, value in legacy_canaries),
    )
    return tuple(
        sorted(
            {value for value in values if isinstance(value, str) and value},
            key=len,
            reverse=True,
        )
    )


def _app_change_wire(change: Any) -> dict[str, Any]:
    field_names = _schema_field_names(change.path.segments)
    return {
        "path": _field_path_wire(change.path.segments),
        "display_path": change.display_path,
        "file_value_before": redact_external_schema_path(
            field_names, change.file_value_before
        ),
        "file_value_after": redact_external_schema_path(
            field_names, change.file_value_after
        ),
        "next_launch_value_before": redact_external_schema_path(
            field_names, change.next_launch_value_before
        ),
        "next_launch_value_after": redact_external_schema_path(
            field_names, change.next_launch_value_after
        ),
        "source_before": change.source_before,
        "source_after": change.source_after,
        "file_value_shadowed": change.file_value_shadowed,
        "semantic_warning": change.semantic_warning,
    }


def _redact_app_preview_wire(
    payload: dict[str, Any],
    *,
    secrets: Secrets,
    legacy_canaries: tuple[tuple[str, str], ...],
    loaded_environment: LoadedSecrets,
) -> dict[str, Any]:
    changes: list[dict[str, Any]] = []
    for original in payload["changes"]:
        change = dict(original)
        change["display_path"] = _sanitize_owner_wire_value(
            redact_wire_value(
                change["display_path"], secrets, legacy_canaries
            ),
            loaded_environment,
        )
        change["path"] = [
            {
                key: (
                    _sanitize_owner_wire_value(
                        redact_wire_value(value, secrets, legacy_canaries),
                        loaded_environment,
                    )
                    if key == "key"
                    else value
                )
                for key, value in segment.items()
            }
            for segment in change["path"]
        ]
        for field_name in (
            "file_value_before",
            "file_value_after",
            "next_launch_value_before",
            "next_launch_value_after",
        ):
            change[field_name] = _sanitize_owner_wire_value(
                redact_wire_value(
                    change[field_name], secrets, legacy_canaries
                ),
                loaded_environment,
            )
        changes.append(change)
    return {**payload, "changes": changes}


def _redact_app_rollback_wire(
    payload: dict[str, Any],
    *,
    secrets: Secrets,
    legacy_canaries: tuple[tuple[str, str], ...],
    loaded_environment: LoadedSecrets,
) -> dict[str, Any]:
    result = dict(payload)
    for field_name in ("changed_fields", "next_launch_changed_fields"):
        result[field_name] = [
            _sanitize_owner_wire_value(
                redact_wire_value(value, secrets, legacy_canaries),
                loaded_environment,
            )
            for value in result[field_name]
        ]
    return result


def _sanitize_owner_wire_value(
    value: Any,
    loaded_environment: LoadedSecrets,
) -> Any:
    if isinstance(value, str):
        return loaded_environment.sanitize_secret_material(value)
    if isinstance(value, Mapping):
        return {
            loaded_environment.sanitize_secret_material(str(key)):
            _sanitize_owner_wire_value(child, loaded_environment)
            for key, child in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [
            _sanitize_owner_wire_value(child, loaded_environment)
            for child in value
        ]
    if value is None:
        return None
    if type(value) in (bool, int, float):
        try:
            canonical = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            return "<unsupported-value>"
        sanitized = loaded_environment.sanitize_secret_material(canonical)
        return value if sanitized == canonical else sanitized
    return "<unsupported-value>"


def _schema_field_names(segments: tuple[object, ...]) -> tuple[str, ...]:
    if not all(isinstance(segment, FieldSegment) for segment in segments):
        return ()
    return tuple(segment.name for segment in segments)


def _redact_affected_field_value(
    affected_fields: tuple[str, ...],
    value: Any,
) -> Any:
    redacted = value
    for field_path in affected_fields:
        redacted = redact_external_schema_path(
            tuple(field_path.split(".")),
            redacted,
        )
    return redacted


def _commit_wire(committed: Any) -> dict[str, Any]:
    restore_point_id = committed.restore_point_id
    return {
        "status": "saved" if restore_point_id is not None else "unchanged",
        "effect_policy": committed.effect_policy,
        "restore_point_id": restore_point_id,
        "maintenance_code": committed.maintenance_code,
    }


def _restore_points_wire(points: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [
        {
            "restore_point_id": point.id,
            "created_at_ns": point.created_at_ns,
        }
        for point in points
    ]


def _app_rollback_confirmation_wire(confirmation: Any) -> dict[str, Any]:
    preview = confirmation.preview
    return {
        "confirmation_receipt": confirmation.receipt_token,
        "restore_point_id": preview.restore_point_id,
        "effect_policy": preview.effect_policy,
        "changed_fields": list(preview.changed_fields),
        "next_launch_changed_fields": list(preview.next_launch_changed_fields),
        "unmanaged_content_changed": preview.unmanaged_content_changed,
        "unmanaged_change_count": preview.unmanaged_change_count,
        "resolution_error_before": preview.resolution_error_before,
        "resolution_error_after": preview.resolution_error_after,
    }


def _overlay_rollback_confirmation_wire(confirmation: Any) -> dict[str, Any]:
    preview = confirmation.preview
    return {
        "confirmation_receipt": confirmation.receipt_token,
        "restore_point_id": preview.restore_point_id,
        "effect_policy": preview.effect_policy,
        "changed_fields": list(preview.changed_fields),
        "unmanaged_content_changed": preview.unmanaged_content_changed,
        "unmanaged_change_count": preview.unmanaged_change_count,
        "resolution_error_before": preview.resolution_error_before,
        "resolution_error_after": preview.resolution_error_after,
    }


def _ordinary_rollback_commit_wire(committed: Any) -> dict[str, Any]:
    return {
        "status": "restored",
        "effect_policy": committed.effect_policy,
        "restore_point_id": committed.restore_point_id,
        "maintenance_code": committed.maintenance_code,
    }


def _sensitive_preview_wire(preview: Any) -> dict[str, Any]:
    return {
        "preview_id": preview.preview_id,
        "command_kind": preview.command_kind,
        "target": preview.target,
        "affected_fields": list(preview.affected_fields),
        "before_next_launch": _redact_affected_field_value(
            preview.affected_fields,
            preview.before_next_launch,
        ),
        "after_next_launch": _redact_affected_field_value(
            preview.affected_fields,
            preview.after_next_launch,
        ),
        "winning_source_before": preview.winning_source_before,
        "winning_source_after": preview.winning_source_after,
        "still_shadowed": preview.still_shadowed,
        "permission_hardening": preview.permission_hardening,
        "changed": preview.changed,
        "secret_change": preview.secret_change,
        "resolution_error_before": preview.resolution_error_before,
        "resolution_error_after": preview.resolution_error_after,
    }


def _sensitive_commit_wire(committed: Any, *, status: str) -> dict[str, Any]:
    if status == "saved" and committed.restore_point_id is None:
        status = "unchanged"
    return {
        "status": status,
        "restore_point_id": committed.restore_point_id,
        "permission_health": committed.permission_health,
        "maintenance_code": committed.maintenance_code,
    }


def _redact_sensitive_preview_wire(
    payload: dict[str, Any],
    *,
    secrets: Secrets,
    legacy_canaries: tuple[tuple[str, str], ...],
) -> dict[str, Any]:
    result = dict(payload)
    for field_name in ("before_next_launch", "after_next_launch"):
        result[field_name] = redact_wire_value(
            result[field_name], secrets, legacy_canaries
        )
    return result


def _redact_sensitive_rollback_wire(
    payload: dict[str, Any],
    *,
    secrets: Secrets,
    legacy_canaries: tuple[tuple[str, str], ...],
) -> dict[str, Any]:
    result = dict(payload)
    result["override_changes"] = [
        {
            **change,
            "before_next_launch": redact_wire_value(
                change["before_next_launch"], secrets, legacy_canaries
            ),
            "after_next_launch": redact_wire_value(
                change["after_next_launch"], secrets, legacy_canaries
            ),
        }
        for change in payload["override_changes"]
    ]
    return result


def _sensitive_rollback_confirmation_wire(confirmation: Any) -> dict[str, Any]:
    preview = confirmation.preview
    return {
        "confirmation_receipt": confirmation.receipt_token,
        "restore_point_id": preview.restore_point_id,
        "secret_changes": [
            {"slot": change.slot, "change": change.change}
            for change in preview.secret_changes
        ],
        "override_changes": [
            {
                "environment_variable": change.environment_variable,
                "affected_fields": list(change.affected_fields),
                "before_next_launch": _redact_affected_field_value(
                    change.affected_fields,
                    change.before_next_launch,
                ),
                "after_next_launch": _redact_affected_field_value(
                    change.affected_fields,
                    change.after_next_launch,
                ),
                "winning_source_before": change.winning_source_before,
                "winning_source_after": change.winning_source_after,
                "still_shadowed": change.still_shadowed,
            }
            for change in preview.override_changes
        ],
        "unmanaged_content_changed": preview.unmanaged_content_changed,
        "unmanaged_change_count": preview.unmanaged_change_count,
        "permission_hardening": preview.permission_hardening,
        "resolution_error_before": preview.resolution_error_before,
        "resolution_error_after": preview.resolution_error_after,
    }


def _raise_bounded_owner_error(exc: Exception) -> None:
    code = getattr(exc, "code", None)
    normalized = (
        _OWNER_ERROR_NORMALIZATION.get(code)
        if isinstance(code, str)
        else None
    )
    if normalized is None:
        raise ConfigStudioServiceError("INTERNAL_ERROR") from None
    raise ConfigStudioServiceError(normalized) from None


def _raise_bounded_sensitive_owner_error(exc: Exception) -> None:
    if isinstance(exc, EnvironmentRefreshError):
        raise ConfigStudioServiceError(
            "ENVIRONMENT_REFRESH_UNAVAILABLE"
        ) from None
    _raise_bounded_owner_error(exc)


__all__ = [
    "ConfigStudioServiceError",
    "OwnerBackedConfigStudioServices",
    "ReadOnlyConfigStudioServices",
]
