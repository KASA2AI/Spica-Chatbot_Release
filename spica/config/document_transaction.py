"""Byte-preserving transactions for Config Studio managed documents."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from spica.ports.config_studio_platform import PlatformCapabilities


@dataclass(frozen=True, slots=True)
class DocumentRevision:
    """Content identity with an explicit missing-document state."""

    exists: bool
    sha256: str = field(repr=False)

    @classmethod
    def from_bytes(cls, content: bytes, *, exists: bool = True) -> "DocumentRevision":
        return cls(exists=exists, sha256=hashlib.sha256(content).hexdigest())


@dataclass(frozen=True, slots=True)
class ManagedDocumentSnapshot:
    content: bytes = field(repr=False)
    revision: DocumentRevision
    _identity: object | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PrivateDocumentInspection:
    snapshot: ManagedDocumentSnapshot | None
    permission_health: str


@dataclass(frozen=True, slots=True)
class ChangePreview:
    current: ManagedDocumentSnapshot
    candidate_revision: DocumentRevision
    changed: bool


@dataclass(frozen=True, slots=True)
class RestorePoint:
    id: str


@dataclass(frozen=True, slots=True)
class RestorePointMetadata:
    id: str
    created_at_ns: int


@dataclass(frozen=True, slots=True)
class DocumentCommit:
    snapshot: ManagedDocumentSnapshot
    restore_point: RestorePoint | None
    maintenance_code: str | None = None


@dataclass(frozen=True, slots=True)
class _PublicationResult:
    maintenance_code: str | None
    identity: object | None = field(repr=False)


class DocumentTransactionError(RuntimeError):
    code = "DOCUMENT_TRANSACTION_ERROR"


class DocumentSafetyError(DocumentTransactionError):
    code = "DOCUMENT_UNSAFE"


class DocumentMultipleLinksError(DocumentSafetyError):
    pass


class DocumentWrongOwnerError(DocumentSafetyError):
    pass


class DocumentConflictError(DocumentTransactionError):
    code = "DOCUMENT_CONFLICT"


class DocumentBusyError(DocumentTransactionError):
    code = "DOCUMENT_BUSY"


class RestorePointError(DocumentTransactionError):
    code = "NO_VALID_RESTORE_POINT"


class DocumentWriteUnsupportedError(DocumentTransactionError):
    code = "WRITES_UNVERIFIED_ON_WINDOWS"


class RestoreMaintenanceError(DocumentTransactionError):
    code = "RESTORE_MAINTENANCE_FAILED"


class ManagedDocumentTransaction:
    """Preview and atomically publish one fixed managed document."""

    _mutexes_guard = threading.Lock()
    _mutexes: dict[str, threading.Lock] = {}

    def __init__(
        self,
        document_path: str | Path,
        *,
        backup_root: str | Path,
        retention: int = 5,
        lock_timeout: float = 2.0,
        publish_mode: int | None = None,
        private_posix: bool = False,
        lock_root: str | Path | None = None,
        platform_capabilities: PlatformCapabilities,
    ) -> None:
        if not isinstance(platform_capabilities, PlatformCapabilities):
            raise TypeError("platform_capabilities must be PlatformCapabilities")
        if publish_mode is not None and not 0 <= publish_mode <= 0o777:
            raise ValueError("publish_mode must be a POSIX permission mode")
        if isinstance(retention, bool) or not isinstance(retention, int) or retention < 1:
            raise ValueError("retention must be a positive integer")
        if (
            private_posix
            and platform_capabilities.posix_permissions
            and publish_mode != 0o600
        ):
            raise ValueError("private POSIX documents must publish with mode 0600")
        self._platform = platform_capabilities
        self.document_path = Path(document_path)
        self.backup_root = Path(backup_root)
        self.retention = retention
        self.lock_timeout = lock_timeout
        self.publish_mode = publish_mode
        self.private_posix = private_posix
        self.lock_root = (
            Path(lock_root)
            if lock_root is not None
            else platform_capabilities.default_lock_root
        )

    def preview(self, candidate: bytes) -> ChangePreview:
        current = self._snapshot()
        candidate_revision = DocumentRevision.from_bytes(candidate)
        return ChangePreview(
            current=current,
            candidate_revision=candidate_revision,
            changed=current.revision != candidate_revision,
        )

    def inspect_private(self) -> PrivateDocumentInspection:
        """Read bytes and POSIX permission facts from one no-follow file identity.

        This is a read-only capability probe and must not create lock or backup
        state. Mutating transactions still take the stable cross-process lock.
        """

        if not self.private_posix or not self._platform.posix_permissions:
            raise DocumentSafetyError("private POSIX inspection is unavailable")
        try:
            snapshot, file_stat = self._snapshot_with_stat()
        except DocumentMultipleLinksError:
            return PrivateDocumentInspection(None, "MULTIPLE_LINKS")
        except DocumentWrongOwnerError:
            return PrivateDocumentInspection(None, "WRONG_OWNER")
        except DocumentSafetyError:
            return PrivateDocumentInspection(None, "DOCUMENT_UNSAFE")
        if file_stat is None:
            return PrivateDocumentInspection(snapshot, "MISSING")
        permission_health = (
            "PRIVATE"
            if stat.S_IMODE(file_stat.st_mode) == 0o600
            else "TOO_PERMISSIVE"
        )
        return PrivateDocumentInspection(snapshot, permission_health)

    def commit(
        self,
        candidate: bytes,
        *,
        expected_revision: DocumentRevision,
        defer_retention: bool = False,
        before_publication: Callable[[], None] | None = None,
    ) -> DocumentCommit:
        self._ensure_writes_supported()
        with self._write_lock():
            current = self._snapshot()
            if current.revision != expected_revision:
                raise DocumentConflictError("managed document revision changed")
            candidate_unchanged = DocumentRevision.from_bytes(candidate) == current.revision
            mode_already_matches = (
                self.publish_mode is None
                or self._current_mode() == self.publish_mode
            )
            if candidate_unchanged and mode_already_matches:
                return DocumentCommit(current, None)
            restore_point = self._create_restore_point(current)
            try:
                publication = self._atomic_publish(
                    candidate,
                    mode=self._target_mode(),
                    expected_snapshot=current,
                    restore_point=restore_point,
                    before_publication=before_publication,
                )
            except BaseException as publish_error:
                self._discard_restore_point_if_unpublished(
                    restore_point,
                    previous_snapshot=current,
                    publish_error=publish_error,
                )
                raise
            published = self._snapshot_after_publication(
                expected_identity=publication.identity,
            )
            if published.revision != DocumentRevision.from_bytes(candidate):
                raise DocumentConflictError(
                    "managed document changed after publication"
                )
            publication_maintenance = self._publication_permission_maintenance(
                published
            )
            retention_maintenance = (
                None
                if defer_retention
                else self._prune_after_publish(
                    protected_restore_point_id=restore_point.id,
                )
            )
            published = self._snapshot_after_publication(
                expected_identity=publication.identity,
            )
            if published.revision != DocumentRevision.from_bytes(candidate):
                raise DocumentConflictError(
                    "managed document changed after publication"
                )
            result = DocumentCommit(
                published,
                restore_point,
                publication_maintenance
                or publication.maintenance_code
                or retention_maintenance,
            )
            return result

    def rollback(
        self,
        restore_point_id: str,
        *,
        expected_revision: DocumentRevision,
        defer_retention: bool = False,
        before_publication: Callable[[], None] | None = None,
    ) -> DocumentCommit:
        self._ensure_writes_supported()
        with self._write_lock():
            current = self._snapshot()
            if current.revision != expected_revision:
                raise DocumentConflictError("managed document revision changed")
            restored = self._load_restore_point(restore_point_id)
            undo_restore_point = self._create_restore_point(current)
            try:
                if restored.revision.exists:
                    publication = self._atomic_publish(
                        restored.content,
                        mode=self._target_mode(),
                        expected_snapshot=current,
                        restore_point=undo_restore_point,
                        before_publication=before_publication,
                    )
                else:
                    publication = self._atomic_remove(
                        expected_snapshot=current,
                        restore_point=undo_restore_point,
                        before_publication=before_publication,
                    )
            except BaseException as publish_error:
                self._discard_restore_point_if_unpublished(
                    undo_restore_point,
                    previous_snapshot=current,
                    publish_error=publish_error,
                )
                raise
            published = self._snapshot_after_publication(
                expected_identity=publication.identity,
            )
            if published.revision != restored.revision:
                raise DocumentConflictError(
                    "managed document changed after publication"
                )
            publication_maintenance = self._publication_permission_maintenance(
                published
            )
            retention_maintenance = (
                None
                if defer_retention
                else self._prune_after_publish(
                    protected_restore_point_id=undo_restore_point.id,
                )
            )
            published = self._snapshot_after_publication(
                expected_identity=publication.identity,
            )
            if published.revision != restored.revision:
                raise DocumentConflictError(
                    "managed document changed after publication"
                )
            result = DocumentCommit(
                published,
                undo_restore_point,
                publication_maintenance
                or publication.maintenance_code
                or retention_maintenance,
            )
            return result

    def finalize_deferred_retention(
        self,
        *,
        expected_snapshot: ManagedDocumentSnapshot,
        protected_restore_point_id: str | None,
    ) -> str | None:
        """Apply retention after a caller has accepted a guarded publication."""

        self._ensure_writes_supported()
        with self._write_lock():
            current = self._snapshot()
            if not self._same_live_snapshot(current, expected_snapshot):
                raise DocumentConflictError(
                    "managed document changed before retention"
                )
            maintenance_code = self._prune_after_publish(
                protected_restore_point_id=protected_restore_point_id,
            )
            current = self._snapshot()
            if not self._same_live_snapshot(current, expected_snapshot):
                raise DocumentConflictError(
                    "managed document changed during retention"
                )
            return maintenance_code

    def recover_failed_publication(
        self,
        restore_point_id: str | None,
        *,
        expected_snapshot: ManagedDocumentSnapshot,
        previous_revision: DocumentRevision,
    ) -> ManagedDocumentSnapshot:
        """Restore a failed guarded publication without creating user history."""

        self._ensure_writes_supported()
        with self._write_lock():
            current = self._snapshot()
            if not self._same_live_snapshot(current, expected_snapshot):
                raise DocumentConflictError("managed document revision changed")
            if restore_point_id is None:
                if current.revision != previous_revision:
                    raise RestorePointError(
                        "recovery requires the prior document bytes"
                    )
                recovered = current
            else:
                restored = self._load_restore_point(restore_point_id)
                if restored.revision != previous_revision:
                    raise RestorePointError(
                        "restore point does not match prior document"
                    )
                if restored.revision.exists:
                    publication = self._atomic_publish(
                        restored.content,
                        mode=self._target_mode(),
                        expected_snapshot=current,
                        restore_point=None,
                    )
                else:
                    publication = self._atomic_remove(
                        expected_snapshot=current,
                        restore_point=None,
                    )
                recovered = self._snapshot_after_publication(
                    expected_identity=publication.identity,
                )
            if recovered.revision != previous_revision:
                raise DocumentConflictError("managed document recovery changed")
            if previous_revision.exists and self.publish_mode is not None:
                self._harden_current_mode(
                    expected_snapshot=recovered,
                    mode=self.publish_mode,
                )
                hardened = self._snapshot()
                if not self._same_live_snapshot(hardened, recovered):
                    raise DocumentConflictError(
                        "managed document recovery changed"
                    )
                recovered = hardened
            if restore_point_id is not None:
                self._delete_restore_point(restore_point_id)
            return recovered

    def restore_snapshot(self, restore_point_id: str) -> ManagedDocumentSnapshot:
        """Read a restore point for backend semantic preview only.

        The returned DTO has a redacted representation and must never be exposed
        directly by an HTTP API.  Callers map it to bounded semantic metadata.
        """
        self._ensure_writes_supported()
        with self._write_lock():
            return self._load_restore_point(restore_point_id)

    def restore_points(self) -> tuple[RestorePointMetadata, ...]:
        """List only fully valid opaque restore metadata without creating state."""

        entries = self._restore_point_entries()
        entries.sort(reverse=True)
        valid: list[RestorePointMetadata] = []
        for created_at_ns, restore_id, _ in entries:
            try:
                self._load_restore_point(restore_id)
            except (DocumentTransactionError, OSError):
                continue
            valid.append(
                RestorePointMetadata(
                    id=restore_id,
                    created_at_ns=created_at_ns,
                )
            )
        return tuple(valid)

    def publication_matches(
        self,
        current: ManagedDocumentSnapshot,
        published: ManagedDocumentSnapshot,
    ) -> bool:
        """Compare private live-file identity without exposing its token."""

        return self._same_live_snapshot(current, published)

    def _snapshot(self) -> ManagedDocumentSnapshot:
        snapshot, _ = self._snapshot_with_stat()
        return snapshot

    def _capture_file_identity(self, descriptor: int) -> object | None:
        if not self._platform.managed_document_writes:
            return None
        return self._platform.file_identity.capture_descriptor(descriptor)

    def _same_identity(self, left: object | None, right: object | None) -> bool:
        if left is None or right is None:
            return left is right
        return self._platform.file_identity.same(left, right)

    def _same_live_snapshot(
        self,
        left: ManagedDocumentSnapshot,
        right: ManagedDocumentSnapshot,
    ) -> bool:
        return left.revision == right.revision and self._same_identity(
            left._identity,
            right._identity,
        )

    def _snapshot_after_publication(
        self,
        *,
        expected_identity: object | None,
    ) -> ManagedDocumentSnapshot:
        try:
            snapshot = self._snapshot()
        except (DocumentSafetyError, OSError) as exc:
            raise DocumentConflictError(
                "managed document changed after publication"
            ) from exc
        if not self._same_identity(snapshot._identity, expected_identity):
            raise DocumentConflictError(
                "managed document changed after publication"
            )
        return snapshot

    def _snapshot_with_stat(
        self,
    ) -> tuple[ManagedDocumentSnapshot, os.stat_result | None]:
        self._validate_directory_chain(self.document_path.parent)
        try:
            document_stat = self.document_path.lstat()
        except FileNotFoundError:
            return (
                ManagedDocumentSnapshot(
                    b"", DocumentRevision.from_bytes(b"", exists=False)
                ),
                None,
            )
        if not stat.S_ISREG(document_stat.st_mode):
            raise DocumentSafetyError("managed document is not a regular file")
        self._validate_managed_file_stat(document_stat)
        content, opened_stat, opened_identity = self._read_verified_regular_with_stat(
            self.document_path,
            document_stat,
        )
        return (
            ManagedDocumentSnapshot(
                content,
                DocumentRevision.from_bytes(content),
                opened_identity,
            ),
            opened_stat,
        )

    @staticmethod
    def _validate_directory_chain(path: Path) -> None:
        absolute = Path(os.path.abspath(path))
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current /= part
            try:
                current_stat = current.lstat()
            except FileNotFoundError as exc:
                raise DocumentSafetyError("managed document parent is missing") from exc
            if not stat.S_ISDIR(current_stat.st_mode):
                raise DocumentSafetyError("managed document parent is unsafe")

    def _read_verified_regular(
        self, path: Path, expected_stat: os.stat_result
    ) -> bytes:
        content, _, _ = self._read_verified_regular_with_stat(path, expected_stat)
        return content

    def _read_verified_regular_with_stat(
        self, path: Path, expected_stat: os.stat_result
    ) -> tuple[bytes, os.stat_result, object | None]:
        open_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        open_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, open_flags)
        except OSError as exc:
            raise DocumentSafetyError("managed document changed during read") from exc
        try:
            opened_stat = os.fstat(descriptor)
            if not stat.S_ISREG(opened_stat.st_mode) or (
                opened_stat.st_dev,
                opened_stat.st_ino,
            ) != (expected_stat.st_dev, expected_stat.st_ino):
                raise DocumentSafetyError("managed document changed during read")
            self._validate_managed_file_stat(opened_stat)
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 64 * 1024):
                chunks.append(chunk)
            content = b"".join(chunks)
            final_stat = os.fstat(descriptor)
            if not self._same_content_payload_facts(opened_stat, final_stat):
                raise DocumentSafetyError("managed document changed during read")
            if opened_stat.st_ctime_ns != final_stat.st_ctime_ns:
                os.lseek(descriptor, 0, os.SEEK_SET)
                verified_chunks: list[bytes] = []
                while chunk := os.read(descriptor, 64 * 1024):
                    verified_chunks.append(chunk)
                verified_content = b"".join(verified_chunks)
                verified_stat = os.fstat(descriptor)
                if (
                    verified_content != content
                    or not self._same_content_generation(final_stat, verified_stat)
                ):
                    raise DocumentSafetyError(
                        "managed document changed during read"
                    )
                final_stat = verified_stat
            self._validate_managed_file_stat(final_stat)
            identity = self._capture_file_identity(descriptor)
            if identity is not None and not self._platform.file_identity.path_matches_no_follow(
                path,
                identity,
            ):
                raise DocumentSafetyError("managed document changed during read")
            if identity is None:
                try:
                    final_path_stat = path.lstat()
                except OSError as exc:
                    raise DocumentSafetyError(
                        "managed document changed during read"
                    ) from exc
                if (
                    final_path_stat.st_dev,
                    final_path_stat.st_ino,
                ) != (final_stat.st_dev, final_stat.st_ino):
                    raise DocumentSafetyError("managed document changed during read")
            return content, final_stat, identity
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def _same_content_generation(
        before: os.stat_result,
        after: os.stat_result,
    ) -> bool:
        return (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )

    @staticmethod
    def _same_content_payload_facts(
        before: os.stat_result,
        after: os.stat_result,
    ) -> bool:
        return (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )

    def _validate_managed_file_stat(self, file_stat: os.stat_result) -> None:
        if file_stat.st_nlink != 1:
            raise DocumentMultipleLinksError("managed file has multiple links")
        if (
            self._platform.posix_permissions
            and file_stat.st_uid != self._platform.user_id
        ):
            raise DocumentWrongOwnerError("managed file owner is unsafe")

    def _validate_private_storage_file_stat(
        self, file_stat: os.stat_result
    ) -> None:
        if not self._platform.posix_permissions:
            return
        if (
            file_stat.st_uid != self._platform.user_id
            or file_stat.st_nlink != 1
            or stat.S_IMODE(file_stat.st_mode) != 0o600
        ):
            raise DocumentSafetyError("private transaction file is unsafe")

    def _validate_private_directory(self, path: Path) -> None:
        if not self._platform.posix_permissions:
            return
        try:
            directory_stat = path.lstat()
        except FileNotFoundError as exc:
            raise DocumentSafetyError("private transaction directory is missing") from exc
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_uid != self._platform.user_id
            or stat.S_IMODE(directory_stat.st_mode) != 0o700
        ):
            raise DocumentSafetyError("private transaction directory is unsafe")

    def _current_mode(self) -> int:
        try:
            return stat.S_IMODE(self.document_path.lstat().st_mode)
        except FileNotFoundError:
            return 0o600

    def _harden_current_mode(
        self,
        *,
        expected_snapshot: ManagedDocumentSnapshot,
        mode: int,
    ) -> None:
        self._validate_directory_chain(self.document_path.parent)
        try:
            expected_stat = self.document_path.lstat()
        except FileNotFoundError as exc:
            raise DocumentConflictError("managed document changed") from exc
        if not stat.S_ISREG(expected_stat.st_mode):
            raise DocumentSafetyError("managed document is not a regular file")
        self._validate_managed_file_stat(expected_stat)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.document_path, flags)
        except OSError as exc:
            raise DocumentSafetyError("managed document changed") from exc
        try:
            opened_stat = os.fstat(descriptor)
            if not stat.S_ISREG(opened_stat.st_mode) or (
                opened_stat.st_dev,
                opened_stat.st_ino,
            ) != (expected_stat.st_dev, expected_stat.st_ino):
                raise DocumentSafetyError("managed document changed")
            self._validate_managed_file_stat(opened_stat)
            opened_identity = self._capture_file_identity(descriptor)
            if not self._same_identity(
                opened_identity,
                expected_snapshot._identity,
            ):
                raise DocumentConflictError("managed document changed")
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 64 * 1024):
                chunks.append(chunk)
            content_stat = os.fstat(descriptor)
            if not self._same_content_generation(opened_stat, content_stat):
                raise DocumentConflictError("managed document changed")
            if (
                DocumentRevision.from_bytes(b"".join(chunks))
                != expected_snapshot.revision
            ):
                raise DocumentConflictError("managed document changed")
            os.fchmod(descriptor, mode)
            hardened_stat = os.fstat(descriptor)
            self._validate_managed_file_stat(hardened_stat)
            if stat.S_IMODE(hardened_stat.st_mode) != mode:
                raise DocumentSafetyError("managed document mode is unsafe")
            if opened_identity is None or not self._platform.file_identity.path_matches_no_follow(
                self.document_path,
                opened_identity,
            ):
                raise DocumentConflictError("managed document changed")
        finally:
            os.close(descriptor)

    def _target_mode(self) -> int:
        if self.publish_mode is not None:
            return self.publish_mode
        return self._current_mode()

    def _publication_permission_maintenance(
        self,
        published: ManagedDocumentSnapshot,
    ) -> str | None:
        if (
            published.revision.exists
            and self.publish_mode is not None
            and self._current_mode() != self.publish_mode
        ):
            return "DOCUMENT_PUBLICATION_PERMISSIONS_UNSAFE"
        return None

    def _ensure_writes_supported(self) -> None:
        if not self._platform.managed_document_writes:
            raise DocumentWriteUnsupportedError(
                "managed document writes are not verified on Windows"
            )

    def _document_backup_root(self) -> Path:
        identity = hashlib.sha256(
            os.fsencode(self.document_path.absolute())
        ).hexdigest()
        return self.backup_root / identity

    @contextmanager
    def _write_lock(self):
        deadline = time.monotonic() + max(0.0, self.lock_timeout)
        key = os.path.normcase(os.path.abspath(self.document_path))
        with self._mutexes_guard:
            mutex = self._mutexes.setdefault(key, threading.Lock())
        acquired = mutex.acquire(timeout=max(0.0, deadline - time.monotonic()))
        if not acquired:
            raise DocumentBusyError("managed document is busy")
        try:
            self._ensure_directory_tree(self.lock_root)
            self._validate_lock_directory(self.lock_root)
            lock_flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
            lock_flags |= getattr(os, "O_NOFOLLOW", 0)
            lock_path = self.lock_root / (
                hashlib.sha256(os.fsencode(key)).hexdigest() + ".lock"
            )
            try:
                lock_descriptor = os.open(lock_path, lock_flags, 0o600)
            except OSError as exc:
                raise DocumentSafetyError("transaction lock is unsafe") from exc
            try:
                opened_stat = os.fstat(lock_descriptor)
                lock_stat = lock_path.lstat()
                if not stat.S_ISREG(opened_stat.st_mode) or (
                    opened_stat.st_dev,
                    opened_stat.st_ino,
                ) != (lock_stat.st_dev, lock_stat.st_ino):
                    raise DocumentSafetyError("transaction lock is unsafe")
                self._validate_lock_file_stat(opened_stat)
                while True:
                    if self._platform.file_lock.try_acquire(lock_descriptor):
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DocumentBusyError("managed document is busy")
                    time.sleep(min(0.01, remaining))
                try:
                    yield
                finally:
                    self._platform.file_lock.release(lock_descriptor)
            finally:
                os.close(lock_descriptor)
        finally:
            mutex.release()

    def _validate_lock_directory(self, path: Path) -> None:
        try:
            directory_stat = path.lstat()
        except FileNotFoundError as exc:
            raise DocumentSafetyError("transaction lock directory is missing") from exc
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise DocumentSafetyError("transaction lock directory is unsafe")
        if self._platform.posix_permissions and (
            directory_stat.st_uid != self._platform.user_id
            or stat.S_IMODE(directory_stat.st_mode) != 0o700
        ):
            raise DocumentSafetyError("transaction lock directory is unsafe")

    def _validate_lock_file_stat(self, file_stat: os.stat_result) -> None:
        if self._platform.posix_permissions and (
            file_stat.st_uid != self._platform.user_id
            or file_stat.st_nlink != 1
            or stat.S_IMODE(file_stat.st_mode) != 0o600
        ):
            raise DocumentSafetyError("transaction lock is unsafe")

    def _create_restore_point(
        self, snapshot: ManagedDocumentSnapshot
    ) -> RestorePoint:
        document_backups = self._document_backup_root()
        self._ensure_directory_tree(document_backups)
        self._validate_private_directory(self.backup_root)
        self._validate_private_directory(document_backups)
        for _ in range(32):
            restore_id = secrets.token_urlsafe(18)
            restore_dir = document_backups / restore_id
            try:
                restore_dir.mkdir(mode=0o700)
            except FileExistsError:
                continue
            try:
                if self._platform.posix_permissions:
                    restore_dir.chmod(0o700)
                self._validate_private_directory(restore_dir)
                if snapshot.revision.exists:
                    self._write_exclusive_private(
                        restore_dir / "content", snapshot.content
                    )
                metadata = (
                    f"{int(snapshot.revision.exists)}\n{time.time_ns()}\n"
                    f"{snapshot.revision.sha256 if snapshot.revision.exists else '-'}\n"
                ).encode("ascii")
                self._write_exclusive_private(restore_dir / "metadata", metadata)
                self._fsync_directory(restore_dir)
                self._fsync_directory(document_backups)
            except BaseException:
                for filename in ("metadata", "content"):
                    try:
                        (restore_dir / filename).unlink()
                    except FileNotFoundError:
                        pass
                try:
                    restore_dir.rmdir()
                except OSError:
                    pass
                raise
            return RestorePoint(restore_id)
        raise DocumentTransactionError("could not allocate restore point")

    def _discard_restore_point_if_unpublished(
        self,
        restore_point: RestorePoint,
        *,
        previous_snapshot: ManagedDocumentSnapshot,
        publish_error: BaseException,
    ) -> None:
        """Remove the attempt backup only when live identity proves no publication."""

        try:
            live_snapshot = self._snapshot()
        except DocumentTransactionError:
            return
        if not self._same_live_snapshot(live_snapshot, previous_snapshot):
            return
        try:
            self._delete_restore_point(restore_point.id)
        except (DocumentTransactionError, OSError) as cleanup_error:
            raise RestoreMaintenanceError(
                "failed publication left a restore point requiring maintenance"
            ) from publish_error

    def _recheck_before_publication(
        self,
        *,
        expected_snapshot: ManagedDocumentSnapshot,
        restore_point: RestorePoint | None,
    ) -> None:
        """Close the RestorePoint creation window before touching live bytes."""

        try:
            live_snapshot = self._snapshot()
        except (DocumentSafetyError, OSError) as exc:
            if restore_point is not None:
                self._discard_prepublication_restore_point(restore_point)
            raise DocumentConflictError(
                "managed document changed before publication"
            ) from exc
        if self._same_live_snapshot(live_snapshot, expected_snapshot):
            return
        if restore_point is not None:
            self._discard_prepublication_restore_point(restore_point)
        raise DocumentConflictError("managed document revision changed")

    def _discard_prepublication_restore_point(
        self,
        restore_point: RestorePoint,
    ) -> None:
        try:
            self._delete_restore_point(restore_point.id)
        except (DocumentTransactionError, OSError):
            # Publication has not started, so live bytes remain authoritative.
            # A private orphan is maintenance debt, never permission to publish.
            pass

    def _delete_restore_point(self, restore_point_id: str) -> None:
        restore_dir = self._restore_point_directory(restore_point_id)
        for filename in ("content", "metadata"):
            child = restore_dir / filename
            try:
                child_stat = child.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(child_stat.st_mode):
                raise DocumentSafetyError("restore point is unsafe")
            self._validate_private_storage_file_stat(child_stat)
            child.unlink()
        restore_dir.rmdir()
        self._fsync_directory(self._document_backup_root())

    @staticmethod
    def _write_exclusive_private(path: Path, content: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise DocumentSafetyError("restore point is unsafe")
            with os.fdopen(descriptor, "wb") as restore_file:
                descriptor = -1
                restore_file.write(content)
                restore_file.flush()
                os.fsync(restore_file.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        path_stat = path.lstat()
        if not stat.S_ISDIR(path_stat.st_mode):
            raise DocumentSafetyError("transaction directory is unsafe")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened_stat = os.fstat(descriptor)
            if not stat.S_ISDIR(opened_stat.st_mode) or (
                opened_stat.st_dev,
                opened_stat.st_ino,
            ) != (path_stat.st_dev, path_stat.st_ino):
                raise DocumentSafetyError("transaction directory is unsafe")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _ensure_directory_tree(path: Path) -> None:
        absolute = Path(os.path.abspath(path))
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current /= part
            try:
                current_stat = current.lstat()
            except FileNotFoundError:
                try:
                    current.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                current_stat = current.lstat()
            if not stat.S_ISDIR(current_stat.st_mode):
                raise DocumentSafetyError("transaction directory is unsafe")

    def _load_restore_point(self, restore_point_id: str) -> ManagedDocumentSnapshot:
        restore_dir = self._restore_point_directory(restore_point_id)
        try:
            self._validate_private_directory(self.backup_root)
            self._validate_private_directory(self._document_backup_root())
        except DocumentSafetyError as exc:
            raise RestorePointError("restore point is invalid") from exc
        try:
            restore_stat = restore_dir.lstat()
            metadata_stat = (restore_dir / "metadata").lstat()
        except FileNotFoundError as exc:
            raise RestorePointError("restore point is invalid") from exc
        if not stat.S_ISDIR(restore_stat.st_mode) or not stat.S_ISREG(
            metadata_stat.st_mode
        ):
            raise RestorePointError("restore point is invalid")
        try:
            self._validate_private_directory(restore_dir)
            self._validate_private_storage_file_stat(metadata_stat)
        except DocumentSafetyError as exc:
            raise RestorePointError("restore point is invalid") from exc
        try:
            metadata_bytes = self._read_verified_regular(
                restore_dir / "metadata", metadata_stat
            )
            metadata = metadata_bytes.decode("ascii").splitlines()
            exists = metadata[0] == "1"
            if metadata[0] not in {"0", "1"} or len(metadata) != 3:
                raise ValueError
            int(metadata[1])
            expected_sha256 = metadata[2]
            if exists:
                if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
                    raise ValueError
            elif expected_sha256 != "-":
                raise ValueError
        except (
            DocumentSafetyError,
            OSError,
            UnicodeError,
            ValueError,
            IndexError,
        ) as exc:
            raise RestorePointError("restore point is invalid") from exc
        if not exists:
            return ManagedDocumentSnapshot(
                b"", DocumentRevision.from_bytes(b"", exists=False)
            )
        content_path = restore_dir / "content"
        try:
            content_stat = content_path.lstat()
            if not stat.S_ISREG(content_stat.st_mode):
                raise RestorePointError("restore point is invalid")
            self._validate_private_storage_file_stat(content_stat)
            content = self._read_verified_regular(content_path, content_stat)
        except (DocumentSafetyError, OSError) as exc:
            raise RestorePointError("restore point is invalid") from exc
        revision = DocumentRevision.from_bytes(content)
        if revision.sha256 != expected_sha256:
            raise RestorePointError("restore point is invalid")
        return ManagedDocumentSnapshot(content, revision)

    def _restore_point_directory(self, restore_point_id: str) -> Path:
        if not isinstance(restore_point_id, str) or re.fullmatch(
            r"[A-Za-z0-9_-]{20,64}", restore_point_id
        ) is None:
            raise RestorePointError("restore point is invalid")
        return self._document_backup_root() / restore_point_id

    def _prune_restore_points(
        self,
        *,
        protected_restore_point_id: str | None = None,
    ) -> None:
        restore_points = self._restore_point_entries()
        newest_first = sorted(restore_points, reverse=True)
        available_ids = {restore_id for _, restore_id, _ in newest_first}
        keep_ids: set[str] = set()
        if protected_restore_point_id in available_ids:
            keep_ids.add(protected_restore_point_id)
        for _, restore_id, _ in newest_first:
            if len(keep_ids) >= self.retention:
                break
            keep_ids.add(restore_id)
        for _, restore_id, restore_dir in restore_points:
            if restore_id in keep_ids:
                continue
            for filename in ("content", "metadata"):
                try:
                    child = restore_dir / filename
                    child_stat = child.lstat()
                    if not stat.S_ISREG(child_stat.st_mode):
                        raise DocumentSafetyError("restore point is unsafe")
                    self._validate_private_storage_file_stat(child_stat)
                    child.unlink()
                except FileNotFoundError:
                    pass
            restore_dir.rmdir()

    def _prune_after_publish(
        self,
        *,
        protected_restore_point_id: str | None = None,
    ) -> str | None:
        """Run retention without misreporting an already-published commit as failed."""

        try:
            self._prune_restore_points(
                protected_restore_point_id=protected_restore_point_id,
            )
        except (DocumentTransactionError, OSError):
            return "RESTORE_RETENTION_DEGRADED"
        return None

    def _restore_point_entries(self) -> list[tuple[int, str, Path]]:
        document_backups = self._document_backup_root()
        try:
            document_backups.lstat()
        except FileNotFoundError:
            return []
        try:
            self._validate_directory_chain(document_backups)
            if not stat.S_ISDIR(document_backups.lstat().st_mode):
                raise DocumentSafetyError("restore point directory is unsafe")
            self._validate_private_directory(self.backup_root)
            self._validate_private_directory(document_backups)
            candidates = tuple(document_backups.iterdir())
        except FileNotFoundError:
            return []
        restore_points: list[tuple[int, str, Path]] = []
        for candidate in candidates:
            if re.fullmatch(r"[A-Za-z0-9_-]{20,64}", candidate.name) is None:
                continue
            try:
                if not stat.S_ISDIR(candidate.lstat().st_mode):
                    continue
                self._validate_private_directory(candidate)
                metadata_path = candidate / "metadata"
                metadata_stat = metadata_path.lstat()
                if not stat.S_ISREG(metadata_stat.st_mode):
                    continue
                self._validate_private_storage_file_stat(metadata_stat)
                metadata = self._read_verified_regular(
                    metadata_path,
                    metadata_stat,
                ).decode("ascii").splitlines()
                if len(metadata) != 3 or metadata[0] not in {"0", "1"}:
                    continue
                created_ns = int(metadata[1])
                expected_hash = metadata[2]
                if metadata[0] == "1":
                    if re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None:
                        continue
                elif expected_hash != "-":
                    continue
            except (DocumentSafetyError, OSError, UnicodeError, ValueError):
                continue
            restore_points.append((created_ns, candidate.name, candidate))
        return restore_points

    def _atomic_publish(
        self,
        content: bytes,
        *,
        mode: int,
        expected_snapshot: ManagedDocumentSnapshot,
        restore_point: RestorePoint | None,
        before_publication: Callable[[], None] | None = None,
    ) -> _PublicationResult:
        parent = self.document_path.parent
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.document_path.name}.config-studio-",
            dir=parent,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb", closefd=False) as temporary_file:
                temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            prepared_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(prepared_stat.st_mode)
                or stat.S_IMODE(prepared_stat.st_mode) != mode
            ):
                raise DocumentSafetyError("temporary document permissions are unsafe")
            self._validate_managed_file_stat(prepared_stat)
            prepared_identity = self._capture_file_identity(descriptor)
            if prepared_identity is None:
                raise DocumentWriteUnsupportedError(
                    "stable publication identity is unavailable"
                )
            if before_publication is not None:
                before_publication()
            if not self._platform.file_identity.path_matches_no_follow(
                temporary_path,
                prepared_identity,
            ):
                raise DocumentSafetyError("temporary document identity changed")
            self._recheck_before_publication(
                expected_snapshot=expected_snapshot,
                restore_point=restore_point,
            )
            os.replace(temporary_path, self.document_path)
            if not self._platform.file_identity.path_matches_no_follow(
                self.document_path,
                prepared_identity,
            ):
                raise DocumentConflictError(
                    "managed document changed after publication"
                )
            maintenance_code = None
            try:
                self._fsync_directory(parent)
            except (DocumentTransactionError, OSError):
                maintenance_code = "DOCUMENT_DURABILITY_UNCONFIRMED"
            return _PublicationResult(maintenance_code, prepared_identity)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    def _atomic_remove(
        self,
        *,
        expected_snapshot: ManagedDocumentSnapshot,
        restore_point: RestorePoint | None,
        before_publication: Callable[[], None] | None = None,
    ) -> _PublicationResult:
        try:
            document_stat = self.document_path.lstat()
        except FileNotFoundError:
            if before_publication is not None:
                before_publication()
            self._recheck_before_publication(
                expected_snapshot=expected_snapshot,
                restore_point=restore_point,
            )
            return _PublicationResult(None, None)
        if not stat.S_ISREG(document_stat.st_mode):
            raise DocumentSafetyError("managed document is not a regular file")
        if before_publication is not None:
            before_publication()
        self._recheck_before_publication(
            expected_snapshot=expected_snapshot,
            restore_point=restore_point,
        )
        self.document_path.unlink()
        maintenance_code = None
        try:
            self._fsync_directory(self.document_path.parent)
        except (DocumentTransactionError, OSError):
            maintenance_code = "DOCUMENT_DURABILITY_UNCONFIRMED"
        return _PublicationResult(maintenance_code, None)
