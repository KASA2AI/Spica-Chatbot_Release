"""Owner-validated authoring for the fixed ``data/config/app.yaml`` document."""

from __future__ import annotations

import io
import hmac
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import UnionType
from typing import Any, Callable, Mapping, Protocol, Union, get_args, get_origin

from pydantic import BaseModel

from spica.config.document_transaction import (
    DocumentRevision,
    DocumentTransactionError,
    ManagedDocumentTransaction,
    RestorePointMetadata,
)
from spica.config.environment_snapshot import EnvironmentSnapshot
from spica.config.manager import ConfigManager, ConfigResolution
from spica.config.schema import AppConfig
from spica.config.secrets import LoadedSecrets
from spica.ports.config_studio_platform import PlatformCapabilities
from spica.config_studio.authoring import (
    AuthoringOperation,
    AuthoringError,
    ConfigAuthoringValidator,
    SetValue,
    UnsetValue,
)
from spica.config_studio.paths import (
    ConfigFieldPath,
    FieldSegment,
    ListIndexSegment,
    MapKeySegment,
    PathSegment,
)
from spica.config_studio.yaml_owner import YamlOwnerError, load_yaml_mapping


_MISSING = object()


class RoundTripEditor(Protocol):
    def apply(self, base: bytes, operations: tuple[AuthoringOperation, ...]) -> bytes: ...


class AppDocumentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def __repr__(self) -> str:
        return f"AppDocumentError(code={self.code!r})"


@dataclass(frozen=True, slots=True)
class AppDocumentStatus:
    recovery_only: bool
    error_code: str | None
    manual_repair_code: str | None


@dataclass(frozen=True, slots=True, repr=False)
class AppFieldChange:
    path: ConfigFieldPath
    display_path: str
    file_value_before: Any = field(repr=False)
    file_value_after: Any = field(repr=False)
    next_launch_value_before: Any = field(repr=False)
    next_launch_value_after: Any = field(repr=False)
    source_before: str
    source_after: str
    file_value_shadowed: bool
    semantic_warning: str | None

    def __repr__(self) -> str:
        return "AppFieldChange(path=<redacted>, values=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class AppChangePreview:
    preview_id: str
    changed: bool
    changes: tuple[AppFieldChange, ...]
    effect_policy: str = "next_spica_launch"

    def __repr__(self) -> str:
        return (
            f"AppChangePreview(preview_id={self.preview_id!r}, "
            f"changed={self.changed!r}, candidate=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class _AppPreviewRecord:
    session_id: str
    semantic_preview: AppChangePreview
    candidate: bytes = field(repr=False)
    revision: DocumentRevision
    environment_snapshot: EnvironmentSnapshot = field(repr=False)
    forbidden_values: tuple[str, ...] = field(repr=False)
    loaded_environment: LoadedSecrets | None = field(repr=False)
    authored_roots: frozenset[str]
    expires_at: float


@dataclass(frozen=True, slots=True)
class AppDocumentCommit:
    restore_point_id: str | None
    effect_policy: str = "next_spica_launch"
    maintenance_code: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class AppRollbackPreview:
    restore_point_id: str
    changed_fields: tuple[str, ...]
    next_launch_changed_fields: tuple[str, ...]
    unmanaged_content_changed: bool
    unmanaged_change_count: int
    resolution_error_before: bool
    resolution_error_after: bool
    effect_policy: str = "next_spica_launch"

    def __repr__(self) -> str:
        return (
            "AppRollbackPreview("
            f"changed_field_count={len(self.changed_fields)}, "
            "next_launch_changed_field_count="
            f"{len(self.next_launch_changed_fields)}, "
            f"unmanaged_content_changed={self.unmanaged_content_changed!r}, "
            f"unmanaged_change_count={self.unmanaged_change_count}, "
            f"resolution_error_before={self.resolution_error_before!r}, "
            f"resolution_error_after={self.resolution_error_after!r}, "
            f"effect_policy={self.effect_policy!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AppRollbackConfirmation:
    receipt_token: str = field(repr=False)
    preview: AppRollbackPreview

    def __repr__(self) -> str:
        return f"AppRollbackConfirmation(preview={self.preview!r}, receipt=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class _RollbackReceipt:
    session_id: str
    current_revision: DocumentRevision
    preview: AppRollbackPreview
    environment_snapshot: EnvironmentSnapshot = field(repr=False)
    forbidden_values: tuple[str, ...] = field(repr=False)
    loaded_environment: LoadedSecrets | None = field(repr=False)
    authored_roots: frozenset[str]
    expires_at: float


class RuamelRoundTripEditor:
    """Round-trip YAML adapter; production PyYAML validation happens afterwards."""

    def __init__(self) -> None:
        try:
            from ruamel.yaml import YAML
            from ruamel.yaml.comments import CommentedMap
        except ImportError as exc:  # pragma: no cover - exercised before install
            raise AppDocumentError(
                "CAPABILITY_UNAVAILABLE",
                "the declared Config Studio YAML dependency is unavailable",
            ) from exc
        self._yaml_factory = YAML
        self._mapping_type = CommentedMap

    def apply(self, base: bytes, operations: tuple[AuthoringOperation, ...]) -> bytes:
        try:
            source = base.decode("utf-8")
        except UnicodeError as exc:
            raise AppDocumentError("DOCUMENT_INVALID", "app document is not UTF-8") from exc
        round_trip = self._yaml_factory(typ="rt")
        round_trip.preserve_quotes = True
        round_trip.allow_duplicate_keys = False
        round_trip.width = 4096
        round_trip.line_break = "\r\n" if "\r\n" in source else "\n"
        try:
            document = round_trip.load(source)
        except Exception as exc:
            raise AppDocumentError("RECOVERY_ONLY", "app document YAML is damaged") from exc
        if document is None:
            document = self._mapping_type()
        if not isinstance(document, dict):
            raise AppDocumentError("RECOVERY_ONLY", "app document root is not a mapping")
        for operation in operations:
            if not isinstance(operation, (SetValue, UnsetValue)):
                raise AppDocumentError("DOCUMENT_INVALID", "unsupported authoring operation")
            if isinstance(operation, SetValue):
                _set_round_trip_value(
                    document,
                    operation.path.segments,
                    operation.value,
                    mapping_type=self._mapping_type,
                )
            else:
                _unset_round_trip_value(document, operation.path.segments)
        output = io.StringIO()
        try:
            round_trip.dump(document, output)
        except Exception as exc:
            raise AppDocumentError("DOCUMENT_INVALID", "app document rendering failed") from exc
        return output.getvalue().encode("utf-8")


class AppConfigDocument:
    """Preview and commit app authoring without exposing candidate bytes or hashes."""

    def __init__(
        self,
        document_path: str | Path,
        *,
        backup_root: str | Path,
        environment_snapshot: EnvironmentSnapshot,
        environment_snapshot_owner: Callable[[], EnvironmentSnapshot] | None = None,
        round_trip_editor: RoundTripEditor,
        platform_capabilities: PlatformCapabilities,
        manager: ConfigManager | None = None,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] | None = None,
        preview_ttl_seconds: float = 5 * 60,
    ) -> None:
        if preview_ttl_seconds <= 0:
            raise ValueError("preview TTL must be positive")
        if token_factory is None:
            import secrets

            token_factory = lambda: secrets.token_urlsafe(24)
        self._transaction = ManagedDocumentTransaction(
            document_path,
            backup_root=backup_root,
            lock_root=Path(backup_root).parent / "locks",
            retention=5,
            platform_capabilities=platform_capabilities,
        )
        self._manager = manager or ConfigManager(document_path)
        self._environment_snapshot = environment_snapshot
        if environment_snapshot_owner is not None and not callable(
            environment_snapshot_owner
        ):
            raise TypeError("environment_snapshot_owner must be callable")
        self._environment_snapshot_owner = environment_snapshot_owner
        self._plugin_root = (
            Path(os.path.abspath(document_path)).parents[2] / "plugins"
        )
        self._editor = round_trip_editor
        self._clock = clock
        self._token_factory = token_factory
        self._preview_ttl_seconds = preview_ttl_seconds
        import secrets

        self._direct_session_id = secrets.token_urlsafe(24)
        self._previews: dict[str, _AppPreviewRecord] = {}
        self._preview_lock = threading.Lock()
        self._rollback_receipts: dict[str, _RollbackReceipt] = {}
        self._rollback_lock = threading.Lock()

    def status(self) -> AppDocumentStatus:
        try:
            environment_snapshot = self._current_environment_snapshot()
            snapshot = self._transaction.preview(b"").current
            document = _load_production_yaml(snapshot.content)
            self._manager.resolve_snapshot(document, environment_snapshot)
        except AppDocumentError as exc:
            if exc.code == "PREVIEW_UNAVAILABLE":
                raise
            return AppDocumentStatus(
                recovery_only=True,
                error_code="RECOVERY_ONLY",
                manual_repair_code="APP_YAML_MANUAL_REPAIR_REQUIRED",
            )
        except (TypeError, ValueError):
            return AppDocumentStatus(
                recovery_only=True,
                error_code="RECOVERY_ONLY",
                manual_repair_code="APP_YAML_MANUAL_REPAIR_REQUIRED",
            )
        return AppDocumentStatus(False, None, None)

    def preview(
        self,
        operations: tuple[AuthoringOperation, ...],
        *,
        session_id: str | None = None,
        forbidden_values: tuple[str, ...] = (),
        environment_snapshot: EnvironmentSnapshot | None = None,
        loaded_environment: LoadedSecrets | None = None,
        environment_guard: Callable[[], LoadedSecrets] | None = None,
        legacy_owner_guard: Callable[[], frozenset[str]] | None = None,
    ) -> AppChangePreview:
        if not isinstance(operations, tuple) or not operations:
            raise AppDocumentError("DOCUMENT_INVALID", "at least one operation is required")
        selected_environment = self._select_environment_snapshot(
            loaded_environment.environment_snapshot
            if loaded_environment is not None
            else environment_snapshot
        )
        selected_forbidden = _normalized_forbidden_values(forbidden_values)
        authored_roots = _operation_roots(operations)
        self._assert_legacy_owner_guard(
            legacy_owner_guard,
            authored_roots=authored_roots,
        )
        captured = self._transaction.preview(b"").current
        try:
            base_document = _load_production_yaml(captured.content)
        except AppDocumentError as exc:
            raise AppDocumentError("RECOVERY_ONLY", "app document requires recovery") from exc
        try:
            before_resolution = self._manager.resolve_snapshot(
                base_document,
                selected_environment,
            )
        except (TypeError, ValueError) as exc:
            raise AppDocumentError("RECOVERY_ONLY", "app document requires recovery") from exc

        candidate_bytes = self._editor.apply(captured.content, operations)
        candidate_document = _load_production_yaml(candidate_bytes)
        _reject_forbidden_candidate(
            candidate_document,
            selected_forbidden,
            loaded_environment=loaded_environment,
        )
        try:
            validated = ConfigAuthoringValidator(
                manager=self._manager,
                environment_snapshot=selected_environment,
                plugin_root=self._plugin_root,
            ).validate(
                base_document,
                candidate_document,
                operations,
            )
        except AuthoringError as exc:
            code = "UNKNOWN_FIELD" if exc.code == "UNKNOWN_FIELD" else "DOCUMENT_INVALID"
            raise AppDocumentError(code, "candidate failed owner validation") from exc
        _reject_forbidden_resolution(
            validated.resolution,
            selected_forbidden,
            loaded_environment=loaded_environment,
        )
        changes = tuple(
            _field_change(
                operation.path,
                base_document=base_document,
                candidate_document=candidate_document,
                before_resolution=before_resolution,
                after_resolution=validated.resolution,
            )
            for operation in operations
        )
        self._assert_environment_guard(
            environment_guard,
            environment_snapshot=selected_environment,
            forbidden_values=selected_forbidden,
            loaded_environment=loaded_environment,
            candidate_document=candidate_document,
        )
        self._assert_legacy_owner_guard(
            legacy_owner_guard,
            authored_roots=authored_roots,
        )
        bound_session = self._bound_session_id(session_id)
        with self._preview_lock:
            self._drop_expired_locked()
            if len(self._previews) >= 64:
                raise AppDocumentError(
                    "PREVIEW_UNAVAILABLE", "too many active app previews"
                )
            preview = AppChangePreview(
                preview_id=self._allocate_preview_id_locked(),
                changed=candidate_bytes != captured.content,
                changes=changes,
            )
            self._previews[preview.preview_id] = _AppPreviewRecord(
                session_id=bound_session,
                semantic_preview=preview,
                candidate=candidate_bytes,
                revision=captured.revision,
                environment_snapshot=selected_environment,
                forbidden_values=selected_forbidden,
                loaded_environment=loaded_environment,
                authored_roots=authored_roots,
                expires_at=self._clock() + self._preview_ttl_seconds,
            )
        return preview

    def commit(
        self,
        preview_id: str,
        *,
        session_id: str | None = None,
        forbidden_values: tuple[str, ...] = (),
        environment_snapshot: EnvironmentSnapshot | None = None,
        loaded_environment: LoadedSecrets | None = None,
        environment_guard: Callable[[], LoadedSecrets] | None = None,
        legacy_owner_guard: Callable[[], frozenset[str]] | None = None,
    ) -> AppDocumentCommit:
        preview = self._consume_preview(
            preview_id,
            session_id=self._bound_session_id(session_id),
        )
        selected_environment = self._select_environment_snapshot(
            loaded_environment.environment_snapshot
            if loaded_environment is not None
            else environment_snapshot
        )
        selected_forbidden = _normalized_forbidden_values(forbidden_values)
        try:
            candidate_document = _load_production_yaml(preview.candidate)
            _reject_forbidden_candidate(
                candidate_document,
                selected_forbidden,
                loaded_environment=loaded_environment,
            )
            if (
                selected_environment != preview.environment_snapshot
                or selected_forbidden != preview.forbidden_values
                or not _same_secret_material(
                    loaded_environment,
                    preview.loaded_environment,
                )
            ):
                raise AppDocumentError(
                    "CONFIRMATION_REQUIRED",
                    "configuration environment changed after preview",
                )
            candidate_resolution = self._manager.resolve_snapshot(
                candidate_document,
                selected_environment,
            )
            _reject_forbidden_resolution(
                candidate_resolution,
                selected_forbidden,
                loaded_environment=loaded_environment,
            )
            self._assert_environment_guard(
                environment_guard,
                environment_snapshot=selected_environment,
                forbidden_values=selected_forbidden,
                loaded_environment=loaded_environment,
                candidate_document=candidate_document,
            )
            self._assert_legacy_owner_guard(
                legacy_owner_guard,
                authored_roots=preview.authored_roots,
            )
            result = self._transaction.commit(
                preview.candidate,
                expected_revision=preview.revision,
            )
        except DocumentTransactionError as exc:
            raise AppDocumentError(exc.code, "app document commit failed") from exc
        except AppDocumentError:
            raise
        except (TypeError, ValueError) as exc:
            raise AppDocumentError("DOCUMENT_INVALID", "candidate validation failed") from exc
        return AppDocumentCommit(
            restore_point_id=(
                result.restore_point.id if result.restore_point is not None else None
            ),
            maintenance_code=result.maintenance_code,
        )

    def restore_points(self) -> tuple[RestorePointMetadata, ...]:
        return self._transaction.restore_points()

    def prepare_rollback(
        self,
        restore_point_id: str,
        *,
        session_id: str,
        forbidden_values: tuple[str, ...] = (),
        environment_snapshot: EnvironmentSnapshot | None = None,
        loaded_environment: LoadedSecrets | None = None,
        environment_guard: Callable[[], LoadedSecrets] | None = None,
        legacy_owner_guard: Callable[[], frozenset[str]] | None = None,
    ) -> AppRollbackConfirmation:
        if not isinstance(session_id, str) or not session_id:
            raise AppDocumentError("CONFIRMATION_REQUIRED", "session is required")
        current = self._transaction.preview(b"").current
        selected_environment = self._select_environment_snapshot(
            loaded_environment.environment_snapshot
            if loaded_environment is not None
            else environment_snapshot
        )
        selected_forbidden = _normalized_forbidden_values(forbidden_values)
        try:
            restored = self._transaction.restore_snapshot(restore_point_id)
            restored_content = restored.content if restored.revision.exists else b""
            restored_document = _load_production_yaml(restored_content)
            _reject_forbidden_candidate(
                restored_document,
                selected_forbidden,
                loaded_environment=loaded_environment,
            )
            preview = self._rollback_preview(
                restore_point_id=restore_point_id,
                current_content=current.content,
                restored_content=restored_content,
                environment_snapshot=selected_environment,
                forbidden_values=selected_forbidden,
                loaded_environment=loaded_environment,
            )
        except DocumentTransactionError as exc:
            raise AppDocumentError(exc.code, "restore point is unavailable") from exc
        self._assert_environment_guard(
            environment_guard,
            environment_snapshot=selected_environment,
            forbidden_values=selected_forbidden,
            loaded_environment=loaded_environment,
            candidate_document=restored_document,
        )
        authored_roots = _rollback_roots(preview)
        self._assert_legacy_owner_guard(
            legacy_owner_guard,
            authored_roots=authored_roots,
        )
        receipt_token = self._allocate_receipt(
            session_id=session_id,
            current_revision=current.revision,
            preview=preview,
            environment_snapshot=selected_environment,
            forbidden_values=selected_forbidden,
            loaded_environment=loaded_environment,
            authored_roots=authored_roots,
        )
        return AppRollbackConfirmation(
            receipt_token=receipt_token,
            preview=preview,
        )

    def rollback(
        self,
        receipt_token: str,
        *,
        session_id: str,
        forbidden_values: tuple[str, ...] = (),
        environment_snapshot: EnvironmentSnapshot | None = None,
        loaded_environment: LoadedSecrets | None = None,
        environment_guard: Callable[[], LoadedSecrets] | None = None,
        legacy_owner_guard: Callable[[], frozenset[str]] | None = None,
    ) -> AppDocumentCommit:
        receipt = self._consume_receipt(receipt_token, session_id=session_id)
        current = self._transaction.preview(b"").current
        if current.revision != receipt.current_revision:
            raise AppDocumentError("DOCUMENT_CONFLICT", "app document changed")
        selected_environment = self._select_environment_snapshot(
            loaded_environment.environment_snapshot
            if loaded_environment is not None
            else environment_snapshot
        )
        selected_forbidden = _normalized_forbidden_values(forbidden_values)
        try:
            restored = self._transaction.restore_snapshot(
                receipt.preview.restore_point_id
            )
            restored_content = restored.content if restored.revision.exists else b""
            restored_document = _load_production_yaml(restored_content)
            _reject_forbidden_candidate(
                restored_document,
                selected_forbidden,
                loaded_environment=loaded_environment,
            )
            if (
                selected_environment != receipt.environment_snapshot
                or selected_forbidden != receipt.forbidden_values
                or not _same_secret_material(
                    loaded_environment,
                    receipt.loaded_environment,
                )
            ):
                raise AppDocumentError(
                    "CONFIRMATION_REQUIRED",
                    "configuration environment changed after rollback preview",
                )
            current_preview = self._rollback_preview(
                restore_point_id=receipt.preview.restore_point_id,
                current_content=current.content,
                restored_content=restored_content,
                environment_snapshot=selected_environment,
                forbidden_values=selected_forbidden,
                loaded_environment=loaded_environment,
            )
            if current_preview != receipt.preview:
                raise AppDocumentError(
                    "CONFIRMATION_REQUIRED",
                    "rollback semantics changed",
                )
            self._assert_environment_guard(
                environment_guard,
                environment_snapshot=selected_environment,
                forbidden_values=selected_forbidden,
                loaded_environment=loaded_environment,
                candidate_document=restored_document,
            )
            self._assert_legacy_owner_guard(
                legacy_owner_guard,
                authored_roots=receipt.authored_roots,
            )
            result = self._transaction.rollback(
                receipt.preview.restore_point_id,
                expected_revision=receipt.current_revision,
            )
        except AppDocumentError:
            raise
        except DocumentTransactionError as exc:
            raise AppDocumentError(exc.code, "app rollback failed") from exc
        return AppDocumentCommit(
            restore_point_id=(
                result.restore_point.id if result.restore_point is not None else None
            ),
            maintenance_code=result.maintenance_code,
        )

    def _rollback_preview(
        self,
        *,
        restore_point_id: str,
        current_content: bytes,
        restored_content: bytes,
        environment_snapshot: EnvironmentSnapshot,
        forbidden_values: tuple[str, ...],
        loaded_environment: LoadedSecrets | None,
    ) -> AppRollbackPreview:
        try:
            restored_document = _load_production_yaml(restored_content)
            restored_resolution = self._manager.resolve_snapshot(
                restored_document,
                environment_snapshot,
            )
            _reject_forbidden_resolution(
                restored_resolution,
                forbidden_values,
                loaded_environment=loaded_environment,
            )
        except (AppDocumentError, TypeError, ValueError) as exc:
            raise AppDocumentError(
                "NO_VALID_RESTORE_POINT",
                "restore point does not pass the production owner",
            ) from exc
        try:
            current_document = _load_production_yaml(current_content)
            current_resolution = self._manager.resolve_snapshot(
                current_document,
                environment_snapshot,
            )
        except (AppDocumentError, TypeError, ValueError):
            return AppRollbackPreview(
                restore_point_id=restore_point_id,
                changed_fields=("<recovery-only-document>",),
                next_launch_changed_fields=(),
                unmanaged_content_changed=True,
                unmanaged_change_count=1,
                resolution_error_before=True,
                resolution_error_after=False,
            )
        current_unknown = _unknown_leaf_values(current_document)
        restored_unknown = _unknown_leaf_values(restored_document)
        current_raw = _flatten_leaf_values(current_document)
        restored_raw = _flatten_leaf_values(restored_document)
        unknown_paths = set(current_unknown) | set(restored_unknown)
        changed_fields = tuple(
            _render_plain_path(path)
            for path in sorted(
                _changed_paths(current_raw, restored_raw) - unknown_paths,
                key=_sortable_path,
            )
        )
        current_resolved = _flatten_leaf_values(
            current_resolution.to_app_config().model_dump()
        )
        restored_resolved = _flatten_leaf_values(
            restored_resolution.to_app_config().model_dump()
        )
        next_launch_changed = tuple(
            _render_plain_path(path)
            for path in sorted(
                _changed_paths(current_resolved, restored_resolved),
                key=_sortable_path,
            )
        )
        unknown_changed = _changed_paths(current_unknown, restored_unknown)
        return AppRollbackPreview(
            restore_point_id=restore_point_id,
            changed_fields=changed_fields,
            next_launch_changed_fields=next_launch_changed,
            unmanaged_content_changed=bool(unknown_changed),
            unmanaged_change_count=len(unknown_changed),
            resolution_error_before=False,
            resolution_error_after=False,
        )

    def _allocate_preview_id_locked(self) -> str:
        for _ in range(32):
            candidate = self._token_factory()
            if not isinstance(candidate, str) or not candidate:
                raise RuntimeError("preview token factory returned an invalid token")
            if candidate not in self._previews:
                return candidate
        raise RuntimeError("could not allocate preview token")

    def _current_environment_snapshot(self) -> EnvironmentSnapshot:
        if self._environment_snapshot_owner is None:
            return self._environment_snapshot
        try:
            snapshot = self._environment_snapshot_owner()
        except Exception:  # noqa: BLE001 -- external owner stays bounded
            raise AppDocumentError(
                "PREVIEW_UNAVAILABLE",
                "configuration environment is unavailable",
            ) from None
        if not isinstance(snapshot, EnvironmentSnapshot):
            raise AppDocumentError(
                "PREVIEW_UNAVAILABLE",
                "configuration environment is unavailable",
            )
        return snapshot

    def _select_environment_snapshot(
        self,
        explicit: EnvironmentSnapshot | None,
    ) -> EnvironmentSnapshot:
        snapshot = (
            self._current_environment_snapshot()
            if explicit is None
            else explicit
        )
        if not isinstance(snapshot, EnvironmentSnapshot):
            raise AppDocumentError(
                "PREVIEW_UNAVAILABLE",
                "configuration environment is unavailable",
            )
        return snapshot

    def _assert_environment_guard(
        self,
        guard: Callable[[], LoadedSecrets] | None,
        *,
        environment_snapshot: EnvironmentSnapshot,
        forbidden_values: tuple[str, ...],
        loaded_environment: LoadedSecrets | None,
        candidate_document: Any,
    ) -> None:
        if guard is None:
            return
        try:
            current_loaded = guard()
        except Exception:  # noqa: BLE001 -- external owner stays bounded
            raise AppDocumentError(
                "PREVIEW_UNAVAILABLE",
                "configuration environment is unavailable",
            ) from None
        if not isinstance(current_loaded, LoadedSecrets):
            raise AppDocumentError(
                "PREVIEW_UNAVAILABLE",
                "configuration environment is unavailable",
            )
        if (
            current_loaded.environment_snapshot != environment_snapshot
            or not _same_secret_material(
                current_loaded,
                loaded_environment,
            )
        ):
            try:
                current_resolution = self._manager.resolve_snapshot(
                    candidate_document,
                    environment_snapshot,
                )
            except (TypeError, ValueError) as exc:
                raise AppDocumentError(
                    "DOCUMENT_INVALID",
                    "app candidate failed owner validation",
                ) from exc
            if (
                _contains_owner_secret_material(
                    candidate_document,
                    current_loaded,
                    annotation=AppConfig,
                )
                or _contains_owner_secret_material(
                    current_resolution.to_app_config().model_dump(),
                    current_loaded,
                    annotation=AppConfig,
                )
            ):
                raise AppDocumentError(
                    "DOCUMENT_INVALID",
                    "app candidate contains current secret material",
                )
            raise AppDocumentError(
                "CONFIRMATION_REQUIRED",
                "configuration environment changed during authoring",
            )

    def _assert_legacy_owner_guard(
        self,
        guard: Callable[[], frozenset[str]] | None,
        *,
        authored_roots: frozenset[str],
    ) -> None:
        if guard is None or not authored_roots:
            return
        try:
            active = guard()
        except Exception:  # noqa: BLE001 -- fixed owner check stays bounded
            raise AppDocumentError(
                "DOCUMENT_UNSAFE",
                "legacy owner state is unavailable",
            ) from None
        if not isinstance(active, frozenset) or any(
            not isinstance(root, str) for root in active
        ):
            raise AppDocumentError(
                "DOCUMENT_UNSAFE",
                "legacy owner state is unavailable",
            )
        if authored_roots & active:
            raise AppDocumentError(
                "DOCUMENT_UNSAFE",
                "retired legacy owner is active for this section",
            )

    def _bound_session_id(self, session_id: str | None) -> str:
        if session_id is None:
            return self._direct_session_id
        if not isinstance(session_id, str) or not session_id:
            raise AppDocumentError("CONFIRMATION_REQUIRED", "session is required")
        return session_id

    def _consume_preview(
        self,
        preview_id: str,
        *,
        session_id: str,
    ) -> _AppPreviewRecord:
        if not isinstance(preview_id, str) or not preview_id:
            raise AppDocumentError("CONFIRMATION_REQUIRED", "preview is required")
        with self._preview_lock:
            preview = self._previews.get(preview_id)
            if preview is None:
                raise AppDocumentError("CONFIRMATION_REQUIRED", "preview is invalid")
            if self._clock() >= preview.expires_at:
                self._previews.pop(preview_id, None)
                raise AppDocumentError("CONFIRMATION_REQUIRED", "preview is invalid")
            if not hmac.compare_digest(preview.session_id, session_id):
                raise AppDocumentError("CONFIRMATION_REQUIRED", "preview is invalid")
            self._previews.pop(preview_id, None)
        if preview is None:
            raise AppDocumentError("CONFIRMATION_REQUIRED", "preview is invalid")
        return preview

    def _drop_expired_locked(self) -> None:
        now = self._clock()
        for preview_id, preview in tuple(self._previews.items()):
            if now >= preview.expires_at:
                self._previews.pop(preview_id, None)

    def _allocate_receipt(
        self,
        *,
        session_id: str,
        current_revision: DocumentRevision,
        preview: AppRollbackPreview,
        environment_snapshot: EnvironmentSnapshot,
        forbidden_values: tuple[str, ...],
        loaded_environment: LoadedSecrets | None,
        authored_roots: frozenset[str],
    ) -> str:
        now = self._clock()
        with self._rollback_lock:
            self._rollback_receipts = {
                token: record
                for token, record in self._rollback_receipts.items()
                if record.expires_at > now
            }
            for _ in range(32):
                token = self._token_factory()
                if token and token not in self._rollback_receipts:
                    self._rollback_receipts[token] = _RollbackReceipt(
                        session_id=session_id,
                        current_revision=current_revision,
                        preview=preview,
                        environment_snapshot=environment_snapshot,
                        forbidden_values=forbidden_values,
                        loaded_environment=loaded_environment,
                        authored_roots=authored_roots,
                        expires_at=now + self._preview_ttl_seconds,
                    )
                    return token
        raise AppDocumentError(
            "CONFIRMATION_REQUIRED",
            "could not allocate rollback confirmation",
        )

    def _consume_receipt(
        self,
        receipt_token: str,
        *,
        session_id: str,
    ) -> _RollbackReceipt:
        if not isinstance(receipt_token, str) or not isinstance(session_id, str):
            raise AppDocumentError("CONFIRMATION_REQUIRED", "rollback receipt is invalid")
        with self._rollback_lock:
            receipt = self._rollback_receipts.get(receipt_token)
            if (
                receipt is None
                or self._clock() >= receipt.expires_at
                or not hmac.compare_digest(receipt.session_id, session_id)
            ):
                if receipt is not None and self._clock() >= receipt.expires_at:
                    self._rollback_receipts.pop(receipt_token, None)
                raise AppDocumentError(
                    "CONFIRMATION_REQUIRED",
                    "rollback receipt is invalid",
                )
            self._rollback_receipts.pop(receipt_token, None)
            return receipt


def _load_production_yaml(content: bytes) -> dict[str, Any]:
    try:
        return load_yaml_mapping(content, reject_aliases=True)
    except YamlOwnerError as exc:
        raise AppDocumentError("DOCUMENT_INVALID", "app document YAML is invalid") from exc


def _reject_forbidden_candidate(
    candidate: Any,
    forbidden_values: tuple[str, ...],
    *,
    loaded_environment: LoadedSecrets | None = None,
) -> None:
    values = _normalized_forbidden_values(forbidden_values)
    if (
        values
        and _contains_forbidden_value(candidate, values, annotation=AppConfig)
    ) or (
        loaded_environment is not None
        and _contains_owner_secret_material(
            candidate,
            loaded_environment,
            annotation=AppConfig,
        )
    ):
        raise AppDocumentError(
            "DOCUMENT_INVALID",
            "app candidate contains forbidden secret material",
        )


def _reject_forbidden_resolution(
    resolution: ConfigResolution,
    forbidden_values: tuple[str, ...],
    *,
    loaded_environment: LoadedSecrets | None = None,
) -> None:
    _reject_forbidden_candidate(
        resolution.to_app_config().model_dump(),
        forbidden_values,
        loaded_environment=loaded_environment,
    )


def _contains_forbidden_value(
    value: Any,
    forbidden_values: tuple[str, ...],
    *,
    annotation: Any = Any,
) -> bool:
    if isinstance(value, str):
        return any(secret in value for secret in forbidden_values)
    if isinstance(value, bytes):
        return any(
            secret.encode("utf-8") in value for secret in forbidden_values
        )
    if isinstance(value, dict):
        model_type = _nested_model(annotation)
        if model_type is not None:
            return any(
                (
                    _contains_forbidden_value(key, forbidden_values)
                    or _contains_forbidden_value(child, forbidden_values)
                )
                if (field_info := model_type.model_fields.get(str(key))) is None
                else _contains_forbidden_value(
                    child,
                    forbidden_values,
                    annotation=field_info.annotation,
                )
                for key, child in value.items()
            )
        value_annotation = _mapping_value_annotation(annotation)
        return any(
            _contains_forbidden_value(key, forbidden_values)
            or _contains_forbidden_value(
                child,
                forbidden_values,
                annotation=value_annotation,
            )
            for key, child in value.items()
        )
    if isinstance(value, list):
        item_annotation = _sequence_item_annotation(annotation)
        return any(
            _contains_forbidden_value(
                child,
                forbidden_values,
                annotation=item_annotation,
            )
            for child in value
        )
    return False


def _normalized_forbidden_values(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {value for value in values if isinstance(value, str) and value},
            key=len,
            reverse=True,
        )
    )


def _operation_roots(
    operations: tuple[AuthoringOperation, ...],
) -> frozenset[str]:
    roots: set[str] = set()
    for operation in operations:
        segments = getattr(getattr(operation, "path", None), "segments", ())
        if segments and isinstance(segments[0], FieldSegment):
            roots.add(segments[0].name)
    return frozenset(roots)


def _rollback_roots(preview: AppRollbackPreview) -> frozenset[str]:
    fields = preview.changed_fields + preview.next_launch_changed_fields
    if "<recovery-only-document>" in fields:
        return frozenset({"plugins", "screen", "song"})
    return frozenset(
        root
        for field_name in fields
        if (root := field_name.split(".", 1)[0].split("[", 1)[0])
    )


def _contains_owner_secret_material(
    value: Any,
    loaded_environment: LoadedSecrets,
    *,
    annotation: Any = Any,
) -> bool:
    if isinstance(value, str):
        return loaded_environment.contains_secret_material(value)
    if isinstance(value, bytes):
        try:
            decoded = value.decode("utf-8")
        except UnicodeError:
            return True
        return loaded_environment.contains_secret_material(decoded)
    if isinstance(value, Mapping):
        model_type = _nested_model(annotation)
        if model_type is not None:
            return any(
                (
                    _contains_owner_secret_material(key, loaded_environment)
                    or _contains_owner_secret_material(child, loaded_environment)
                )
                if (field_info := model_type.model_fields.get(str(key))) is None
                else _contains_owner_secret_material(
                    child,
                    loaded_environment,
                    annotation=field_info.annotation,
                )
                for key, child in value.items()
            )
        value_annotation = _mapping_value_annotation(annotation)
        return any(
            _contains_owner_secret_material(key, loaded_environment)
            or _contains_owner_secret_material(
                child,
                loaded_environment,
                annotation=value_annotation,
            )
            for key, child in value.items()
        )
    if isinstance(value, (tuple, list)):
        item_annotation = _sequence_item_annotation(annotation)
        return any(
            _contains_owner_secret_material(
                child,
                loaded_environment,
                annotation=item_annotation,
            )
            for child in value
        )
    return False


def _same_secret_material(
    left: LoadedSecrets | None,
    right: LoadedSecrets | None,
) -> bool:
    if left is None or right is None:
        return left is right
    return left.same_secret_material(right)


def _mapping_value_annotation(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        for option in get_args(annotation):
            candidate = _mapping_value_annotation(option)
            if candidate is not Any:
                return candidate
        return Any
    if origin is dict:
        arguments = get_args(annotation)
        return arguments[1] if len(arguments) == 2 else Any
    return Any


def _sequence_item_annotation(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        for option in get_args(annotation):
            candidate = _sequence_item_annotation(option)
            if candidate is not Any:
                return candidate
        return Any
    if origin in (list, tuple):
        arguments = get_args(annotation)
        return arguments[0] if arguments else Any
    return Any


def _plain_path(path: ConfigFieldPath) -> tuple[str | int, ...]:
    result: list[str | int] = []
    for segment in path.segments:
        if isinstance(segment, FieldSegment):
            result.append(segment.name)
        elif isinstance(segment, MapKeySegment):
            result.append(segment.key)
        elif isinstance(segment, ListIndexSegment):
            result.append(segment.index)
    return tuple(result)


def _display_path(path: ConfigFieldPath) -> str:
    rendered = ""
    for segment in path.segments:
        if isinstance(segment, FieldSegment):
            rendered += ("." if rendered else "") + segment.name
        elif isinstance(segment, MapKeySegment):
            rendered += f"[{segment.key!r}]"
        elif isinstance(segment, ListIndexSegment):
            rendered += f"[{segment.index}]"
    return rendered


def _get_path(document: Any, path: tuple[str | int, ...]) -> Any:
    current = document
    for segment in path:
        if isinstance(segment, int):
            if not isinstance(current, list) or segment >= len(current):
                return _MISSING
            current = current[segment]
        else:
            if not isinstance(current, dict) or segment not in current:
                return _MISSING
            current = current[segment]
    return current


def _field_change(
    path: ConfigFieldPath,
    *,
    base_document: dict[str, Any],
    candidate_document: dict[str, Any],
    before_resolution: ConfigResolution,
    after_resolution: ConfigResolution,
) -> AppFieldChange:
    plain = _plain_path(path)
    before_leaf = before_resolution.resolved_at(plain)
    after_leaf = after_resolution.resolved_at(plain)
    file_before = _get_path(base_document, plain)
    file_after = _get_path(candidate_document, plain)
    shadowed = after_leaf.source.kind in {
        "env_override",
        "secret_tainted_env_override",
    }
    return AppFieldChange(
        path=path,
        display_path=_display_path(path),
        file_value_before=None if file_before is _MISSING else file_before,
        file_value_after=None if file_after is _MISSING else file_after,
        next_launch_value_before=before_leaf.next_launch_value,
        next_launch_value_after=after_leaf.next_launch_value,
        source_before=before_leaf.source.kind,
        source_after=after_leaf.source.kind,
        file_value_shadowed=shadowed,
        semantic_warning=(
            "APP_FILE_VALUE_SHADOWED_BY_ENV" if shadowed else None
        ),
    )


def _set_round_trip_value(
    document: Any,
    segments: tuple[PathSegment, ...],
    value: Any,
    *,
    mapping_type: type,
) -> None:
    if not segments:
        raise AppDocumentError("DOCUMENT_INVALID", "operation path is empty")
    current = document
    for index, segment in enumerate(segments[:-1]):
        next_segment = segments[index + 1]
        if isinstance(segment, (FieldSegment, MapKeySegment)):
            if not isinstance(current, dict):
                raise AppDocumentError("DOCUMENT_INVALID", "path crosses a non-mapping")
            key = segment.name if isinstance(segment, FieldSegment) else segment.key
            if key not in current or current[key] is None:
                current[key] = [] if isinstance(next_segment, ListIndexSegment) else mapping_type()
            current = current[key]
        elif isinstance(segment, ListIndexSegment):
            if not isinstance(current, list) or segment.index >= len(current):
                raise AppDocumentError("DOCUMENT_INVALID", "list index is outside document")
            current = current[segment.index]
    final = segments[-1]
    if isinstance(final, (FieldSegment, MapKeySegment)):
        if not isinstance(current, dict):
            raise AppDocumentError("DOCUMENT_INVALID", "path crosses a non-mapping")
        key = final.name if isinstance(final, FieldSegment) else final.key
        current[key] = value
    elif isinstance(final, ListIndexSegment):
        if not isinstance(current, list) or final.index >= len(current):
            raise AppDocumentError("DOCUMENT_INVALID", "list index is outside document")
        current[final.index] = value


def _unset_round_trip_value(
    document: Any,
    segments: tuple[PathSegment, ...],
) -> None:
    if not segments:
        raise AppDocumentError("DOCUMENT_INVALID", "operation path is empty")
    current = document
    for segment in segments[:-1]:
        if isinstance(segment, (FieldSegment, MapKeySegment)):
            if not isinstance(current, dict):
                raise AppDocumentError("DOCUMENT_INVALID", "path crosses a non-mapping")
            key = segment.name if isinstance(segment, FieldSegment) else segment.key
            if key not in current:
                return
            current = current[key]
        elif isinstance(segment, ListIndexSegment):
            if not isinstance(current, list) or segment.index >= len(current):
                return
            current = current[segment.index]
    final = segments[-1]
    if isinstance(final, (FieldSegment, MapKeySegment)):
        if not isinstance(current, dict):
            raise AppDocumentError("DOCUMENT_INVALID", "path crosses a non-mapping")
        key = final.name if isinstance(final, FieldSegment) else final.key
        current.pop(key, None)
    elif isinstance(final, ListIndexSegment):
        if not isinstance(current, list):
            raise AppDocumentError("DOCUMENT_INVALID", "path crosses a non-list")
        if final.index < len(current):
            current.pop(final.index)


def _flatten_leaf_values(
    node: Any,
    prefix: tuple[str | int, ...] = (),
) -> dict[tuple[str | int, ...], Any]:
    if isinstance(node, dict) and node:
        result: dict[tuple[str | int, ...], Any] = {}
        for key, value in node.items():
            result.update(_flatten_leaf_values(value, prefix + (str(key),)))
        return result
    if isinstance(node, list) and node:
        result = {}
        for index, value in enumerate(node):
            result.update(_flatten_leaf_values(value, prefix + (index,)))
        return result
    return {prefix: node}


def _unknown_leaf_values(
    document: dict[str, Any],
    model_type: type[BaseModel] = AppConfig,
    prefix: tuple[str | int, ...] = (),
) -> dict[tuple[str | int, ...], Any]:
    unknown: dict[tuple[str | int, ...], Any] = {}
    for key, value in document.items():
        field_info = model_type.model_fields.get(str(key))
        path = prefix + (str(key),)
        if field_info is None:
            unknown.update(_flatten_leaf_values(value, path))
            continue
        nested = _nested_model(field_info.annotation)
        if nested is not None and isinstance(value, dict):
            unknown.update(_unknown_leaf_values(value, nested, path))
    return unknown


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        for option in get_args(annotation):
            nested = _nested_model(option)
            if nested is not None:
                return nested
        return None
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _changed_paths(
    before: dict[tuple[str | int, ...], Any],
    after: dict[tuple[str | int, ...], Any],
) -> set[tuple[str | int, ...]]:
    missing = object()
    return {
        path
        for path in set(before) | set(after)
        if before.get(path, missing) != after.get(path, missing)
    }


def _sortable_path(path: tuple[str | int, ...]) -> tuple[str, ...]:
    return tuple(f"{type(segment).__name__}:{segment}" for segment in path)


def _render_plain_path(path: tuple[str | int, ...]) -> str:
    rendered = ""
    for segment in path:
        if isinstance(segment, int):
            rendered += f"[{segment}]"
        else:
            rendered += ("." if rendered else "") + segment
    return rendered
