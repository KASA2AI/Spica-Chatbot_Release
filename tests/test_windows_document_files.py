"""Native NTFS/ACL acceptance; shared transaction behavior lives in its suite."""
import os
from pathlib import Path

import pytest

from spica.adapters.config_platform import current_platform_capabilities
from spica.config.document_transaction import DocumentConflictError, DocumentSafetyError, ManagedDocumentTransaction

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows file APIs")


def transaction(root):
    return ManagedDocumentTransaction(root / "prefs.json", backup_root=root / "backups",
        lock_root=root / "locks", platform_capabilities=current_platform_capabilities())


def test_windows_exact_bytes_private_restore_and_rollback(tmp_path):
    tx = transaction(tmp_path)
    original = b'original\r\n\x1a\xff'
    candidate = b'new\n\x1a\x00\xfe'
    tx.document_path.write_bytes(original)
    before = tx.preview(candidate).current
    result = tx.commit(candidate, expected_revision=before.revision)
    assert tx.document_path.read_bytes() == candidate
    assert result.maintenance_code == "DOCUMENT_DURABILITY_UNCONFIRMED"
    native = current_platform_capabilities().native_files
    native.validate_private(tx.document_path)
    for path in tx.backup_root.rglob("*"):
        native.validate_private(path, directory=path.is_dir())
    assert tx.restore_snapshot(result.restore_point.id).content == original
    restored = tx.rollback(result.restore_point.id, expected_revision=result.snapshot.revision)
    assert restored.snapshot.content == original
    assert tx.document_path.read_bytes() == original
    # No native handle can outlive the transaction and block a later rename.
    tx.document_path.replace(tmp_path / "moved.json")


@pytest.mark.parametrize("target", ["parent", "backups", "locks"])
def test_windows_refuses_junctions_without_touching_target(tmp_path, target):
    import _winapi

    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "untouched"
    marker.write_bytes(b"keep")
    link = tmp_path / ("managed" if target == "parent" else target)
    _winapi.CreateJunction(str(outside), str(link))
    try:
        tx = transaction(link if target == "parent" else tmp_path)
        with pytest.raises(DocumentSafetyError):
            preview = tx.preview(b"new")
            tx.commit(b"new", expected_revision=preview.current.revision)
        assert marker.read_bytes() == b"keep"
        assert not (outside / "prefs.json").exists()
    finally:
        # Remove the junction itself, never recurse into its target.
        os.rmdir(link)


def test_windows_rejects_public_restore_acl_before_publication(tmp_path):
    import win32security

    tx = transaction(tmp_path)
    tx.document_path.write_bytes(b"before")
    before = tx.preview(b"after").current
    tx.backup_root.mkdir()
    native = current_platform_capabilities().native_files
    native.prepare_directory(tx.backup_root)
    descriptor = win32security.GetNamedSecurityInfo(str(tx.backup_root), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION)
    acl = descriptor.GetSecurityDescriptorDacl()
    acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, 3, 0x1F01FF,
        win32security.CreateWellKnownSid(win32security.WinWorldSid))
    win32security.SetNamedSecurityInfo(str(tx.backup_root), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION, None, None, acl, None)
    with pytest.raises(DocumentSafetyError):
        tx.commit(b"after", expected_revision=before.revision)
    assert tx.document_path.read_bytes() == b"before"


def test_windows_detects_hardlink_added_during_publication(tmp_path):
    tx = transaction(tmp_path)
    tx.document_path.write_bytes(b"before")
    revision = tx.preview(b"after").current.revision
    outside_link = tmp_path / "other-link"
    with pytest.raises(DocumentConflictError):
        tx.commit(b"after", expected_revision=revision,
            before_publication=lambda: os.link(tx.document_path, outside_link))
    assert tx.document_path.read_bytes() == outside_link.read_bytes() == b"before"


def test_windows_failed_prepublication_check_cleans_restore_once(tmp_path, monkeypatch):
    tx = transaction(tmp_path)
    tx.document_path.write_bytes(b"before")
    revision = tx.preview(b"after").current.revision
    real = tx._snapshot
    reads = 0

    def fail_one_read():
        nonlocal reads
        reads += 1
        if reads == 2:
            raise PermissionError("injected transient read failure")
        return real()

    monkeypatch.setattr(tx, "_snapshot", fail_one_read)
    with pytest.raises(DocumentConflictError):
        tx.commit(b"after", expected_revision=revision)
    assert tx.document_path.read_bytes() == b"before"
    assert tx.restore_points() == ()


def test_saved_credential_does_not_inherit_read_access_for_other_users(tmp_path):
    import win32security
    from spica.config.secrets import load_secrets

    descriptor = win32security.GetNamedSecurityInfo(str(tmp_path), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION)
    acl = descriptor.GetSecurityDescriptorDacl()
    acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, 3, 0x120089,
        win32security.CreateWellKnownSid(win32security.WinWorldSid))
    win32security.SetNamedSecurityInfo(str(tmp_path), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION, None, None, acl, None)
    path = tmp_path / 'xiaosan.env'
    owner = load_secrets(with_environment_snapshot=True, repo_env_path=path,
                         parent_env_path=tmp_path / 'absent.env', inherited_environment={}, prime_process=False)
    owner.write_local_secret('openai_api_key', 'local-test-key')
    current_platform_capabilities().native_files.validate_private(path)
