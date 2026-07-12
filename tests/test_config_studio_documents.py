from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import spica.config.document_transaction as transaction_module
from spica.config.env_roster import LEGACY_ENV_VARS, consumed_env_names
from spica.config.document_transaction import (
    DocumentBusyError,
    DocumentConflictError,
    DocumentSafetyError,
    DocumentWriteUnsupportedError,
    ManagedDocumentTransaction,
    RestorePointError,
)
from spica.adapters.config_studio.platform import platform_capabilities_for
from support.config_studio_transactions import after_first_transaction_fsync


def _transaction(document, *, backup_root, **kwargs):
    kwargs.setdefault(
        "platform_capabilities",
        platform_capabilities_for(
            os_family="posix",
            runtime_name="linux",
            user_id=os.getuid(),
            temp_directory=Path(backup_root).parent / "platform-tmp",
        ),
    )
    return ManagedDocumentTransaction(
        document,
        backup_root=backup_root,
        **kwargs,
    )


def test_preview_reports_current_and_candidate_revisions_without_writing(tmp_path):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"enabled: false\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )

    preview = transaction.preview(b"enabled: true\n")

    assert preview.changed is True
    assert preview.current.content == b"enabled: false\n"
    assert preview.current.revision.exists is True
    assert preview.current.revision.sha256 == (
        "f7e88436af3f7e00d86e65802642bff4ff12996ca46a7ea3da7ae2549cf36c6e"
    )
    assert preview.candidate_revision.sha256 == (
        "58b2e0c9e66599dd94f77b2ab49ca64f0b6841d32f4b0874c4c4b8fd5b9fb862"
    )
    assert document.read_bytes() == b"enabled: false\n"


def test_transaction_dto_repr_omits_content_hash_and_path(tmp_path):
    document = tmp_path / "sensitive-name.yaml"
    document.write_bytes(b"canary-value: do-not-log\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )

    preview = transaction.preview(b"canary-value: replacement\n")
    rendered = repr(preview)

    assert "do-not-log" not in rendered
    assert "replacement" not in rendered
    assert preview.current.revision.sha256 not in rendered
    assert str(document) not in rendered


def test_preview_preserves_the_distinction_between_missing_and_empty(tmp_path):
    document = tmp_path / "app.yaml"
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )

    preview = transaction.preview(b"")

    assert preview.changed is True
    assert preview.current.content == b""
    assert preview.current.revision.exists is False
    assert preview.candidate_revision.exists is True


def test_preview_rejects_a_symlink_document(tmp_path):
    target = tmp_path / "actual.yaml"
    target.write_bytes(b"enabled: false\n")
    document = tmp_path / "app.yaml"
    document.symlink_to(target)
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )

    with pytest.raises(DocumentSafetyError) as caught:
        transaction.preview(b"enabled: true\n")

    assert caught.value.code == "DOCUMENT_UNSAFE"
    assert target.read_bytes() == b"enabled: false\n"


def test_preview_rejects_a_hardlinked_ordinary_document_before_reading_content(
    tmp_path,
):
    outside = tmp_path / "outside.yaml"
    original = b"private_external_canary: do-not-read-or-replace\n"
    outside.write_bytes(original)
    document = tmp_path / "app.yaml"
    os.link(outside, document)
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )

    with pytest.raises(DocumentSafetyError) as caught:
        transaction.preview(b"enabled: true\n")

    assert caught.value.code == "DOCUMENT_UNSAFE"
    assert document.stat().st_nlink == 2
    assert document.read_bytes() == original
    assert outside.read_bytes() == original
    assert not (tmp_path / "backups").exists()


def test_preview_rejects_an_ordinary_document_not_owned_by_the_platform_user(
    tmp_path,
):
    document = tmp_path / "overlay_config.json"
    original = b'{"spica_voice_volume": 0.5}\n'
    document.write_bytes(original)
    wrong_owner_platform = platform_capabilities_for(
        os_family="posix",
        runtime_name="linux",
        user_id=os.getuid() + 1,
        temp_directory=tmp_path / "platform-tmp",
    )
    transaction = ManagedDocumentTransaction(
        document,
        backup_root=tmp_path / "backups",
        platform_capabilities=wrong_owner_platform,
    )

    with pytest.raises(DocumentSafetyError) as caught:
        transaction.preview(b'{"spica_voice_volume": 0.8}\n')

    assert caught.value.code == "DOCUMENT_UNSAFE"
    assert document.read_bytes() == original
    assert not (tmp_path / "backups").exists()


def test_preview_does_not_follow_a_symlink_swapped_in_after_lstat(
    tmp_path, monkeypatch
):
    outside = tmp_path / "outside.yaml"
    outside.write_bytes(b"secret: outside\n")
    document = tmp_path / "app.yaml"
    document.write_bytes(b"enabled: false\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    real_lstat = Path.lstat
    swapped = False

    def swap_after_lstat(path):
        nonlocal swapped
        result = real_lstat(path)
        if path == document and not swapped:
            swapped = True
            document.unlink()
            document.symlink_to(outside)
        return result

    monkeypatch.setattr(Path, "lstat", swap_after_lstat)

    with pytest.raises(DocumentSafetyError) as caught:
        transaction.preview(b"enabled: true\n")

    assert caught.value.code == "DOCUMENT_UNSAFE"
    assert outside.read_bytes() == b"secret: outside\n"


def test_preview_rejects_a_document_beneath_a_symlinked_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "app.yaml").write_bytes(b"secret: outside\n")
    managed_parent = tmp_path / "managed"
    managed_parent.symlink_to(outside, target_is_directory=True)
    transaction = _transaction(
        managed_parent / "app.yaml",
        backup_root=tmp_path / "backups",
    )

    with pytest.raises(DocumentSafetyError) as caught:
        transaction.preview(b"enabled: true\n")

    assert caught.value.code == "DOCUMENT_UNSAFE"
    assert (outside / "app.yaml").read_bytes() == b"secret: outside\n"


def test_commit_rejects_a_symlinked_backup_root(tmp_path):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"enabled: false\n")
    outside = tmp_path / "outside-backups"
    outside.mkdir()
    backup_root = tmp_path / "backups"
    backup_root.symlink_to(outside, target_is_directory=True)
    transaction = _transaction(document, backup_root=backup_root)
    revision = transaction.preview(b"enabled: true\n").current.revision

    with pytest.raises(DocumentSafetyError) as caught:
        transaction.commit(
            b"enabled: true\n",
            expected_revision=revision,
        )

    assert caught.value.code == "DOCUMENT_UNSAFE"
    assert document.read_bytes() == b"enabled: false\n"
    assert not list(outside.iterdir())


def test_commit_rejects_a_symlink_substituted_for_the_stable_lock(tmp_path):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"version: 0\n")
    backup_root = tmp_path / "backups"
    lock_root = tmp_path / "locks"
    transaction = _transaction(
        document,
        backup_root=backup_root,
        lock_root=lock_root,
    )
    revision = transaction.preview(b"version: 1\n").current.revision
    committed = transaction.commit(b"version: 1\n", expected_revision=revision)
    lock_path, = lock_root.glob("*.lock")
    lock_path.unlink()
    outside = tmp_path / "outside-lock"
    outside.write_bytes(b"do not lock me")
    lock_path.symlink_to(outside)

    with pytest.raises(DocumentSafetyError) as caught:
        transaction.commit(
            b"version: 2\n",
            expected_revision=committed.snapshot.revision,
        )

    assert caught.value.code == "DOCUMENT_UNSAFE"
    assert document.read_bytes() == b"version: 1\n"
    assert outside.read_bytes() == b"do not lock me"


def test_commit_atomically_publishes_exact_candidate_bytes(tmp_path):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"enabled: false\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    preview = transaction.preview(b"enabled: true\r\n# keep bytes\r\n")

    result = transaction.commit(
        b"enabled: true\r\n# keep bytes\r\n",
        expected_revision=preview.current.revision,
    )

    assert document.read_bytes() == b"enabled: true\r\n# keep bytes\r\n"
    assert result.snapshot.content == b"enabled: true\r\n# keep bytes\r\n"
    assert result.restore_point is not None
    assert result.restore_point.id


def test_commit_of_unchanged_bytes_is_a_noop_without_a_restore_point(tmp_path):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"enabled: true\n")
    backup_root = tmp_path / "backups"
    transaction = _transaction(document, backup_root=backup_root)
    preview = transaction.preview(b"enabled: true\n")

    result = transaction.commit(
        b"enabled: true\n",
        expected_revision=preview.current.revision,
    )

    assert result.snapshot == preview.current
    assert result.restore_point is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_restore_point_storage_is_private(tmp_path):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"enabled: false\n")
    document.chmod(0o664)
    backup_root = tmp_path / "backups"
    transaction = _transaction(document, backup_root=backup_root)
    preview = transaction.preview(b"enabled: true\n")

    committed = transaction.commit(
        b"enabled: true\n",
        expected_revision=preview.current.revision,
    )

    restore_dir, = backup_root.rglob(committed.restore_point.id)
    assert stat.S_IMODE(backup_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(restore_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((restore_dir / "metadata").stat().st_mode) == 0o600
    assert stat.S_IMODE((restore_dir / "content").stat().st_mode) == 0o600
    assert stat.S_IMODE(document.stat().st_mode) == 0o664


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_ordinary_document_rejects_non_private_restore_storage(tmp_path):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"enabled: false\n")
    document.chmod(0o664)
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    backup_root.chmod(0o777)
    transaction = _transaction(document, backup_root=backup_root)
    preview = transaction.preview(b"enabled: true\n")

    with pytest.raises(DocumentSafetyError) as caught:
        transaction.commit(
            b"enabled: true\n",
            expected_revision=preview.current.revision,
        )

    assert caught.value.code == "DOCUMENT_UNSAFE"
    assert stat.S_IMODE(document.stat().st_mode) == 0o664
    assert document.read_bytes() == b"enabled: false\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_explicit_publish_mode_applies_to_commit_and_rollback(tmp_path):
    document = tmp_path / "xiaosan.env"
    document.write_bytes(b"MODEL=before\n")
    document.chmod(0o664)
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
        retention=1,
        publish_mode=0o600,
    )
    revision = transaction.preview(b"MODEL=after\n").current.revision

    committed = transaction.commit(b"MODEL=after\n", expected_revision=revision)
    assert stat.S_IMODE(document.stat().st_mode) == 0o600

    transaction.rollback(
        committed.restore_point.id,
        expected_revision=committed.snapshot.revision,
    )
    assert stat.S_IMODE(document.stat().st_mode) == 0o600


def test_commit_rejects_a_revision_changed_since_preview(tmp_path):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"owner: studio\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    preview = transaction.preview(b"owner: candidate\n")
    document.write_bytes(b"owner: other-session\n")

    with pytest.raises(DocumentConflictError) as caught:
        transaction.commit(
            b"owner: candidate\n",
            expected_revision=preview.current.revision,
        )

    assert caught.value.code == "DOCUMENT_CONFLICT"
    assert document.read_bytes() == b"owner: other-session\n"


def test_commit_rechecks_revision_after_restore_point_before_publication(
    tmp_path,
    monkeypatch,
):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"owner: studio\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    revision = transaction.preview(b"owner: candidate\n").current.revision
    after_first_transaction_fsync(
        monkeypatch,
        lambda: document.write_bytes(b"owner: other-session\n"),
    )

    with pytest.raises(DocumentConflictError) as caught:
        transaction.commit(
            b"owner: candidate\n",
            expected_revision=revision,
        )

    assert caught.value.code == "DOCUMENT_CONFLICT"
    assert document.read_bytes() == b"owner: other-session\n"
    assert transaction.restore_points() == ()


def test_commit_rechecks_revision_after_tempfile_is_prepared_before_replace(
    tmp_path,
    monkeypatch,
):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"owner: studio\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    revision = transaction.preview(b"owner: candidate\n").current.revision
    real_mkstemp = transaction_module.tempfile.mkstemp

    def prepare_temp_then_external_edit(*args, **kwargs):
        prepared = real_mkstemp(*args, **kwargs)
        document.write_bytes(b"owner: other-session\n")
        return prepared

    monkeypatch.setattr(transaction_module.tempfile, "mkstemp", prepare_temp_then_external_edit)

    with pytest.raises(DocumentConflictError) as caught:
        transaction.commit(
            b"owner: candidate\n",
            expected_revision=revision,
        )

    assert caught.value.code == "DOCUMENT_CONFLICT"
    assert document.read_bytes() == b"owner: other-session\n"
    assert transaction.restore_points() == ()


def test_commit_rejects_same_bytes_replaced_inside_publication_callback(
    tmp_path,
):
    document = tmp_path / "app.yaml"
    original = b"owner: studio\n"
    document.write_bytes(original)
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    revision = transaction.preview(b"owner: candidate\n").current.revision
    external_inode: int | None = None

    def external_writer_replaces_same_bytes():
        nonlocal external_inode
        external = tmp_path / "external-writer.yaml"
        external.write_bytes(original)
        external_inode = external.stat().st_ino
        os.replace(external, document)

    with pytest.raises(DocumentConflictError) as caught:
        transaction.commit(
            b"owner: candidate\n",
            expected_revision=revision,
            before_publication=external_writer_replaces_same_bytes,
        )

    assert caught.value.code == "DOCUMENT_CONFLICT"
    assert document.read_bytes() == original
    assert document.stat().st_ino == external_inode
    assert transaction.restore_points() == ()


def test_commit_rejects_path_replacement_after_final_snapshot_opens(
    tmp_path,
    monkeypatch,
):
    document = tmp_path / "app.yaml"
    original = b"owner: studio\n"
    external_bytes = b"owner: other-session\n"
    document.write_bytes(original)
    original_stat = document.stat()
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    revision = transaction.preview(b"owner: candidate\n").current.revision
    real_read = transaction_module.os.read
    real_replace = os.replace
    document_reads = 0

    def replace_path_after_open_descriptor_read(descriptor, size):
        nonlocal document_reads
        chunk = real_read(descriptor, size)
        descriptor_stat = os.fstat(descriptor)
        if chunk and (descriptor_stat.st_dev, descriptor_stat.st_ino) == (
            original_stat.st_dev,
            original_stat.st_ino,
        ):
            document_reads += 1
            if document_reads == 2:
                external = tmp_path / "external-writer.yaml"
                external.write_bytes(external_bytes)
                real_replace(external, document)
        return chunk

    monkeypatch.setattr(
        transaction_module.os,
        "read",
        replace_path_after_open_descriptor_read,
    )

    with pytest.raises(DocumentConflictError) as caught:
        transaction.commit(
            b"owner: candidate\n",
            expected_revision=revision,
        )

    assert caught.value.code == "DOCUMENT_CONFLICT"
    assert document_reads >= 2
    assert document.read_bytes() == external_bytes
    assert transaction.restore_points() == ()


def test_commit_rejects_in_place_rewrite_after_final_snapshot_read(
    tmp_path,
    monkeypatch,
):
    document = tmp_path / "app.yaml"
    original = b"owner: studio\n"
    external_bytes = b"other\n"
    document.write_bytes(original)
    original_inode = document.stat().st_ino
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    revision = transaction.preview(b"owner: candidate\n").current.revision
    real_read = transaction_module.os.read
    document_reads = 0

    def rewrite_same_inode_after_descriptor_read(descriptor, size):
        nonlocal document_reads
        chunk = real_read(descriptor, size)
        descriptor_stat = os.fstat(descriptor)
        if chunk and descriptor_stat.st_ino == original_inode:
            document_reads += 1
            if document_reads == 2:
                document.write_bytes(external_bytes)
                assert document.stat().st_ino == original_inode
        return chunk

    monkeypatch.setattr(
        transaction_module.os,
        "read",
        rewrite_same_inode_after_descriptor_read,
    )

    with pytest.raises(DocumentConflictError) as caught:
        transaction.commit(
            b"owner: candidate\n",
            expected_revision=revision,
        )

    assert caught.value.code == "DOCUMENT_CONFLICT"
    assert document_reads >= 2
    assert document.read_bytes() == external_bytes
    assert document.stat().st_ino == original_inode
    assert transaction.restore_points() == ()


def test_commit_rejects_same_size_rewrite_even_if_mtime_is_restored(
    tmp_path,
    monkeypatch,
):
    document = tmp_path / "app.yaml"
    original = b"owner: studio\n"
    external_bytes = b"owner: other!\n"
    assert len(external_bytes) == len(original)
    document.write_bytes(original)
    original_stat = document.stat()
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    revision = transaction.preview(b"owner: candidate\n").current.revision
    real_read = transaction_module.os.read
    document_reads = 0

    def rewrite_then_restore_mtime(descriptor, size):
        nonlocal document_reads
        chunk = real_read(descriptor, size)
        descriptor_stat = os.fstat(descriptor)
        if chunk and descriptor_stat.st_ino == original_stat.st_ino:
            document_reads += 1
            if document_reads == 2:
                document.write_bytes(external_bytes)
                os.utime(
                    document,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )
        return chunk

    monkeypatch.setattr(
        transaction_module.os,
        "read",
        rewrite_then_restore_mtime,
    )

    with pytest.raises(DocumentConflictError) as caught:
        transaction.commit(
            b"owner: candidate\n",
            expected_revision=revision,
        )

    assert caught.value.code == "DOCUMENT_CONFLICT"
    assert document_reads >= 2
    assert document.read_bytes() == external_bytes
    assert document.stat().st_ino == original_stat.st_ino


@pytest.mark.skipif(os.name != "posix", reason="POSIX hardlink contract")
def test_commit_rejects_hardlink_added_before_final_path_identity_check(
    tmp_path,
    monkeypatch,
):
    document = tmp_path / "app.yaml"
    original = b"owner: studio\n"
    document.write_bytes(original)
    original_inode = document.stat().st_ino
    platform = platform_capabilities_for(
        os_family="posix",
        runtime_name="linux",
        user_id=os.getuid(),
        temp_directory=tmp_path / "platform-tmp",
    )
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
        platform_capabilities=platform,
    )
    revision = transaction.preview(b"owner: candidate\n").current.revision
    identity_owner = platform.file_identity
    capture_descriptor = identity_owner.capture_descriptor
    original_captures = 0
    outside_link = tmp_path / "outside-hardlink.yaml"

    def capture_then_add_hardlink(descriptor):
        nonlocal original_captures
        identity = capture_descriptor(descriptor)
        if os.fstat(descriptor).st_ino == original_inode:
            original_captures += 1
            if original_captures == 2:
                os.link(document, outside_link)
        return identity

    monkeypatch.setattr(
        identity_owner,
        "capture_descriptor",
        capture_then_add_hardlink,
    )

    with pytest.raises(DocumentConflictError) as caught:
        transaction.commit(
            b"owner: candidate\n",
            expected_revision=revision,
        )

    assert caught.value.code == "DOCUMENT_CONFLICT"
    assert original_captures >= 2
    assert document.read_bytes() == original
    assert outside_link.read_bytes() == original
    assert document.stat().st_ino == outside_link.stat().st_ino


def test_commit_callback_runs_after_temp_fsync_and_before_final_target_cas(
    tmp_path,
    monkeypatch,
):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"version: old\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    revision = transaction.preview(b"version: new\n").current.revision
    events: list[str] = []
    temporary_descriptor: int | None = None
    real_mkstemp = transaction_module.tempfile.mkstemp
    real_fsync = transaction_module.os.fsync
    real_open = transaction_module.os.open
    real_replace = os.replace
    callback_completed = False
    target_cas_recorded = False

    def record_mkstemp(*args, **kwargs):
        nonlocal temporary_descriptor
        temporary_descriptor, name = real_mkstemp(*args, **kwargs)
        return temporary_descriptor, name

    def record_fsync(descriptor):
        result = real_fsync(descriptor)
        if descriptor == temporary_descriptor:
            events.append("temp_fsync")
        return result

    def record_callback():
        nonlocal callback_completed
        callback_completed = True
        events.append("callback")

    def record_open(path, flags, mode=0o777):
        nonlocal target_cas_recorded
        if (
            callback_completed
            and not target_cas_recorded
            and Path(path) == document
        ):
            target_cas_recorded = True
            events.append("target_cas")
        return real_open(path, flags, mode)

    def record_replace(source, target):
        if Path(target) == document:
            events.append("replace")
        return real_replace(source, target)

    monkeypatch.setattr(transaction_module.tempfile, "mkstemp", record_mkstemp)
    monkeypatch.setattr(transaction_module.os, "fsync", record_fsync)
    monkeypatch.setattr(transaction_module.os, "open", record_open)
    monkeypatch.setattr(transaction_module.os, "replace", record_replace)

    transaction.commit(
        b"version: new\n",
        expected_revision=revision,
        before_publication=record_callback,
    )

    assert events == ["temp_fsync", "callback", "target_cas", "replace"]


def test_commit_callback_failure_removes_only_attempt_state(
    tmp_path,
):
    document = tmp_path / "app.yaml"
    original = b"version: old\n"
    document.write_bytes(original)
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    revision = transaction.preview(b"version: new\n").current.revision

    def reject_publication():
        raise RuntimeError("synthetic owner guard rejection")

    with pytest.raises(RuntimeError, match="synthetic owner guard rejection"):
        transaction.commit(
            b"version: new\n",
            expected_revision=revision,
            before_publication=reject_publication,
        )

    assert document.read_bytes() == original
    assert transaction.restore_points() == ()
    assert not list(tmp_path.glob(".app.yaml.config-studio-*"))


def test_transactions_for_one_document_share_a_bounded_process_mutex(
    tmp_path, monkeypatch
):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"version: 1\n")
    first = _transaction(
        document,
        backup_root=tmp_path / "backups",
        lock_timeout=1,
    )
    second = _transaction(
        document,
        backup_root=tmp_path / "backups",
        lock_timeout=0.01,
    )
    revision = first.preview(b"version: 2\n").current.revision
    replace_started = threading.Event()
    allow_replace = threading.Event()
    real_replace = __import__("os").replace

    def delayed_replace(source, target):
        replace_started.set()
        assert allow_replace.wait(2)
        real_replace(source, target)

    monkeypatch.setattr("spica.config.document_transaction.os.replace", delayed_replace)
    first_error: list[BaseException] = []

    def commit_first():
        try:
            first.commit(b"version: 2\n", expected_revision=revision)
        except BaseException as exc:  # pragma: no cover - asserted after join
            first_error.append(exc)

    thread = threading.Thread(target=commit_first)
    thread.start()
    assert replace_started.wait(2)
    try:
        with pytest.raises(DocumentBusyError) as caught:
            second.commit(b"version: 3\n", expected_revision=revision)
        assert caught.value.code == "DOCUMENT_BUSY"
    finally:
        allow_replace.set()
        thread.join(2)

    assert not first_error
    assert not thread.is_alive()
    assert document.read_bytes() == b"version: 2\n"


def test_commit_honors_the_same_bounded_lock_across_processes(tmp_path):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"version: 1\n")
    child_backup_root = tmp_path / "child-backups"
    parent_backup_root = tmp_path / "parent-backups"
    lock_root = tmp_path / "shared-locks"
    marker = tmp_path / "replace-started"
    child_script = """
import sys
import time
from pathlib import Path
import spica.config.document_transaction as module
from spica.adapters.config_studio.platform import current_platform_capabilities

document, backup_root, marker, lock_root = map(Path, sys.argv[1:])
real_replace = module.os.replace
def delayed_replace(source, target):
    marker.write_text("ready", encoding="ascii")
    time.sleep(1)
    real_replace(source, target)
module.os.replace = delayed_replace
transaction = module.ManagedDocumentTransaction(
    document,
    backup_root=backup_root,
    lock_root=lock_root,
    platform_capabilities=current_platform_capabilities(),
)
revision = transaction.preview(b"version: 2\\n").current.revision
transaction.commit(b"version: 2\\n", expected_revision=revision)
"""
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            child_script,
            str(document),
            str(child_backup_root),
            str(marker),
            str(lock_root),
        ],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **{
                name: ""
                for name in consumed_env_names() | frozenset(LEGACY_ENV_VARS)
            },
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
        },
    )
    deadline = time.monotonic() + 3
    try:
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), child.communicate(timeout=1)
        transaction = _transaction(
            document,
            backup_root=parent_backup_root,
            lock_root=lock_root,
            lock_timeout=0.05,
        )
        revision = transaction.preview(b"version: 3\n").current.revision

        started = time.monotonic()
        with pytest.raises(DocumentBusyError) as caught:
            transaction.commit(b"version: 3\n", expected_revision=revision)

        assert caught.value.code == "DOCUMENT_BUSY"
        assert time.monotonic() - started < 0.5
    finally:
        try:
            stdout, stderr = child.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            child.terminate()
            stdout, stderr = child.communicate(timeout=3)

    assert child.returncode == 0, (stdout, stderr)
    assert document.read_bytes() == b"version: 2\n"


def test_failed_publish_leaves_original_bytes_intact_and_removes_temp_file(
    tmp_path, monkeypatch
):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"safe: original\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    revision = transaction.preview(b"unsafe: candidate\n").current.revision

    def fail_replace(source, target):
        raise OSError("injected replace failure")

    monkeypatch.setattr("spica.config.document_transaction.os.replace", fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        transaction.commit(
            b"unsafe: candidate\n",
            expected_revision=revision,
        )

    assert document.read_bytes() == b"safe: original\n"
    assert not list(tmp_path.glob(".app.yaml.config-studio-*"))
    assert transaction.restore_points() == ()


def test_commit_reports_conflict_if_a_nonparticipating_writer_wins_after_publish(
    tmp_path, monkeypatch
):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"version: old\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    revision = transaction.preview(b"version: studio\n").current.revision
    real_replace = os.replace

    def overwrite_after_replace(source, target):
        real_replace(source, target)
        external = Path(target).with_name(f".{Path(target).name}.external-writer")
        external.write_bytes(b"version: legacy-writer\n")
        real_replace(external, target)

    monkeypatch.setattr(
        "spica.config.document_transaction.os.replace",
        overwrite_after_replace,
    )

    with pytest.raises(DocumentConflictError):
        transaction.commit(
            b"version: studio\n",
            expected_revision=revision,
        )

    assert document.read_bytes() == b"version: legacy-writer\n"


def test_commit_reports_conflict_for_same_bytes_on_a_different_inode_after_publish(
    tmp_path, monkeypatch
):
    document = tmp_path / "app.yaml"
    candidate = b"version: studio\n"
    document.write_bytes(b"version: old\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    revision = transaction.preview(candidate).current.revision
    real_replace = os.replace
    external_inode: int | None = None

    def replace_then_publish_same_bytes_from_another_inode(source, target):
        nonlocal external_inode
        real_replace(source, target)
        external = Path(target).with_name(f".{Path(target).name}.external-writer")
        external.write_bytes(candidate)
        external_inode = external.stat().st_ino
        real_replace(external, target)

    monkeypatch.setattr(
        transaction_module.os,
        "replace",
        replace_then_publish_same_bytes_from_another_inode,
    )

    with pytest.raises(DocumentConflictError) as caught:
        transaction.commit(candidate, expected_revision=revision)

    assert caught.value.code == "DOCUMENT_CONFLICT"
    assert document.read_bytes() == candidate
    assert document.stat().st_ino == external_inode


def test_commit_rejects_in_place_rewrite_during_post_publication_read(
    tmp_path,
    monkeypatch,
):
    document = tmp_path / "app.yaml"
    candidate = b"version: studio\n"
    external_bytes = b"other\n"
    document.write_bytes(b"version: old\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    revision = transaction.preview(candidate).current.revision
    real_read = transaction_module.os.read
    publication_inode: int | None = None
    publication_reads = 0
    real_replace = os.replace

    def remember_publication_inode(source, target):
        nonlocal publication_inode
        real_replace(source, target)
        publication_inode = document.stat().st_ino

    def rewrite_same_inode_after_publication_read(descriptor, size):
        nonlocal publication_reads
        chunk = real_read(descriptor, size)
        descriptor_stat = os.fstat(descriptor)
        if chunk and publication_inode == descriptor_stat.st_ino:
            publication_reads += 1
            if publication_reads == 1:
                document.write_bytes(external_bytes)
                assert document.stat().st_ino == publication_inode
        return chunk

    monkeypatch.setattr(transaction_module.os, "replace", remember_publication_inode)
    monkeypatch.setattr(
        transaction_module.os,
        "read",
        rewrite_same_inode_after_publication_read,
    )

    with pytest.raises(DocumentConflictError) as caught:
        transaction.commit(candidate, expected_revision=revision)

    assert caught.value.code == "DOCUMENT_CONFLICT"
    assert publication_reads == 1
    assert document.read_bytes() == external_bytes
    assert document.stat().st_ino == publication_inode


def test_commit_reports_conflict_if_target_becomes_a_symlink_after_publish(
    tmp_path, monkeypatch
):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"version: old\n")
    outside = tmp_path / "outside-owner-file"
    outside.write_bytes(b"outside must remain untouched\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    revision = transaction.preview(b"version: studio\n").current.revision
    real_replace = os.replace

    def replace_then_swap_to_symlink(source, target):
        real_replace(source, target)
        Path(target).unlink()
        Path(target).symlink_to(outside)

    monkeypatch.setattr(
        "spica.config.document_transaction.os.replace",
        replace_then_swap_to_symlink,
    )

    with pytest.raises(DocumentConflictError):
        transaction.commit(
            b"version: studio\n",
            expected_revision=revision,
        )

    assert document.is_symlink()
    assert outside.read_bytes() == b"outside must remain untouched\n"


def test_post_publish_retention_failure_is_reported_as_maintenance_not_false_commit_failure(
    tmp_path, monkeypatch
):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"version: 1\n")
    backup_root = tmp_path / "backups"
    transaction = _transaction(
        document,
        backup_root=backup_root,
    )
    revision = transaction.preview(b"version: 2\n").current.revision
    real_iterdir = Path.iterdir

    def fail_retention(path):
        if path.parent == backup_root:
            raise OSError("injected retention failure")
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_retention)

    committed = transaction.commit(
        b"version: 2\n",
        expected_revision=revision,
    )

    assert document.read_bytes() == b"version: 2\n"
    assert committed.snapshot.content == b"version: 2\n"
    assert committed.maintenance_code == "RESTORE_RETENTION_DEGRADED"


@pytest.mark.skipif(os.name != "posix", reason="POSIX private recovery contract")
def test_failed_publication_recovery_rechecks_identity_after_hardening(
    tmp_path,
    monkeypatch,
):
    document = tmp_path / "xiaosan.env"
    original = b"MODEL=original\n"
    candidate = b"MODEL=candidate\n"
    document.write_bytes(original)
    document.chmod(0o600)
    platform = platform_capabilities_for(
        os_family="posix",
        runtime_name="linux",
        user_id=os.getuid(),
        temp_directory=tmp_path / "platform-tmp",
    )
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
        retention=1,
        publish_mode=0o600,
        private_posix=True,
        platform_capabilities=platform,
    )
    revision = transaction.preview(candidate).current.revision
    committed = transaction.commit(
        candidate,
        expected_revision=revision,
        defer_retention=True,
    )
    document.chmod(0o640)
    real_fchmod = transaction_module.os.fchmod
    real_replace = os.replace
    identity_owner = platform.file_identity
    path_matches_no_follow = identity_owner.path_matches_no_follow
    external_inode: int | None = None
    external_published = False
    hardened_live_document = False

    def remember_live_document_hardening(descriptor, mode):
        nonlocal hardened_live_document
        result = real_fchmod(descriptor, mode)
        descriptor_stat = os.fstat(descriptor)
        document_stat = document.stat()
        if (
            (descriptor_stat.st_dev, descriptor_stat.st_ino)
            == (document_stat.st_dev, document_stat.st_ino)
            and document.read_bytes() == original
        ):
            hardened_live_document = True
        return result

    def publish_external_before_identity_check(path, identity):
        nonlocal external_inode, external_published
        if (
            hardened_live_document
            and not external_published
            and Path(path) == document
        ):
            external_published = True
            external = tmp_path / "external-writer.env"
            external.write_bytes(original)
            external.chmod(0o600)
            external_inode = external.stat().st_ino
            real_replace(external, document)
        return path_matches_no_follow(path, identity)

    monkeypatch.setattr(
        transaction_module.os,
        "fchmod",
        remember_live_document_hardening,
    )
    monkeypatch.setattr(
        identity_owner,
        "path_matches_no_follow",
        publish_external_before_identity_check,
    )

    with pytest.raises(DocumentConflictError) as caught:
        transaction.recover_failed_publication(
            committed.restore_point.id,
            expected_snapshot=committed.snapshot,
            previous_revision=revision,
        )

    assert caught.value.code == "DOCUMENT_CONFLICT"
    assert document.read_bytes() == original
    assert document.stat().st_ino == external_inode
    assert tuple(item.id for item in transaction.restore_points()) == (
        committed.restore_point.id,
    )


def test_parent_fsync_failure_after_replace_reports_durability_without_false_failure(
    tmp_path, monkeypatch
):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"version: old\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    revision = transaction.preview(b"version: new\n").current.revision
    real_fsync = transaction_module.os.fsync
    parent_stat = document.parent.stat()

    def fail_live_parent(descriptor):
        descriptor_stat = os.fstat(descriptor)
        if (
            stat.S_ISDIR(descriptor_stat.st_mode)
            and (descriptor_stat.st_dev, descriptor_stat.st_ino)
            == (parent_stat.st_dev, parent_stat.st_ino)
            and document.read_bytes() == b"version: new\n"
        ):
            raise OSError("injected live parent fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(transaction_module.os, "fsync", fail_live_parent)

    committed = transaction.commit(
        b"version: new\n",
        expected_revision=revision,
    )

    assert document.read_bytes() == b"version: new\n"
    assert committed.snapshot.content == b"version: new\n"
    assert committed.maintenance_code == "DOCUMENT_DURABILITY_UNCONFIRMED"


def test_rollback_restores_exact_bytes_and_first_backs_up_current_state(tmp_path):
    document = tmp_path / "app.yaml"
    original = b"quoted: 'value'\r\n# original comment\r\n"
    replacement = b"quoted: value\n"
    document.write_bytes(original)
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    before = transaction.preview(replacement).current.revision
    committed = transaction.commit(replacement, expected_revision=before)

    rolled_back = transaction.rollback(
        committed.restore_point.id,
        expected_revision=committed.snapshot.revision,
    )

    assert document.read_bytes() == original
    assert rolled_back.snapshot.content == original
    assert rolled_back.restore_point is not None
    assert rolled_back.restore_point.id != committed.restore_point.id

    undone = transaction.rollback(
        rolled_back.restore_point.id,
        expected_revision=rolled_back.snapshot.revision,
    )
    assert undone.snapshot.content == replacement


def test_rollback_reports_conflict_if_a_nonparticipating_writer_wins_after_publish(
    tmp_path, monkeypatch
):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"version: original\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    original_revision = transaction.preview(b"version: current\n").current.revision
    committed = transaction.commit(
        b"version: current\n",
        expected_revision=original_revision,
    )
    real_replace = os.replace

    def overwrite_after_replace(source, target):
        real_replace(source, target)
        external = Path(target).with_name(f".{Path(target).name}.external-writer")
        external.write_bytes(b"version: legacy-writer\n")
        real_replace(external, target)

    monkeypatch.setattr(
        "spica.config.document_transaction.os.replace",
        overwrite_after_replace,
    )

    with pytest.raises(DocumentConflictError):
        transaction.rollback(
            committed.restore_point.id,
            expected_revision=committed.snapshot.revision,
        )

    assert document.read_bytes() == b"version: legacy-writer\n"


def test_rollback_reports_conflict_for_same_bytes_on_a_different_inode_after_publish(
    tmp_path, monkeypatch
):
    document = tmp_path / "app.yaml"
    original = b"version: original\n"
    document.write_bytes(original)
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    original_revision = transaction.preview(b"version: current\n").current.revision
    committed = transaction.commit(
        b"version: current\n",
        expected_revision=original_revision,
    )
    real_replace = os.replace
    external_inode: int | None = None

    def replace_then_publish_same_bytes_from_another_inode(source, target):
        nonlocal external_inode
        real_replace(source, target)
        external = Path(target).with_name(f".{Path(target).name}.external-writer")
        external.write_bytes(original)
        external_inode = external.stat().st_ino
        real_replace(external, target)

    monkeypatch.setattr(
        transaction_module.os,
        "replace",
        replace_then_publish_same_bytes_from_another_inode,
    )

    with pytest.raises(DocumentConflictError) as caught:
        transaction.rollback(
            committed.restore_point.id,
            expected_revision=committed.snapshot.revision,
        )

    assert caught.value.code == "DOCUMENT_CONFLICT"
    assert document.read_bytes() == original
    assert document.stat().st_ino == external_inode


def test_rollback_rechecks_revision_after_undo_restore_point_before_publication(
    tmp_path,
    monkeypatch,
):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"version: original\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    original_revision = transaction.preview(b"version: current\n").current.revision
    committed = transaction.commit(
        b"version: current\n",
        expected_revision=original_revision,
    )
    restore_ids_before = tuple(item.id for item in transaction.restore_points())
    after_first_transaction_fsync(
        monkeypatch,
        lambda: document.write_bytes(b"version: other-session\n"),
    )

    with pytest.raises(DocumentConflictError) as caught:
        transaction.rollback(
            committed.restore_point.id,
            expected_revision=committed.snapshot.revision,
        )

    assert caught.value.code == "DOCUMENT_CONFLICT"
    assert document.read_bytes() == b"version: other-session\n"
    assert tuple(item.id for item in transaction.restore_points()) == restore_ids_before


def test_rollback_rechecks_revision_after_tempfile_is_prepared_before_replace(
    tmp_path,
    monkeypatch,
):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"version: original\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    original_revision = transaction.preview(b"version: current\n").current.revision
    committed = transaction.commit(
        b"version: current\n",
        expected_revision=original_revision,
    )
    real_mkstemp = transaction_module.tempfile.mkstemp

    def prepare_temp_then_external_edit(*args, **kwargs):
        prepared = real_mkstemp(*args, **kwargs)
        document.write_bytes(b"version: other-session\n")
        return prepared

    monkeypatch.setattr(transaction_module.tempfile, "mkstemp", prepare_temp_then_external_edit)

    with pytest.raises(DocumentConflictError) as caught:
        transaction.rollback(
            committed.restore_point.id,
            expected_revision=committed.snapshot.revision,
        )

    assert caught.value.code == "DOCUMENT_CONFLICT"
    assert document.read_bytes() == b"version: other-session\n"


def test_rollback_rejects_same_bytes_replaced_inside_publication_callback(
    tmp_path,
):
    document = tmp_path / "app.yaml"
    original = b"version: original\n"
    current = b"version: current\n"
    document.write_bytes(original)
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    original_revision = transaction.preview(current).current.revision
    committed = transaction.commit(current, expected_revision=original_revision)
    restore_ids_before = tuple(item.id for item in transaction.restore_points())
    external_inode: int | None = None

    def external_writer_replaces_same_bytes():
        nonlocal external_inode
        external = tmp_path / "external-writer.yaml"
        external.write_bytes(current)
        external_inode = external.stat().st_ino
        os.replace(external, document)

    with pytest.raises(DocumentConflictError) as caught:
        transaction.rollback(
            committed.restore_point.id,
            expected_revision=committed.snapshot.revision,
            before_publication=external_writer_replaces_same_bytes,
        )

    assert caught.value.code == "DOCUMENT_CONFLICT"
    assert document.read_bytes() == current
    assert document.stat().st_ino == external_inode
    assert tuple(item.id for item in transaction.restore_points()) == restore_ids_before


def test_rollback_rejects_path_replacement_after_final_snapshot_opens(
    tmp_path,
    monkeypatch,
):
    document = tmp_path / "app.yaml"
    original = b"version: original\n"
    current = b"version: current\n"
    external_bytes = b"version: other-session\n"
    document.write_bytes(original)
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    original_revision = transaction.preview(current).current.revision
    committed = transaction.commit(current, expected_revision=original_revision)
    current_stat = document.stat()
    restore_ids_before = tuple(item.id for item in transaction.restore_points())
    real_read = transaction_module.os.read
    real_replace = os.replace
    document_reads = 0

    def replace_path_after_open_descriptor_read(descriptor, size):
        nonlocal document_reads
        chunk = real_read(descriptor, size)
        descriptor_stat = os.fstat(descriptor)
        if chunk and (descriptor_stat.st_dev, descriptor_stat.st_ino) == (
            current_stat.st_dev,
            current_stat.st_ino,
        ):
            document_reads += 1
            if document_reads == 2:
                external = tmp_path / "external-writer.yaml"
                external.write_bytes(external_bytes)
                real_replace(external, document)
        return chunk

    monkeypatch.setattr(
        transaction_module.os,
        "read",
        replace_path_after_open_descriptor_read,
    )

    with pytest.raises(DocumentConflictError) as caught:
        transaction.rollback(
            committed.restore_point.id,
            expected_revision=committed.snapshot.revision,
        )

    assert caught.value.code == "DOCUMENT_CONFLICT"
    assert document_reads >= 2
    assert document.read_bytes() == external_bytes
    assert tuple(item.id for item in transaction.restore_points()) == restore_ids_before


def test_rollback_rejects_in_place_rewrite_after_final_snapshot_read(
    tmp_path,
    monkeypatch,
):
    document = tmp_path / "app.yaml"
    original = b"version: original\n"
    current = b"version: current\n"
    external_bytes = b"other\n"
    document.write_bytes(original)
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    original_revision = transaction.preview(current).current.revision
    committed = transaction.commit(current, expected_revision=original_revision)
    current_inode = document.stat().st_ino
    restore_ids_before = tuple(item.id for item in transaction.restore_points())
    real_read = transaction_module.os.read
    document_reads = 0

    def rewrite_same_inode_after_descriptor_read(descriptor, size):
        nonlocal document_reads
        chunk = real_read(descriptor, size)
        descriptor_stat = os.fstat(descriptor)
        if chunk and descriptor_stat.st_ino == current_inode:
            document_reads += 1
            if document_reads == 2:
                document.write_bytes(external_bytes)
                assert document.stat().st_ino == current_inode
        return chunk

    monkeypatch.setattr(
        transaction_module.os,
        "read",
        rewrite_same_inode_after_descriptor_read,
    )

    with pytest.raises(DocumentConflictError) as caught:
        transaction.rollback(
            committed.restore_point.id,
            expected_revision=committed.snapshot.revision,
        )

    assert caught.value.code == "DOCUMENT_CONFLICT"
    assert document_reads >= 2
    assert document.read_bytes() == external_bytes
    assert document.stat().st_ino == current_inode
    assert tuple(item.id for item in transaction.restore_points()) == restore_ids_before


def test_rollback_callback_runs_after_temp_fsync_and_before_final_target_cas(
    tmp_path,
    monkeypatch,
):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"version: original\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    original_revision = transaction.preview(b"version: current\n").current.revision
    committed = transaction.commit(
        b"version: current\n",
        expected_revision=original_revision,
    )
    events: list[str] = []
    temporary_descriptor: int | None = None
    real_mkstemp = transaction_module.tempfile.mkstemp
    real_fsync = transaction_module.os.fsync
    real_open = transaction_module.os.open
    real_replace = os.replace
    callback_completed = False
    target_cas_recorded = False

    def record_mkstemp(*args, **kwargs):
        nonlocal temporary_descriptor
        temporary_descriptor, name = real_mkstemp(*args, **kwargs)
        return temporary_descriptor, name

    def record_fsync(descriptor):
        result = real_fsync(descriptor)
        if descriptor == temporary_descriptor:
            events.append("temp_fsync")
        return result

    def record_callback():
        nonlocal callback_completed
        callback_completed = True
        events.append("callback")

    def record_open(path, flags, mode=0o777):
        nonlocal target_cas_recorded
        if (
            callback_completed
            and not target_cas_recorded
            and Path(path) == document
        ):
            target_cas_recorded = True
            events.append("target_cas")
        return real_open(path, flags, mode)

    def record_replace(source, target):
        if Path(target) == document:
            events.append("replace")
        return real_replace(source, target)

    monkeypatch.setattr(transaction_module.tempfile, "mkstemp", record_mkstemp)
    monkeypatch.setattr(transaction_module.os, "fsync", record_fsync)
    monkeypatch.setattr(transaction_module.os, "open", record_open)
    monkeypatch.setattr(transaction_module.os, "replace", record_replace)

    transaction.rollback(
        committed.restore_point.id,
        expected_revision=committed.snapshot.revision,
        before_publication=record_callback,
    )

    assert events == ["temp_fsync", "callback", "target_cas", "replace"]


def test_rollback_callback_failure_keeps_selected_restore_point_only(
    tmp_path,
):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"version: original\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    original_revision = transaction.preview(b"version: current\n").current.revision
    committed = transaction.commit(
        b"version: current\n",
        expected_revision=original_revision,
    )
    restore_ids_before = tuple(item.id for item in transaction.restore_points())

    def reject_publication():
        raise RuntimeError("synthetic owner guard rejection")

    with pytest.raises(RuntimeError, match="synthetic owner guard rejection"):
        transaction.rollback(
            committed.restore_point.id,
            expected_revision=committed.snapshot.revision,
            before_publication=reject_publication,
        )

    assert document.read_bytes() == b"version: current\n"
    assert tuple(item.id for item in transaction.restore_points()) == restore_ids_before
    assert not list(tmp_path.glob(".app.yaml.config-studio-*"))


def test_restore_snapshot_is_safe_for_backend_semantic_preview_only(tmp_path):
    document = tmp_path / "xiaosan.env"
    original = b"OPENAI_API_KEY='restore-canary'\n"
    document.write_bytes(original)
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    revision = transaction.preview(b"OPENAI_API_KEY='replacement'\n").current.revision
    committed = transaction.commit(
        b"OPENAI_API_KEY='replacement'\n",
        expected_revision=revision,
    )

    restore_snapshot = transaction.restore_snapshot(committed.restore_point.id)

    assert restore_snapshot.content == original
    assert "restore-canary" not in repr(restore_snapshot)
    assert restore_snapshot.revision.sha256 not in repr(restore_snapshot)

    with pytest.raises(RestorePointError) as caught:
        transaction.restore_snapshot("../../xiaosan.env")
    assert caught.value.code == "NO_VALID_RESTORE_POINT"

    restore_dir, = (tmp_path / "backups").rglob(committed.restore_point.id)
    (restore_dir / "content").write_bytes(b"OPENAI_API_KEY='tampered'\n")
    with pytest.raises(RestorePointError) as tampered:
        transaction.restore_snapshot(committed.restore_point.id)
    assert tampered.value.code == "NO_VALID_RESTORE_POINT"
    assert transaction.restore_points() == ()


@pytest.mark.skipif(os.name != "posix", reason="POSIX private document contract")
def test_private_transaction_rechecks_owner_and_link_count_inside_its_lock(tmp_path):
    outside = tmp_path / "outside.env"
    outside.write_bytes(b"MODEL=same\n")
    document = tmp_path / "xiaosan.env"
    os.link(outside, document)
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
        retention=1,
        publish_mode=0o600,
        private_posix=True,
    )

    with pytest.raises(DocumentSafetyError) as caught:
        transaction.preview(b"MODEL=new\n")

    assert caught.value.code == "DOCUMENT_UNSAFE"
    assert outside.read_bytes() == b"MODEL=same\n"


def test_rollback_restores_original_nonexistence(tmp_path):
    document = tmp_path / "new.yaml"
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    preview = transaction.preview(b"created: true\n")
    committed = transaction.commit(
        b"created: true\n",
        expected_revision=preview.current.revision,
    )

    rolled_back = transaction.rollback(
        committed.restore_point.id,
        expected_revision=committed.snapshot.revision,
    )

    assert rolled_back.snapshot.revision.exists is False
    assert not document.exists()


def test_rollback_to_nonexistence_rechecks_after_final_lstat_before_unlink(
    tmp_path,
):
    document = tmp_path / "new.yaml"
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    preview = transaction.preview(b"created: true\n")
    committed = transaction.commit(
        b"created: true\n",
        expected_revision=preview.current.revision,
    )

    with pytest.raises(DocumentConflictError) as caught:
        transaction.rollback(
            committed.restore_point.id,
            expected_revision=committed.snapshot.revision,
            before_publication=lambda: document.write_bytes(
                b"owner: other-session\n"
            ),
        )

    assert caught.value.code == "DOCUMENT_CONFLICT"
    assert document.read_bytes() == b"owner: other-session\n"


def test_rollback_to_nonexistence_treats_concurrent_delete_as_conflict(
    tmp_path,
):
    document = tmp_path / "new.yaml"
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    preview = transaction.preview(b"created: true\n")
    committed = transaction.commit(
        b"created: true\n",
        expected_revision=preview.current.revision,
    )

    with pytest.raises(DocumentConflictError) as caught:
        transaction.rollback(
            committed.restore_point.id,
            expected_revision=committed.snapshot.revision,
            before_publication=document.unlink,
        )

    assert caught.value.code == "DOCUMENT_CONFLICT"
    assert not document.exists()


def test_successful_commits_retain_only_the_five_newest_restore_points(tmp_path):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"version: 0\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    revision = transaction.preview(b"version: 1\n").current.revision
    restore_ids: list[str] = []
    for version in range(1, 7):
        committed = transaction.commit(
            f"version: {version}\n".encode(),
            expected_revision=revision,
        )
        restore_ids.append(committed.restore_point.id)
        revision = committed.snapshot.revision

    with pytest.raises(RestorePointError) as caught:
        transaction.rollback(restore_ids[0], expected_revision=revision)
    assert caught.value.code == "NO_VALID_RESTORE_POINT"
    assert document.read_bytes() == b"version: 6\n"

    rolled_back = transaction.rollback(
        restore_ids[-1],
        expected_revision=revision,
    )
    assert rolled_back.snapshot.content == b"version: 5\n"


def test_retention_always_keeps_the_restore_point_created_by_this_commit(
    tmp_path,
    monkeypatch,
):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"version: 0\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
        retention=1,
    )
    now = [200]
    monkeypatch.setattr(transaction_module.time, "time_ns", lambda: now[0])
    first = transaction.commit(
        b"version: 1\n",
        expected_revision=transaction.preview(b"").current.revision,
    )
    now[0] = 1

    second = transaction.commit(
        b"version: 2\n",
        expected_revision=first.snapshot.revision,
    )

    assert tuple(item.id for item in transaction.restore_points()) == (
        second.restore_point.id,
    )
    with pytest.raises(RestorePointError):
        transaction.restore_snapshot(first.restore_point.id)
    restored = transaction.rollback(
        second.restore_point.id,
        expected_revision=second.snapshot.revision,
    )
    assert restored.snapshot.content == b"version: 1\n"


def test_restore_point_ids_are_opaque_and_allocated_exclusively(
    tmp_path, monkeypatch
):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"version: 0\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
    )
    generated_ids = iter(("A" * 24, "A" * 24, "B" * 24, "C" * 24))
    monkeypatch.setattr(
        "spica.config.document_transaction.secrets.token_urlsafe",
        lambda _size: next(generated_ids),
    )
    first_revision = transaction.preview(b"version: 1\n").current.revision
    first = transaction.commit(b"version: 1\n", expected_revision=first_revision)
    second = transaction.commit(
        b"version: 2\n",
        expected_revision=first.snapshot.revision,
    )

    assert first.restore_point.id == "A" * 24
    assert second.restore_point.id == "B" * 24
    restored = transaction.rollback(
        first.restore_point.id,
        expected_revision=second.snapshot.revision,
    )
    assert restored.snapshot.content == b"version: 0\n"

    with pytest.raises(RestorePointError):
        transaction.rollback(
            "../../app.yaml",
            expected_revision=restored.snapshot.revision,
        )


def test_restore_point_listing_exposes_only_opaque_metadata_and_is_read_only(tmp_path):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"version: 0\n")
    backup_root = tmp_path / "backups"
    transaction = _transaction(document, backup_root=backup_root)

    assert transaction.restore_points() == ()
    assert not backup_root.exists()

    first = transaction.commit(
        b"version: 1\n",
        expected_revision=transaction.preview(b"").current.revision,
    )
    second = transaction.commit(
        b"version: 2\n",
        expected_revision=first.snapshot.revision,
    )

    metadata = transaction.restore_points()

    assert [item.id for item in metadata] == [
        second.restore_point.id,
        first.restore_point.id,
    ]
    assert all(item.created_at_ns > 0 for item in metadata)
    rendered = repr(metadata)
    assert str(document) not in rendered
    assert "sha256" not in rendered
    assert "content" not in rendered
    assert "size" not in rendered


def test_rollback_does_not_follow_restore_content_swapped_after_lstat(
    tmp_path, monkeypatch
):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"version: original\n")
    backup_root = tmp_path / "backups"
    transaction = _transaction(document, backup_root=backup_root)
    revision = transaction.preview(b"version: current\n").current.revision
    committed = transaction.commit(b"version: current\n", expected_revision=revision)
    restore_dir, = backup_root.rglob(committed.restore_point.id)
    restore_content = restore_dir / "content"
    outside = tmp_path / "outside"
    outside.write_bytes(b"version: outside\n")
    real_lstat = Path.lstat
    swapped = False

    def swap_after_lstat(path):
        nonlocal swapped
        result = real_lstat(path)
        if path == restore_content and not swapped:
            swapped = True
            restore_content.unlink()
            restore_content.symlink_to(outside)
        return result

    monkeypatch.setattr(Path, "lstat", swap_after_lstat)

    with pytest.raises(RestorePointError) as caught:
        transaction.rollback(
            committed.restore_point.id,
            expected_revision=committed.snapshot.revision,
        )

    assert caught.value.code == "NO_VALID_RESTORE_POINT"
    assert document.read_bytes() == b"version: current\n"
    assert outside.read_bytes() == b"version: outside\n"


def test_windows_preview_is_available_but_writes_fail_closed_until_verified(
    tmp_path,
):
    document = tmp_path / "app.yaml"
    document.write_bytes(b"enabled: false\n")
    transaction = _transaction(
        document,
        backup_root=tmp_path / "backups",
        platform_capabilities=platform_capabilities_for(
            os_family="nt",
            runtime_name="win32",
            user_id=None,
            temp_directory=tmp_path,
        ),
    )
    preview = transaction.preview(b"enabled: true\n")

    with pytest.raises(DocumentWriteUnsupportedError) as caught:
        transaction.commit(
            b"enabled: true\n",
            expected_revision=preview.current.revision,
        )

    assert caught.value.code == "WRITES_UNVERIFIED_ON_WINDOWS"
    assert document.read_bytes() == b"enabled: false\n"
