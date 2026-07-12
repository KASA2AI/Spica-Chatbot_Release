"""Typed authoring service for the fixed overlay preference document."""

from __future__ import annotations

import hmac
import json
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from spica.config.document_transaction import (
    DocumentRevision,
    DocumentTransactionError,
    ManagedDocumentTransaction,
    RestorePointMetadata,
)
from spica.config.overlay_owner import (
    OverlayConfig,
    overlay_field_bounds,
    resolve_overlay_config,
)
from spica.ports.config_studio_platform import PlatformCapabilities
from spica.config_studio.overlay_contract import OverlayOwnerError, OverlaySetValue


_MAX_DOCUMENT_BYTES = 1024 * 1024


class OverlayDocumentError(OverlayOwnerError):
    def __repr__(self) -> str:
        return f"OverlayDocumentError(code={self.code!r})"


@dataclass(frozen=True, slots=True)
class OverlayDocumentStatus:
    recovery_only: bool
    error_code: str | None


@dataclass(frozen=True, slots=True, repr=False)
class OverlayChangePreview:
    preview_id: str = field(repr=False)
    key: str
    file_value_before: float = field(repr=False)
    file_value_after: float = field(repr=False)
    changed: bool
    effect_policy: str = "next_spica_launch"

    def __repr__(self) -> str:
        return (
            f"OverlayChangePreview(key={self.key!r}, changed={self.changed!r}, "
            "values=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class OverlayDocumentCommit:
    restore_point_id: str | None
    maintenance_code: str | None = None
    effect_policy: str = "next_spica_launch"


@dataclass(frozen=True, slots=True)
class OverlayRollbackPreview:
    restore_point_id: str
    changed_fields: tuple[str, ...]
    unmanaged_content_changed: bool
    unmanaged_change_count: int
    resolution_error_before: bool
    resolution_error_after: bool = False
    effect_policy: str = "next_spica_launch"


@dataclass(frozen=True, slots=True, repr=False)
class OverlayRollbackConfirmation:
    receipt_token: str = field(repr=False)
    preview: OverlayRollbackPreview

    def __repr__(self) -> str:
        return f"OverlayRollbackConfirmation(preview={self.preview!r}, receipt=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class _PreviewRecord:
    session_id: str
    candidate: bytes = field(repr=False)
    revision: DocumentRevision
    expires_at: float


@dataclass(frozen=True, slots=True, repr=False)
class _RollbackRecord:
    session_id: str
    revision: DocumentRevision
    preview: OverlayRollbackPreview
    expires_at: float


class OverlayConfigDocument:
    """Preview, CAS-publish, and rollback owner-validated overlay preferences."""

    def __init__(
        self,
        document_path: str | Path,
        *,
        backup_root: str | Path,
        platform_capabilities: PlatformCapabilities,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] | None = None,
        preview_ttl_seconds: float = 5 * 60,
    ) -> None:
        if preview_ttl_seconds <= 0:
            raise ValueError("preview TTL must be positive")
        if token_factory is None:
            import secrets

            token_factory = lambda: secrets.token_urlsafe(24)
        state_root = Path(backup_root)
        self._transaction = ManagedDocumentTransaction(
            document_path,
            backup_root=state_root,
            lock_root=state_root.parent / "locks",
            retention=5,
            platform_capabilities=platform_capabilities,
        )
        self._clock = clock
        self._token_factory = token_factory
        self._ttl = preview_ttl_seconds
        self._previews: dict[str, _PreviewRecord] = {}
        self._rollback_receipts: dict[str, _RollbackRecord] = {}
        self._lock = threading.Lock()

    def status(self) -> OverlayDocumentStatus:
        snapshot = self._transaction.preview(b"").current
        try:
            raw = _load_document(
                snapshot.content,
                exists=snapshot.revision.exists,
            )
            resolve_overlay_config(raw)
        except OverlayDocumentError:
            return OverlayDocumentStatus(True, "RECOVERY_ONLY")
        return OverlayDocumentStatus(False, None)

    def preview(
        self,
        command: OverlaySetValue,
        *,
        session_id: str,
    ) -> OverlayChangePreview:
        session = _session_id(session_id)
        if not isinstance(command, OverlaySetValue):
            raise OverlayDocumentError("DOCUMENT_INVALID", "unsupported command")
        bounds = overlay_field_bounds(command.key)
        if bounds is None:
            raise OverlayDocumentError("UNKNOWN_FIELD", "overlay field is not owned")
        if type(command.value) not in (int, float) or not math.isfinite(command.value):
            raise OverlayDocumentError("TYPE_MISMATCH", "overlay value must be numeric")
        minimum, maximum = bounds
        value = float(command.value)
        if not minimum <= value <= maximum:
            raise OverlayDocumentError("VALUE_OUT_OF_RANGE", "overlay value is out of range")

        captured = self._transaction.preview(b"").current
        raw = _load_document(captured.content, exists=captured.revision.exists)
        before = getattr(resolve_overlay_config(raw), command.key)
        candidate_document = dict(raw)
        candidate_document[command.key] = value
        candidate = (json.dumps(candidate_document, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )
        _load_document(candidate, exists=True)
        now = self._clock()
        with self._lock:
            self._drop_expired(now)
            if len(self._previews) >= 64:
                raise OverlayDocumentError("PREVIEW_UNAVAILABLE", "too many previews")
            token = self._allocate_token(self._previews)
            self._previews[token] = _PreviewRecord(
                session_id=session,
                candidate=candidate,
                revision=captured.revision,
                expires_at=now + self._ttl,
            )
        return OverlayChangePreview(
            preview_id=token,
            key=command.key,
            file_value_before=before,
            file_value_after=value,
            changed=candidate != captured.content,
        )

    def commit(
        self,
        preview_id: str,
        *,
        session_id: str,
    ) -> OverlayDocumentCommit:
        record = self._consume_preview(preview_id, session_id=_session_id(session_id))
        try:
            candidate = _load_document(record.candidate, exists=True)
            resolve_overlay_config(candidate)
            result = self._transaction.commit(
                record.candidate,
                expected_revision=record.revision,
            )
        except DocumentTransactionError as exc:
            raise OverlayDocumentError(exc.code, "overlay commit failed") from exc
        return OverlayDocumentCommit(
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
    ) -> OverlayRollbackConfirmation:
        session = _session_id(session_id)
        current = self._transaction.preview(b"").current
        try:
            restored = self._transaction.restore_snapshot(restore_point_id)
            preview = _rollback_preview(
                restore_point_id,
                current.content,
                current.revision.exists,
                restored.content,
                restored.revision.exists,
            )
        except DocumentTransactionError as exc:
            raise OverlayDocumentError(exc.code, "restore point is unavailable") from exc
        now = self._clock()
        with self._lock:
            self._drop_expired(now)
            token = self._allocate_token(self._rollback_receipts)
            self._rollback_receipts[token] = _RollbackRecord(
                session_id=session,
                revision=current.revision,
                preview=preview,
                expires_at=now + self._ttl,
            )
        return OverlayRollbackConfirmation(token, preview)

    def rollback(
        self,
        receipt_token: str,
        *,
        session_id: str,
    ) -> OverlayDocumentCommit:
        record = self._consume_rollback(
            receipt_token,
            session_id=_session_id(session_id),
        )
        current = self._transaction.preview(b"").current
        if current.revision != record.revision:
            raise OverlayDocumentError("DOCUMENT_CONFLICT", "overlay document changed")
        try:
            restored = self._transaction.restore_snapshot(
                record.preview.restore_point_id
            )
            current_preview = _rollback_preview(
                record.preview.restore_point_id,
                current.content,
                current.revision.exists,
                restored.content,
                restored.revision.exists,
            )
            if current_preview != record.preview:
                raise OverlayDocumentError(
                    "CONFIRMATION_REQUIRED", "rollback semantics changed"
                )
            result = self._transaction.rollback(
                record.preview.restore_point_id,
                expected_revision=record.revision,
            )
        except OverlayDocumentError:
            raise
        except DocumentTransactionError as exc:
            raise OverlayDocumentError(exc.code, "overlay rollback failed") from exc
        return OverlayDocumentCommit(
            restore_point_id=(
                result.restore_point.id if result.restore_point is not None else None
            ),
            maintenance_code=result.maintenance_code,
        )

    def _consume_preview(self, preview_id: str, *, session_id: str) -> _PreviewRecord:
        with self._lock:
            record = self._previews.get(preview_id)
            if (
                record is None
                or self._clock() >= record.expires_at
                or not hmac.compare_digest(record.session_id, session_id)
            ):
                if record is not None and self._clock() >= record.expires_at:
                    self._previews.pop(preview_id, None)
                raise OverlayDocumentError("CONFIRMATION_REQUIRED", "preview is invalid")
            self._previews.pop(preview_id, None)
            return record

    def _consume_rollback(
        self, receipt_token: str, *, session_id: str
    ) -> _RollbackRecord:
        with self._lock:
            record = self._rollback_receipts.get(receipt_token)
            if (
                record is None
                or self._clock() >= record.expires_at
                or not hmac.compare_digest(record.session_id, session_id)
            ):
                if record is not None and self._clock() >= record.expires_at:
                    self._rollback_receipts.pop(receipt_token, None)
                raise OverlayDocumentError("CONFIRMATION_REQUIRED", "receipt is invalid")
            self._rollback_receipts.pop(receipt_token, None)
            return record

    def _allocate_token(self, records: dict[str, Any]) -> str:
        for _ in range(32):
            token = self._token_factory()
            if isinstance(token, str) and 20 <= len(token) <= 128 and token not in records:
                return token
        raise OverlayDocumentError("PREVIEW_UNAVAILABLE", "could not allocate token")

    def _drop_expired(self, now: float) -> None:
        self._previews = {
            token: record
            for token, record in self._previews.items()
            if record.expires_at > now
        }
        self._rollback_receipts = {
            token: record
            for token, record in self._rollback_receipts.items()
            if record.expires_at > now
        }


def _load_document(content: bytes, *, exists: bool) -> dict[str, Any]:
    if not exists:
        return {}
    if len(content) > _MAX_DOCUMENT_BYTES:
        raise OverlayDocumentError("DOCUMENT_INVALID", "overlay document is too large")
    try:
        text = content.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise OverlayDocumentError("RECOVERY_ONLY", "overlay document is invalid") from exc
    if not isinstance(value, dict):
        raise OverlayDocumentError("RECOVERY_ONLY", "overlay root is not an object")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _rollback_preview(
    restore_point_id: str,
    current_content: bytes,
    current_exists: bool,
    restored_content: bytes,
    restored_exists: bool,
) -> OverlayRollbackPreview:
    restored = _load_document(restored_content, exists=restored_exists)
    restored_config = resolve_overlay_config(restored)
    try:
        current = _load_document(current_content, exists=current_exists)
        current_config = resolve_overlay_config(current)
    except OverlayDocumentError:
        return OverlayRollbackPreview(
            restore_point_id=restore_point_id,
            changed_fields=("<recovery-only-document>",),
            unmanaged_content_changed=True,
            unmanaged_change_count=1,
            resolution_error_before=True,
        )
    changed = tuple(
        key
        for key in OverlayConfig.__dataclass_fields__
        if getattr(current_config, key) != getattr(restored_config, key)
    )
    owned = set(OverlayConfig.__dataclass_fields__)
    unknown_keys = (set(current) | set(restored)) - owned
    unmanaged_count = sum(current.get(key) != restored.get(key) for key in unknown_keys)
    return OverlayRollbackPreview(
        restore_point_id=restore_point_id,
        changed_fields=changed,
        unmanaged_content_changed=unmanaged_count > 0,
        unmanaged_change_count=unmanaged_count,
        resolution_error_before=False,
    )


def _session_id(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise OverlayDocumentError("CONFIRMATION_REQUIRED", "session is required")
    return value


__all__ = [
    "OverlayChangePreview",
    "OverlayConfigDocument",
    "OverlayDocumentCommit",
    "OverlayDocumentError",
    "OverlayDocumentStatus",
    "OverlayRollbackConfirmation",
    "OverlayRollbackPreview",
    "OverlaySetValue",
]
