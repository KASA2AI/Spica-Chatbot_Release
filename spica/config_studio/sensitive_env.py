"""Write-only management for the repository ``xiaosan.env`` document."""

from __future__ import annotations

import copy
import hmac
import io
import json
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from dotenv.parser import parse_stream

from spica.config.document_transaction import (
    DocumentConflictError,
    DocumentRevision,
    DocumentSafetyError,
    DocumentTransactionError,
    DocumentCommit,
    ManagedDocumentSnapshot,
    ManagedDocumentTransaction,
    RestorePointError,
    RestorePointMetadata,
)
from spica.config.env_roster import APP_ENV_MAP, SCREEN_ENV_MAP, SECRETS_ENV_MAP
from spica.config.manager import ConfigManager, ConfigResolution
from spica.ports.config_studio_platform import PlatformCapabilities
from spica.config.secrets import (
    EnvironmentRefreshError,
    LoadedSecrets,
    RepoEnvironmentTransition,
    ResolvedRepoEnvironment,
)


_MANAGED_OVERRIDES = {
    **{environment_name: field_path for field_path, environment_name in APP_ENV_MAP.items()},
    **{
        environment_name: f"screen.{field_name}"
        for field_name, environment_name in SCREEN_ENV_MAP.items()
    },
}
_ASSIGNMENT = re.compile(
    r"(?m)^(?P<indent>[^\S\r\n]*)"
    r"(?:(?P<export>export)[^\S\r\n]+)?"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?=[^\S\r\n]*(?:=|\r?$))"
)


@dataclass(frozen=True, slots=True)
class SecretSlotStatus:
    slot: str
    configured: bool


@dataclass(frozen=True, slots=True)
class SensitiveEnvStatus:
    secret_slots: tuple[SecretSlotStatus, ...]
    permission_health: str


@dataclass(frozen=True, slots=True)
class ClearMappedOverride:
    environment_variable: str


@dataclass(frozen=True, slots=True, repr=False)
class SetSecret:
    slot: str
    value: str = field(repr=False)

    def __repr__(self) -> str:
        return f"SetSecret(slot={self.slot!r}, value=<redacted>)"


@dataclass(frozen=True, slots=True)
class ClearSecret:
    slot: str


@dataclass(frozen=True, slots=True, repr=False)
class SensitiveEnvPreview:
    """Immutable semantic preview backed by a bounded server-side candidate."""

    preview_id: str = field(repr=False)
    command_kind: str
    target: str
    affected_fields: tuple[str, ...]
    before_next_launch: Any
    after_next_launch: Any
    winning_source_before: str | None
    winning_source_after: str | None
    still_shadowed: bool
    permission_hardening: bool
    changed: bool
    secret_change: str | None
    resolution_error_before: bool
    resolution_error_after: bool

    def __repr__(self) -> str:
        return (
            "SensitiveEnvPreview("
            f"command_kind={self.command_kind!r}, target={self.target!r}, "
            f"changed={self.changed!r}, sensitive_payload=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class SensitiveCommitConfirmation:
    receipt_token: str = field(repr=False)
    preview: SensitiveEnvPreview


@dataclass(frozen=True, slots=True)
class SensitiveEnvCommit:
    restore_point_id: str | None = field(repr=False)
    permission_health: str
    maintenance_code: str | None = None


@dataclass(frozen=True, slots=True)
class SecretRollbackChange:
    slot: str
    change: str


@dataclass(frozen=True, slots=True, repr=False)
class OverrideRollbackChange:
    environment_variable: str
    affected_fields: tuple[str, ...]
    before_next_launch: Any
    after_next_launch: Any
    winning_source_before: str | None
    winning_source_after: str | None
    still_shadowed: bool

    def __repr__(self) -> str:
        return (
            "OverrideRollbackChange("
            f"affected_field_count={len(self.affected_fields)}, "
            f"still_shadowed={self.still_shadowed!r}, values=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class SensitiveRollbackPreview:
    restore_point_id: str
    secret_changes: tuple[SecretRollbackChange, ...]
    override_changes: tuple[OverrideRollbackChange, ...]
    unmanaged_content_changed: bool
    unmanaged_change_count: int
    permission_hardening: bool
    resolution_error_before: bool
    resolution_error_after: bool

    def __repr__(self) -> str:
        return (
            "SensitiveRollbackPreview("
            f"secret_change_count={len(self.secret_changes)}, "
            f"override_change_count={len(self.override_changes)}, "
            f"unmanaged_content_changed={self.unmanaged_content_changed!r}, "
            f"unmanaged_change_count={self.unmanaged_change_count}, "
            f"permission_hardening={self.permission_hardening!r}, "
            f"resolution_error_before={self.resolution_error_before!r}, "
            f"resolution_error_after={self.resolution_error_after!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class RollbackConfirmation:
    receipt_token: str = field(repr=False)
    preview: SensitiveRollbackPreview

    def __repr__(self) -> str:
        return f"RollbackConfirmation(preview={self.preview!r}, receipt=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class _ReceiptRecord:
    session_id: str
    current_revision: DocumentRevision
    preview: SensitiveRollbackPreview
    owner_transition: RepoEnvironmentTransition = field(repr=False)
    expires_at: float


@dataclass(frozen=True, slots=True, repr=False)
class _PreviewRecord:
    session_id: str
    preview: SensitiveEnvPreview
    candidate: bytes = field(repr=False)
    base_document: dict[str, Any] | None = field(repr=False)
    owner_transition: RepoEnvironmentTransition = field(repr=False)
    current_revision: DocumentRevision
    expires_at: float


@dataclass(frozen=True, slots=True, repr=False)
class _ClearReceiptRecord:
    session_id: str
    preview_id: str
    current_revision: DocumentRevision
    semantic_preview: SensitiveEnvPreview
    owner_transition: RepoEnvironmentTransition = field(repr=False)
    expires_at: float


class SensitiveEnvError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SensitiveEnvDocument:
    """Own one fixed repository dotenv without consulting process globals."""

    def __init__(
        self,
        document_path: str | Path,
        *,
        backup_root: str | Path,
        environment_owner: LoadedSecrets,
        base_document: Mapping[str, Any] | None = None,
        base_document_owner: Callable[[], Mapping[str, Any]] | None = None,
        manager: ConfigManager | None = None,
        clock: Callable[[], float] = time.monotonic,
        receipt_factory: Callable[[], str] = lambda: secrets.token_urlsafe(24),
        preview_factory: Callable[[], str] = lambda: secrets.token_urlsafe(24),
        receipt_ttl_seconds: float = 60.0,
        platform_capabilities: PlatformCapabilities,
    ) -> None:
        if receipt_ttl_seconds <= 0:
            raise ValueError("receipt TTL must be positive")
        if not isinstance(environment_owner, LoadedSecrets):
            raise TypeError("environment_owner must be LoadedSecrets")
        if not isinstance(platform_capabilities, PlatformCapabilities):
            raise TypeError("platform_capabilities must be PlatformCapabilities")
        self._platform = platform_capabilities
        self._document_path = Path(document_path)
        self._backup_root = Path(backup_root)
        self._environment_owner = environment_owner
        self._base_document = copy.deepcopy(dict(base_document or {}))
        if base_document_owner is not None and not callable(base_document_owner):
            raise TypeError("base_document_owner must be callable")
        self._base_document_owner = base_document_owner
        self._manager = manager or ConfigManager()
        self._clock = clock
        self._receipt_factory = receipt_factory
        self._preview_factory = preview_factory
        self._receipt_ttl_seconds = receipt_ttl_seconds
        self._direct_session_id = secrets.token_urlsafe(24)
        self._preview_records: dict[str, _PreviewRecord] = {}
        self._clear_receipts: dict[str, _ClearReceiptRecord] = {}
        self._preview_lock = threading.Lock()
        self._receipts: dict[str, _ReceiptRecord] = {}
        self._receipts_lock = threading.Lock()
        self._transaction = ManagedDocumentTransaction(
            self._document_path,
            backup_root=self._backup_root,
            lock_root=self._backup_root.parent / "locks",
            retention=1,
            publish_mode=0o600,
            private_posix=True,
            platform_capabilities=self._platform,
        )

    def __repr__(self) -> str:
        return "SensitiveEnvDocument(<redacted>)"

    def status(self) -> SensitiveEnvStatus:
        snapshot, permission_health = self._snapshot_and_permission_health()
        resolved = (
            self._environment_owner.resolve_repo_dotenv(snapshot.content)
            if snapshot is not None
            else None
        )
        secret_slots = tuple(
            SecretSlotStatus(
                slot=slot,
                configured=(
                    resolved.secret_configured(slot)
                    if resolved is not None
                    else False
                ),
            )
            for slot in SECRETS_ENV_MAP
        )
        return SensitiveEnvStatus(
            secret_slots=secret_slots,
            permission_health=permission_health,
        )

    def preview(
        self,
        command: ClearMappedOverride | SetSecret | ClearSecret,
        *,
        session_id: str | None = None,
    ) -> SensitiveEnvPreview:
        bound_session = self._bound_session_id(session_id)
        snapshot, permission_health = self._snapshot_and_permission_health()
        if snapshot is None:
            raise SensitiveEnvError(permission_health, "sensitive document is unsafe")
        if isinstance(command, ClearMappedOverride):
            environment_name = command.environment_variable
            if environment_name not in _MANAGED_OVERRIDES:
                raise SensitiveEnvError(
                    "OVERRIDE_NOT_MANAGED", "environment override is not managed"
                )
            candidate = _remove_definitions(snapshot.content, {environment_name})
            command_kind = "clear_mapped_override"
            target = environment_name
        elif isinstance(command, (SetSecret, ClearSecret)):
            environment_name = SECRETS_ENV_MAP.get(command.slot)
            if environment_name is None:
                raise SensitiveEnvError("SECRET_SLOT_INVALID", "secret slot is invalid")
            candidate = _remove_definitions(snapshot.content, {environment_name})
            if isinstance(command, SetSecret):
                if (
                    not isinstance(command.value, str)
                    or not command.value
                    or "\x00" in command.value
                ):
                    raise SensitiveEnvError(
                        "SECRET_VALUE_INVALID", "secret value is invalid"
                    )
                candidate = _append_secret(
                    candidate,
                    environment_name,
                    command.value,
                    line_ending=(
                        b"\r\n" if b"\r\n" in snapshot.content else b"\n"
                    ),
                )
                if not self._environment_owner.repo_secret_roundtrips(
                    candidate,
                    slot=command.slot,
                    expected_value=command.value,
                ):
                    raise SensitiveEnvError(
                        "SECRET_VALUE_UNREPRESENTABLE",
                        "secret value cannot round-trip through the dotenv owner",
                    )
                command_kind = "set_secret"
            else:
                command_kind = "clear_secret"
            target = command.slot
        else:
            raise SensitiveEnvError("COMMAND_UNSUPPORTED", "unsupported command")
        base_document = (
            self._current_base_document()
            if isinstance(command, ClearMappedOverride)
            else None
        )
        owner_transition = self._environment_owner.resolve_repo_transition(
            snapshot.content,
            candidate,
        )
        return self._store_preview(
            session_id=bound_session,
            candidate=candidate,
            base_document=base_document,
            revision=snapshot.revision,
            owner_transition=owner_transition,
            semantic_fields=self._semantic_fields(
                current_content=snapshot.content,
                candidate=candidate,
                permission_health=permission_health,
                command_kind=command_kind,
                target=target,
                base_document=base_document,
                transition=owner_transition,
            ),
        )

    def commit(
        self,
        preview: SensitiveEnvPreview,
        *,
        session_id: str | None = None,
        confirmation_token: str | None = None,
    ) -> SensitiveEnvCommit:
        if not isinstance(preview, SensitiveEnvPreview):
            raise SensitiveEnvError("PREVIEW_INVALID", "preview does not belong here")
        if not self._platform.sensitive_document_writes:
            raise SensitiveEnvError(
                "SENSITIVE_WRITES_UNVERIFIED_ON_WINDOWS",
                "sensitive writes require verified owner-only DACL support",
            )
        bound_session = self._bound_session_id(session_id)
        if preview.command_kind == "clear_secret":
            if not confirmation_token:
                raise SensitiveEnvError(
                    "SECRET_CLEAR_CONFIRMATION_REQUIRED",
                    "clearing a secret requires explicit confirmation",
                )
            self._consume_clear_confirmation(
                confirmation_token,
                preview=preview,
                session_id=bound_session,
            )
        record = self._consume_preview(preview, session_id=bound_session)
        self._assert_backup_root_safe()
        snapshot, permission_health = self._snapshot_and_permission_health()
        if snapshot is None:
            raise SensitiveEnvError(permission_health, "sensitive document is unsafe")
        if snapshot.revision != record.current_revision:
            raise SensitiveEnvError(
                "DOCUMENT_CONFLICT",
                "sensitive document changed after preview",
            )
        def recheck_owner_state() -> None:
            if record.base_document is not None:
                try:
                    current_base_document = self._current_base_document()
                except SensitiveEnvError as exc:
                    if exc.code != "PREVIEW_UNAVAILABLE":
                        raise
                    raise SensitiveEnvError(
                        "CONFIRMATION_REQUIRED",
                        "app document changed after sensitive preview",
                    ) from exc
                if current_base_document != record.base_document:
                    raise SensitiveEnvError(
                        "CONFIRMATION_REQUIRED",
                        "app document changed after sensitive preview",
                    )
            try:
                current_transition = self._environment_owner.resolve_repo_transition(
                    snapshot.content,
                    record.candidate,
                )
            except EnvironmentRefreshError as exc:
                raise SensitiveEnvError(
                    "CONFIRMATION_REQUIRED",
                    "sensitive owner changed after preview",
                ) from exc
            if not record.owner_transition.same_secret_material(current_transition):
                raise SensitiveEnvError(
                    "CONFIRMATION_REQUIRED",
                    "sensitive owner material changed after preview",
                )
            current_preview = SensitiveEnvPreview(
                preview_id=record.preview.preview_id,
                **self._semantic_fields(
                    current_content=snapshot.content,
                    candidate=record.candidate,
                    permission_health=permission_health,
                    command_kind=record.preview.command_kind,
                    target=record.preview.target,
                    base_document=record.base_document,
                    transition=current_transition,
                ),
            )
            if current_preview != record.preview:
                raise SensitiveEnvError(
                    "CONFIRMATION_REQUIRED",
                    "sensitive preview semantics changed",
                )

        recheck_owner_state()
        try:
            result = self._transaction.commit(
                record.candidate,
                expected_revision=record.current_revision,
                defer_retention=True,
                before_publication=recheck_owner_state,
            )
        except DocumentConflictError as exc:
            raise SensitiveEnvError(
                "DOCUMENT_CONFLICT",
                "sensitive document changed during publication",
            ) from exc
        except PermissionError as exc:
            raise SensitiveEnvError(
                "PERMISSION_HARDENING_FAILED",
                "sensitive document permissions could not be hardened",
            ) from exc
        final_permission_health, retention_maintenance = (
            self._finalize_sensitive_publication(
                result,
                previous=snapshot,
            )
        )
        return SensitiveEnvCommit(
            restore_point_id=(
                result.restore_point.id if result.restore_point is not None else None
            ),
            permission_health=final_permission_health,
            maintenance_code=result.maintenance_code or retention_maintenance,
        )

    def prepare_secret_clear(
        self,
        preview: SensitiveEnvPreview,
        *,
        session_id: str | None = None,
    ) -> SensitiveCommitConfirmation:
        """Issue a short-lived, one-use receipt for one clear-secret preview."""

        if not isinstance(preview, SensitiveEnvPreview):
            raise SensitiveEnvError("PREVIEW_INVALID", "preview does not belong here")
        bound_session = self._bound_session_id(session_id)
        record = self._peek_preview(preview, session_id=bound_session)
        if preview.command_kind != "clear_secret":
            raise SensitiveEnvError(
                "SECRET_CLEAR_CONFIRMATION_INVALID",
                "confirmation is only available for secret clearing",
            )
        now = self._clock()
        with self._preview_lock:
            self._clear_receipts = {
                token: stored
                for token, stored in self._clear_receipts.items()
                if stored.expires_at > now
            }
            for _ in range(32):
                token = self._receipt_factory()
                if token and token not in self._clear_receipts:
                    self._clear_receipts[token] = _ClearReceiptRecord(
                        session_id=bound_session,
                        preview_id=preview.preview_id,
                        current_revision=record.current_revision,
                        semantic_preview=preview,
                        owner_transition=record.owner_transition,
                        expires_at=now + self._receipt_ttl_seconds,
                    )
                    return SensitiveCommitConfirmation(
                        receipt_token=token,
                        preview=preview,
                    )
        raise SensitiveEnvError(
            "SECRET_CLEAR_CONFIRMATION_UNAVAILABLE",
            "could not issue secret clear confirmation",
        )

    def restore_points(self) -> tuple[RestorePointMetadata, ...]:
        """Return opaque bounded metadata for this sensitive document only."""

        self._assert_backup_root_safe()
        try:
            return self._transaction.restore_points()
        except DocumentTransactionError as exc:
            raise SensitiveEnvError(
                "SENSITIVE_BACKUP_UNSAFE",
                "sensitive restore points are unavailable",
            ) from exc

    def prepare_rollback(
        self,
        restore_point_id: str,
        *,
        session_id: str,
    ) -> RollbackConfirmation:
        if not session_id:
            raise SensitiveEnvError("SESSION_INVALID", "session is required")
        if not self._platform.sensitive_document_writes:
            raise SensitiveEnvError(
                "SENSITIVE_WRITES_UNVERIFIED_ON_WINDOWS",
                "sensitive rollback requires verified owner-only DACL support",
            )
        self._assert_backup_root_safe()
        current, permission_health = self._snapshot_and_permission_health()
        if current is None:
            raise SensitiveEnvError(permission_health, "sensitive document is unsafe")
        try:
            restored = self._transaction.restore_snapshot(restore_point_id)
        except RestorePointError as exc:
            raise SensitiveEnvError(
                "NO_VALID_RESTORE_POINT", "restore point is not available"
            ) from exc
        if restored.revision.exists and _dotenv_has_errors(restored.content):
            raise SensitiveEnvError(
                "NO_VALID_RESTORE_POINT", "restore point dotenv is invalid"
            )
        owner_transition = self._environment_owner.resolve_repo_transition(
            current.content,
            restored.content,
        )
        preview = self._rollback_preview(
            restore_point_id=restore_point_id,
            current=current,
            restored=restored,
            permission_health=permission_health,
            transition=owner_transition,
        )
        if preview.resolution_error_after:
            raise SensitiveEnvError(
                "NO_VALID_RESTORE_POINT",
                "restore point cannot be resolved by the production owner",
            )
        receipt_token = self._issue_receipt(
            session_id=session_id,
            current_revision=current.revision,
            preview=preview,
            owner_transition=owner_transition,
        )
        return RollbackConfirmation(receipt_token=receipt_token, preview=preview)

    def rollback(
        self,
        receipt_token: str,
        *,
        session_id: str,
    ) -> SensitiveEnvCommit:
        if not self._platform.sensitive_document_writes:
            raise SensitiveEnvError(
                "SENSITIVE_WRITES_UNVERIFIED_ON_WINDOWS",
                "sensitive rollback requires verified owner-only DACL support",
            )
        self._assert_backup_root_safe()
        record = self._consume_receipt(receipt_token, session_id=session_id)
        current, permission_health = self._snapshot_and_permission_health()
        if current is None:
            raise SensitiveEnvError(permission_health, "sensitive document is unsafe")
        if current.revision != record.current_revision:
            raise SensitiveEnvError(
                "DOCUMENT_CONFLICT", "sensitive document revision changed"
            )
        try:
            restored = self._transaction.restore_snapshot(
                record.preview.restore_point_id
            )
        except RestorePointError as exc:
            raise SensitiveEnvError(
                "NO_VALID_RESTORE_POINT", "restore point is not available"
            ) from exc
        def recheck_owner_state() -> None:
            try:
                current_transition = self._environment_owner.resolve_repo_transition(
                    current.content,
                    restored.content,
                )
            except EnvironmentRefreshError as exc:
                raise SensitiveEnvError(
                    "ROLLBACK_CONFIRMATION_INVALID",
                    "rollback owner changed after confirmation",
                ) from exc
            if not record.owner_transition.same_secret_material(current_transition):
                raise SensitiveEnvError(
                    "ROLLBACK_CONFIRMATION_INVALID",
                    "rollback owner material changed",
                )
            try:
                current_preview = self._rollback_preview(
                    restore_point_id=record.preview.restore_point_id,
                    current=current,
                    restored=restored,
                    permission_health=permission_health,
                    transition=current_transition,
                )
            except SensitiveEnvError as exc:
                if exc.code != "PREVIEW_UNAVAILABLE":
                    raise
                raise SensitiveEnvError(
                    "ROLLBACK_CONFIRMATION_INVALID",
                    "rollback app owner changed",
                ) from exc
            if current_preview != record.preview:
                raise SensitiveEnvError(
                    "ROLLBACK_CONFIRMATION_INVALID", "rollback semantics changed"
                )

        recheck_owner_state()
        try:
            result = self._transaction.rollback(
                record.preview.restore_point_id,
                expected_revision=record.current_revision,
                defer_retention=True,
                before_publication=recheck_owner_state,
            )
        except DocumentConflictError as exc:
            raise SensitiveEnvError(
                "DOCUMENT_CONFLICT",
                "sensitive document changed during publication",
            ) from exc
        except PermissionError as exc:
            raise SensitiveEnvError(
                "PERMISSION_HARDENING_FAILED",
                "sensitive document permissions could not be hardened",
            ) from exc
        final_permission_health, retention_maintenance = (
            self._finalize_sensitive_publication(
                result,
                previous=current,
            )
        )
        return SensitiveEnvCommit(
            restore_point_id=(
                result.restore_point.id if result.restore_point is not None else None
            ),
            permission_health=final_permission_health,
            maintenance_code=result.maintenance_code or retention_maintenance,
        )

    def _finalize_sensitive_publication(
        self,
        result: DocumentCommit,
        *,
        previous: ManagedDocumentSnapshot,
    ) -> tuple[str, str | None]:
        live, permission_health = self._snapshot_and_permission_health()
        if live is None or not self._transaction.publication_matches(
            live,
            result.snapshot,
        ):
            raise SensitiveEnvError(
                "DOCUMENT_CONFLICT",
                "sensitive document changed after publication",
            )
        publication_is_private = (
            result.snapshot.revision.exists and permission_health == "PRIVATE"
        ) or (
            not result.snapshot.revision.exists and permission_health == "MISSING"
        )
        retention_maintenance: str | None = None
        if publication_is_private:
            try:
                retention_maintenance = (
                    self._transaction.finalize_deferred_retention(
                        expected_snapshot=result.snapshot,
                        protected_restore_point_id=(
                            result.restore_point.id
                            if result.restore_point is not None
                            else None
                        ),
                    )
                )
            except DocumentConflictError as exc:
                raise SensitiveEnvError(
                    "DOCUMENT_CONFLICT",
                    "sensitive document changed before retention",
                ) from exc
            live_after_retention, health_after_retention = (
                self._snapshot_and_permission_health()
            )
            if (
                live_after_retention is None
                or not self._transaction.publication_matches(
                    live_after_retention,
                    result.snapshot,
                )
            ):
                raise SensitiveEnvError(
                    "DOCUMENT_CONFLICT",
                    "sensitive document changed after retention",
                )
            expected_health = (
                "PRIVATE" if result.snapshot.revision.exists else "MISSING"
            )
            if health_after_retention == expected_health:
                return health_after_retention, retention_maintenance

        try:
            recovered = self._transaction.recover_failed_publication(
                (
                    result.restore_point.id
                    if result.restore_point is not None
                    else None
                ),
                expected_snapshot=result.snapshot,
                previous_revision=previous.revision,
            )
            if recovered.revision != previous.revision:
                raise DocumentSafetyError(
                    "sensitive recovery did not restore previous bytes"
                )
            recovered_live, recovered_health = (
                self._snapshot_and_permission_health()
            )
            expected_health = "PRIVATE" if previous.revision.exists else "MISSING"
            if recovered_live is None or not self._transaction.publication_matches(
                recovered_live,
                recovered,
            ):
                raise DocumentConflictError(
                    "sensitive document changed after recovery"
                )
            if (
                recovered_live.revision != previous.revision
                or recovered_health != expected_health
            ):
                raise DocumentSafetyError(
                    "sensitive recovery could not prove the previous state"
                )
        except DocumentConflictError as exc:
            raise SensitiveEnvError(
                "DOCUMENT_CONFLICT",
                "sensitive document changed before recovery",
            ) from exc
        except (DocumentTransactionError, OSError) as exc:
            raise SensitiveEnvError(
                "PERMISSION_HARDENING_FAILED",
                "sensitive publication permissions are unsafe",
            ) from exc
        raise SensitiveEnvError(
            "PERMISSION_HARDENING_FAILED",
            "sensitive publication permissions are unsafe",
        )

    def _rollback_preview(
        self,
        *,
        restore_point_id: str,
        current: ManagedDocumentSnapshot,
        restored: ManagedDocumentSnapshot,
        permission_health: str,
        transition: RepoEnvironmentTransition,
    ) -> SensitiveRollbackPreview:
        before_environment = transition.before
        after_environment = transition.after
        base_document = self._current_base_document()
        before_resolution, before_resolution_error = self._resolve(
            before_environment,
            base_document=base_document,
        )
        after_resolution, after_resolution_error = self._resolve(
            after_environment,
            base_document=base_document,
        )
        secret_changes = tuple(
            SecretRollbackChange(
                slot=slot,
                change=transition.repo_change(environment_name),
            )
            for slot, environment_name in SECRETS_ENV_MAP.items()
        )
        override_changes: list[OverrideRollbackChange] = []
        for environment_name, field_path in sorted(_MANAGED_OVERRIDES.items()):
            before_value, before_source = self._resolved_field(
                field_path,
                environment_name,
                environment=before_environment,
                resolution=before_resolution,
            )
            after_value, after_source = self._resolved_field(
                field_path,
                environment_name,
                environment=after_environment,
                resolution=after_resolution,
            )
            before_value = _sanitize_transition_value(
                before_value,
                transition,
            )
            after_value = _sanitize_transition_value(
                after_value,
                transition,
            )
            repo_changed = transition.repo_changed(environment_name)
            if not repo_changed and (before_value, before_source) == (
                after_value,
                after_source,
            ):
                continue
            override_changes.append(
                OverrideRollbackChange(
                    environment_variable=environment_name,
                    affected_fields=(field_path,),
                    before_next_launch=before_value,
                    after_next_launch=after_value,
                    winning_source_before=before_source,
                    winning_source_after=after_source,
                    still_shadowed=after_source in {"inherited", "parent_dotenv"},
                )
            )
        unmanaged_change_count = _unmanaged_change_count(
            current.content, restored.content
        )
        return SensitiveRollbackPreview(
            restore_point_id=restore_point_id,
            secret_changes=secret_changes,
            override_changes=tuple(override_changes),
            unmanaged_content_changed=unmanaged_change_count > 0,
            unmanaged_change_count=unmanaged_change_count,
            permission_hardening=permission_health == "TOO_PERMISSIVE",
            resolution_error_before=(
                _dotenv_has_errors(current.content) or before_resolution_error
            ),
            resolution_error_after=(
                _dotenv_has_errors(restored.content) or after_resolution_error
            ),
        )

    def _resolve(
        self,
        environment: ResolvedRepoEnvironment,
        *,
        base_document: Mapping[str, Any],
    ) -> tuple[ConfigResolution | None, bool]:
        if set(environment.tainted_environment_names) & set(_MANAGED_OVERRIDES):
            return None, True
        try:
            return self._manager.resolve_snapshot(
                copy.deepcopy(dict(base_document)),
                environment.environment_snapshot,
            ), False
        except (TypeError, ValueError):
            return None, True

    def _semantic_fields(
        self,
        *,
        current_content: bytes,
        candidate: bytes,
        permission_health: str,
        command_kind: str,
        target: str,
        base_document: Mapping[str, Any] | None,
        transition: RepoEnvironmentTransition,
    ) -> dict[str, Any]:
        before_environment = transition.before
        after_environment = transition.after
        if command_kind == "clear_mapped_override":
            field_path = _MANAGED_OVERRIDES.get(target)
            if field_path is None or base_document is None:
                raise SensitiveEnvError(
                    "PREVIEW_INVALID",
                    "sensitive preview semantics are invalid",
                )
            before_resolution, before_resolution_error = self._resolve(
                before_environment,
                base_document=base_document,
            )
            after_resolution, after_resolution_error = self._resolve(
                after_environment,
                base_document=base_document,
            )
            before_value, before_source = self._resolved_field(
                field_path,
                target,
                environment=before_environment,
                resolution=before_resolution,
            )
            after_value, after_source = self._resolved_field(
                field_path,
                target,
                environment=after_environment,
                resolution=after_resolution,
            )
            before_value = _sanitize_transition_value(before_value, transition)
            after_value = _sanitize_transition_value(after_value, transition)
            affected_fields = (field_path,)
            secret_change = None
        elif command_kind in {"set_secret", "clear_secret"}:
            environment_name = SECRETS_ENV_MAP.get(target)
            if environment_name is None or base_document is not None:
                raise SensitiveEnvError(
                    "PREVIEW_INVALID",
                    "sensitive preview semantics are invalid",
                )
            before_value = after_value = None
            before_source = before_environment.secret_source(target)
            after_source = after_environment.secret_source(target)
            before_resolution_error = False
            after_resolution_error = False
            affected_fields = ()
            secret_change = transition.repo_change(environment_name)
        else:
            raise SensitiveEnvError(
                "PREVIEW_INVALID",
                "sensitive preview semantics are invalid",
            )
        return {
            "command_kind": command_kind,
            "target": target,
            "affected_fields": affected_fields,
            "before_next_launch": before_value,
            "after_next_launch": after_value,
            "winning_source_before": before_source,
            "winning_source_after": after_source,
            "still_shadowed": after_source in {"inherited", "parent_dotenv"},
            "permission_hardening": permission_health == "TOO_PERMISSIVE",
            "changed": candidate != current_content,
            "secret_change": secret_change,
            "resolution_error_before": (
                _dotenv_has_errors(current_content) or before_resolution_error
            ),
            "resolution_error_after": (
                _dotenv_has_errors(candidate) or after_resolution_error
            ),
        }

    def _current_base_document(self) -> dict[str, Any]:
        if self._base_document_owner is None:
            return copy.deepcopy(self._base_document)
        try:
            document = self._base_document_owner()
        except Exception:  # noqa: BLE001 -- owner errors stay bounded
            raise SensitiveEnvError(
                "PREVIEW_UNAVAILABLE",
                "app document owner is unavailable",
            ) from None
        if not isinstance(document, Mapping):
            raise SensitiveEnvError(
                "PREVIEW_UNAVAILABLE",
                "app document owner is unavailable",
            )
        return copy.deepcopy(dict(document))

    def _resolved_field(
        self,
        field_path: str,
        environment_name: str,
        *,
        environment: ResolvedRepoEnvironment,
        resolution: ConfigResolution | None,
    ) -> tuple[Any, str | None]:
        if resolution is None:
            return None, environment.environment_snapshot.layer_for(
                environment_name
            )
        leaf = resolution.resolved_at(tuple(field_path.split(".")))
        source = (
            leaf.source.environment_layer
            if leaf.source.kind == "env_override"
            else leaf.source.kind
        )
        return leaf.next_launch_value, source

    def _bound_session_id(self, session_id: str | None) -> str:
        if session_id is None:
            return self._direct_session_id
        if not isinstance(session_id, str) or not session_id:
            raise SensitiveEnvError("SESSION_INVALID", "session is required")
        return session_id

    def _store_preview(
        self,
        *,
        session_id: str,
        candidate: bytes,
        base_document: Mapping[str, Any] | None,
        revision: DocumentRevision,
        owner_transition: RepoEnvironmentTransition,
        semantic_fields: Mapping[str, Any],
    ) -> SensitiveEnvPreview:
        now = self._clock()
        with self._preview_lock:
            self._preview_records = {
                token: record
                for token, record in self._preview_records.items()
                if record.expires_at > now
            }
            if len(self._preview_records) >= 64:
                raise SensitiveEnvError(
                    "PREVIEW_UNAVAILABLE", "too many active sensitive previews"
                )
            for _ in range(32):
                preview_id = self._preview_factory()
                if (
                    isinstance(preview_id, str)
                    and 20 <= len(preview_id) <= 128
                    and preview_id not in self._preview_records
                ):
                    preview = SensitiveEnvPreview(
                        preview_id=preview_id,
                        **semantic_fields,
                    )
                    self._preview_records[preview_id] = _PreviewRecord(
                        session_id=session_id,
                        preview=preview,
                        candidate=bytes(candidate),
                        base_document=(
                            copy.deepcopy(dict(base_document))
                            if base_document is not None
                            else None
                        ),
                        owner_transition=owner_transition,
                        current_revision=revision,
                        expires_at=now + self._receipt_ttl_seconds,
                    )
                    return preview
        raise SensitiveEnvError(
            "PREVIEW_UNAVAILABLE", "could not allocate sensitive preview"
        )

    def _peek_preview(
        self,
        preview: SensitiveEnvPreview,
        *,
        session_id: str,
    ) -> _PreviewRecord:
        return self._get_preview(preview, session_id=session_id, consume=False)

    def _consume_preview(
        self,
        preview: SensitiveEnvPreview,
        *,
        session_id: str,
    ) -> _PreviewRecord:
        return self._get_preview(preview, session_id=session_id, consume=True)

    def _get_preview(
        self,
        preview: SensitiveEnvPreview,
        *,
        session_id: str,
        consume: bool,
    ) -> _PreviewRecord:
        with self._preview_lock:
            record = self._preview_records.get(preview.preview_id)
            if record is None:
                raise SensitiveEnvError("PREVIEW_INVALID", "preview is invalid")
            if self._clock() >= record.expires_at:
                self._preview_records.pop(preview.preview_id, None)
                raise SensitiveEnvError("PREVIEW_EXPIRED", "preview expired")
            if (
                not hmac.compare_digest(record.session_id, session_id)
                or record.preview != preview
            ):
                raise SensitiveEnvError("PREVIEW_INVALID", "preview is invalid")
            if consume:
                self._preview_records.pop(preview.preview_id, None)
            return record

    def _consume_clear_confirmation(
        self,
        receipt_token: str,
        *,
        preview: SensitiveEnvPreview,
        session_id: str,
    ) -> None:
        with self._preview_lock:
            record = self._clear_receipts.get(receipt_token)
            if record is None:
                raise SensitiveEnvError(
                    "SECRET_CLEAR_CONFIRMATION_INVALID",
                    "secret clear receipt is invalid",
                )
            if self._clock() >= record.expires_at:
                self._clear_receipts.pop(receipt_token, None)
                raise SensitiveEnvError(
                    "SECRET_CLEAR_CONFIRMATION_EXPIRED",
                    "secret clear receipt expired",
                )
            preview_record = self._preview_records.get(preview.preview_id)
            valid = (
                hmac.compare_digest(record.session_id, session_id)
                and hmac.compare_digest(record.preview_id, preview.preview_id)
                and record.semantic_preview == preview
                and preview_record is not None
                and preview_record.current_revision == record.current_revision
                and preview_record.owner_transition.same_secret_material(
                    record.owner_transition
                )
            )
            if not valid:
                raise SensitiveEnvError(
                    "SECRET_CLEAR_CONFIRMATION_INVALID",
                    "secret clear receipt is invalid",
                )
            self._clear_receipts.pop(receipt_token, None)

    def _issue_receipt(
        self,
        *,
        session_id: str,
        current_revision: DocumentRevision,
        preview: SensitiveRollbackPreview,
        owner_transition: RepoEnvironmentTransition,
    ) -> str:
        now = self._clock()
        with self._receipts_lock:
            self._receipts = {
                token: record
                for token, record in self._receipts.items()
                if record.expires_at > now
            }
            for _ in range(32):
                token = self._receipt_factory()
                if token and token not in self._receipts:
                    self._receipts[token] = _ReceiptRecord(
                        session_id=session_id,
                        current_revision=current_revision,
                        preview=preview,
                        owner_transition=owner_transition,
                        expires_at=now + self._receipt_ttl_seconds,
                    )
                    return token
        raise SensitiveEnvError(
            "ROLLBACK_CONFIRMATION_UNAVAILABLE", "could not issue rollback receipt"
        )

    def _consume_receipt(
        self,
        receipt_token: str,
        *,
        session_id: str,
    ) -> _ReceiptRecord:
        with self._receipts_lock:
            record = self._receipts.get(receipt_token)
            if record is None:
                raise SensitiveEnvError(
                    "ROLLBACK_CONFIRMATION_INVALID", "rollback receipt is invalid"
                )
            if self._clock() >= record.expires_at:
                self._receipts.pop(receipt_token, None)
                raise SensitiveEnvError(
                    "ROLLBACK_CONFIRMATION_EXPIRED", "rollback receipt expired"
                )
            if not hmac.compare_digest(record.session_id, session_id):
                raise SensitiveEnvError(
                    "ROLLBACK_CONFIRMATION_INVALID", "rollback receipt is invalid"
                )
            self._receipts.pop(receipt_token, None)
            return record

    def _snapshot_and_permission_health(
        self,
    ) -> tuple[ManagedDocumentSnapshot | None, str]:
        if (
            self._platform.posix_permissions
            and self._platform.sensitive_document_writes
        ):
            inspection = self._transaction.inspect_private()
            return inspection.snapshot, inspection.permission_health
        try:
            document_stat = self._document_path.lstat()
        except FileNotFoundError:
            snapshot = self._transaction.preview(b"").current
            return snapshot, "MISSING"
        if not stat.S_ISREG(document_stat.st_mode):
            return None, "DOCUMENT_UNSAFE"
        if self._platform.posix_permissions:
            if document_stat.st_uid != self._platform.user_id:
                return None, "WRONG_OWNER"
            if document_stat.st_nlink != 1:
                return None, "MULTIPLE_LINKS"
        try:
            snapshot = self._transaction.preview(b"").current
        except DocumentSafetyError:
            return None, "DOCUMENT_UNSAFE"
        if self._platform.posix_permissions:
            permission_health = (
                "PRIVATE"
                if stat.S_IMODE(document_stat.st_mode) == 0o600
                else "TOO_PERMISSIVE"
            )
        else:
            permission_health = "DACL_UNVERIFIED"
        return snapshot, permission_health

    def _assert_backup_root_safe(self) -> None:
        try:
            backup_stat = self._backup_root.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(backup_stat.st_mode):
            raise SensitiveEnvError(
                "SENSITIVE_BACKUP_UNSAFE", "sensitive backup root is unsafe"
            )
        if self._platform.posix_permissions and (
            backup_stat.st_uid != self._platform.user_id
            or stat.S_IMODE(backup_stat.st_mode) != 0o700
        ):
            raise SensitiveEnvError(
                "SENSITIVE_BACKUP_UNSAFE", "sensitive backup root is not private"
            )


def _sanitize_transition_value(
    value: Any,
    transition: RepoEnvironmentTransition,
) -> Any:
    if isinstance(value, str):
        return transition.sanitize_secret_material(value)
    if isinstance(value, Mapping):
        return {
            transition.sanitize_secret_material(str(key)):
            _sanitize_transition_value(child, transition)
            for key, child in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [
            _sanitize_transition_value(child, transition)
            for child in value
        ]
    if value is None or type(value) in (bool, int, float):
        try:
            canonical_value = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except ValueError:
            return value
        sanitized = transition.sanitize_secret_material(canonical_value)
        if sanitized != canonical_value:
            return sanitized
        return value
    return "<unsupported-value>"


def _dotenv_has_errors(content: bytes) -> bool:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return any(binding.error for binding in parse_stream(io.StringIO(text)))


def _remove_definitions(content: bytes, names: set[str]) -> bytes:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SensitiveEnvError("DOTENV_INVALID", "dotenv is not valid UTF-8") from exc
    output: list[str] = []
    consumed: list[str] = []
    for binding in parse_stream(io.StringIO(text)):
        original = binding.original.string
        consumed.append(original)
        match = _ASSIGNMENT.search(original)
        parsed_key = binding.key
        detected_key = match.group("key") if match is not None else None
        key = parsed_key if parsed_key is not None else detected_key
        if key not in names:
            output.append(original)
            continue
        if match is None or match.group("key") != key:
            raise SensitiveEnvError("DOTENV_INVALID", "dotenv assignment is invalid")
        assignment_start = (
            match.start("export")
            if match.group("export") is not None
            else match.start("key")
        )
        output.append(original[:assignment_start])
    if "".join(consumed) != text:
        raise SensitiveEnvError("DOTENV_INVALID", "dotenv parser lost input bytes")
    return "".join(output).encode("utf-8")


def _append_secret(
    content: bytes,
    environment_name: str,
    value: str,
    *,
    line_ending: bytes,
) -> bytes:
    candidate = content
    if candidate and not candidate.endswith((b"\n", b"\r")):
        candidate += line_ending
    quoted = value.replace("\\", "\\\\").replace("'", "\\'")
    return (
        candidate
        + environment_name.encode("ascii")
        + b"='"
        + quoted.encode("utf-8")
        + b"'"
        + line_ending
    )


def _unmanaged_change_count(before: bytes, after: bytes) -> int:
    managed_names = set(_MANAGED_OVERRIDES) | set(SECRETS_ENV_MAP.values())
    before_segments = _unmanaged_segments(before, managed_names)
    after_segments = _unmanaged_segments(after, managed_names)
    common_length = min(len(before_segments), len(after_segments))
    return sum(
        before_segments[index] != after_segments[index]
        for index in range(common_length)
    ) + abs(
        len(before_segments) - len(after_segments)
    )


def _unmanaged_segments(content: bytes, managed_names: set[str]) -> tuple[str, ...]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return ("<invalid-utf8>",)
    segments: list[str] = []
    for binding in parse_stream(io.StringIO(text)):
        original = binding.original.string
        match = _ASSIGNMENT.search(original)
        key = binding.key or (match.group("key") if match is not None else None)
        if key not in managed_names:
            segments.append(original)
    return tuple(segments)
