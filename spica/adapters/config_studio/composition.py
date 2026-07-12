"""Fixed-path adapter composition for the Config Studio sidecar."""

from __future__ import annotations

from pathlib import Path
import stat
from typing import Callable

from spica.config.document_transaction import DocumentTransactionError
from spica.config.secrets import LoadedSecrets
from spica.ports.config_studio_platform import PlatformCapabilities
from spica.config_studio.app_document import (
    AppConfigDocument,
    AppDocumentError,
    RuamelRoundTripEditor,
)
from spica.config_studio.managed_catalog import read_fixed_regular_file
from spica.config_studio.self_check_service import SelfCheckService
from spica.config_studio.sensitive_env import SensitiveEnvDocument
from spica.config_studio.services import (
    OwnerBackedConfigStudioServices,
    ReadOnlyConfigStudioServices,
)
from spica.config_studio.yaml_owner import load_yaml_mapping
from spica.config_studio.overlay_document import OverlayConfigDocument


def _private_state_tree_is_safe(
    state_root: Path,
    *,
    platform: PlatformCapabilities,
) -> bool:
    """Inspect existing transaction state without creating or repairing it."""

    if not platform.posix_permissions or platform.user_id is None:
        return False
    try:
        parent_stat = state_root.parent.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        return False
    else:
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid != platform.user_id
        ):
            return False
    for path in (state_root, state_root / "backups", state_root / "locks"):
        try:
            path_stat = path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return False
        if (
            not stat.S_ISDIR(path_stat.st_mode)
            or path_stat.st_uid != platform.user_id
            or stat.S_IMODE(path_stat.st_mode) != 0o700
        ):
            return False
    return True


def create_production_config_studio_services(
    *,
    repo_root: str | Path,
    loaded_environment: LoadedSecrets,
    environment_owner: Callable[[], LoadedSecrets],
    platform_capabilities: PlatformCapabilities,
    background_health_code: str | None,
    self_check_service: SelfCheckService | None,
) -> ReadOnlyConfigStudioServices:
    """Derive capabilities from verified adapters and fixed production owners."""

    root = Path(repo_root).resolve()
    if not isinstance(loaded_environment, LoadedSecrets):
        raise TypeError("loaded_environment must be LoadedSecrets")
    if not callable(environment_owner):
        raise TypeError("environment_owner must be callable")
    if not isinstance(platform_capabilities, PlatformCapabilities):
        raise TypeError("platform_capabilities must be PlatformCapabilities")

    def latest_app_document() -> dict[str, object]:
        read = read_fixed_regular_file(
            root / "data" / "config" / "app.yaml",
            platform_capabilities=platform_capabilities,
        )
        if read.status == "missing":
            return {}
        if read.content is None:
            raise ValueError("APP_DOCUMENT_UNAVAILABLE")
        return load_yaml_mapping(read.content, reject_aliases=False)

    service_options = {
        "repo_root": root,
        "environment_snapshot": loaded_environment.environment_snapshot,
        "platform_capabilities": platform_capabilities,
        "secrets": loaded_environment.secrets,
        "tainted_environment_names": loaded_environment.tainted_environment_names,
        "legacy_secret_canaries": loaded_environment.legacy_secret_canaries,
        "background_health_code": background_health_code,
        "self_check_service": self_check_service,
        "environment_owner": environment_owner,
    }
    app_document = None
    overlay_document = None
    sensitive_document = None
    enabled: set[str] = set()
    state_root = root / "spica_data" / "config_studio"
    backup_root = state_root / "backups"
    state_is_safe = _private_state_tree_is_safe(
        state_root,
        platform=platform_capabilities,
    )

    if platform_capabilities.managed_document_writes and state_is_safe:
        try:
            app_document = AppConfigDocument(
                root / "data" / "config" / "app.yaml",
                backup_root=backup_root,
                environment_snapshot=loaded_environment.environment_snapshot,
                environment_snapshot_owner=(
                    lambda: environment_owner().environment_snapshot
                ),
                round_trip_editor=RuamelRoundTripEditor(),
                platform_capabilities=platform_capabilities,
            )
            app_document.status()
        except (
            AppDocumentError,
            DocumentTransactionError,
            OSError,
            TypeError,
            ValueError,
        ):
            app_document = None
        if app_document is not None:
            enabled.add("app_config_write")

        try:
            overlay_document = OverlayConfigDocument(
                root / "ui" / "overlay_config.json",
                backup_root=backup_root,
                platform_capabilities=platform_capabilities,
            )
            overlay_document.status()
        except (DocumentTransactionError, OSError, TypeError, ValueError):
            overlay_document = None
        if overlay_document is not None:
            enabled.add("overlay_write")

    if (
        platform_capabilities.managed_document_writes
        and platform_capabilities.sensitive_document_writes
        and state_is_safe
    ):
        try:
            sensitive_document = SensitiveEnvDocument(
                root / "xiaosan.env",
                backup_root=backup_root,
                environment_owner=loaded_environment,
                base_document_owner=latest_app_document,
                platform_capabilities=platform_capabilities,
            )
            sensitive_health = sensitive_document.status().permission_health
        except Exception:  # noqa: BLE001 -- optional writer fails closed
            sensitive_document = None
        else:
            if sensitive_health not in {"MISSING", "PRIVATE", "TOO_PERMISSIVE"}:
                sensitive_document = None
        if sensitive_document is not None:
            enabled.add("sensitive_write")

    if not enabled:
        return ReadOnlyConfigStudioServices(**service_options)
    enabled.add("rollback")
    return OwnerBackedConfigStudioServices(
        **service_options,
        app_document=app_document,
        overlay_document=overlay_document,
        sensitive_document=sensitive_document,
        enabled_write_capabilities=frozenset(enabled),
    )


__all__ = ["create_production_config_studio_services"]
