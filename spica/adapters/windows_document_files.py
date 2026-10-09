"""Windows file identity, private ACLs and delete-sharing for document writes.

Imports are lazy so the platform factory remains usable on Linux. No Win32
handle escapes this adapter except a CRT descriptor owned by the transaction.
Windows has no portable directory-fsync guarantee: publication reports that
limitation through the existing durability maintenance receipt.
"""
from __future__ import annotations

import errno
import os
import secrets
from contextlib import contextmanager
from functools import cached_property, wraps
from pathlib import Path
from types import SimpleNamespace


def _os_errors(method):
    """Keep the port's error contract independent of the Win32 wrapper."""
    @wraps(method)
    def invoke(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            if isinstance(exc, self._api.types.error):
                raise OSError(exc.winerror, "Windows document file operation failed") from exc
            raise
    return invoke


class WindowsFileLock:
    def try_acquire(self, descriptor: int) -> bool:
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False
            raise
        return True

    def release(self, descriptor: int) -> None:
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)


class WindowsDocumentFiles:
    @cached_property
    def _api(self):
        import msvcrt
        import pywintypes
        import win32api
        import win32con
        import win32file
        import win32security

        return SimpleNamespace(crt=msvcrt, types=pywintypes, api=win32api,
                               con=win32con, file=win32file, security=win32security)

    @cached_property
    def _user(self):
        api = self._api
        token = api.security.OpenProcessToken(api.api.GetCurrentProcess(), api.con.TOKEN_QUERY)
        try:
            return api.security.GetTokenInformation(token, api.security.TokenUser)[0]
        finally:
            token.Close()

    def _open(self, path, *, write_acl=False):
        api = self._api
        # OPEN_REPARSE_POINT + BACKUP_SEMANTICS: inspect the named object itself,
        # including directories and junctions. Never silently follow a link.
        return api.file.CreateFile(str(path), api.con.READ_CONTROL | (api.con.WRITE_DAC if write_acl else 0),
            api.con.FILE_SHARE_READ | api.con.FILE_SHARE_WRITE | api.con.FILE_SHARE_DELETE,
            None, api.con.OPEN_EXISTING, 0x00200000 | 0x02000000, None)

    @_os_errors
    def open_read(self, path: Path, *, private=False) -> int:
        api = self._api
        handle = api.file.CreateFile(str(path), api.con.GENERIC_READ,
            api.con.FILE_SHARE_READ | api.con.FILE_SHARE_WRITE | api.con.FILE_SHARE_DELETE,
            None, api.con.OPEN_EXISTING, 0x00200000, None)
        try:
            self._identity(handle)
            if private:
                self._check_private(handle)
        except BaseException:
            handle.Close()
            raise
        raw = handle.Detach()
        try:
            return api.crt.open_osfhandle(raw, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            api.api.CloseHandle(raw)
            raise

    @contextmanager
    def _opened(self, path, *, write_acl=False):
        handle = self._open(path, write_acl=write_acl)
        try:
            yield handle
        finally:
            handle.Close()

    def _security(self, handle):
        security = self._api.security
        result = security.GetSecurityInfo(handle, security.SE_FILE_OBJECT,
            security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION)
        if result.GetSecurityDescriptorOwner() != self._user:
            raise PermissionError("managed Windows object has a different owner")
        return result

    def _check_kind(self, handle, *, directory=False):
        info = self._api.file.GetFileInformationByHandle(handle)
        if info[0] & 0x400 or bool(info[0] & 0x10) != directory:
            raise PermissionError("managed Windows object is a reparse point or has the wrong type")
        if not directory and info[7] != 1:
            raise PermissionError("managed Windows file has multiple links")
        return info

    def _identity(self, handle):
        info = self._check_kind(handle)
        self._security(handle)
        return (info[4], info[8], info[9], str(self._user))

    @_os_errors
    def capture_descriptor(self, descriptor: int) -> object:
        return self._identity(self._api.crt.get_osfhandle(descriptor))

    def path_matches_no_follow(self, path: Path, identity: object) -> bool:
        try:
            with self._opened(path) as handle:
                return self._identity(handle) == identity
        except (OSError, self._api.types.error):
            return False

    @staticmethod
    def same(left: object, right: object) -> bool:
        return isinstance(left, tuple) and len(left) == 4 and left == right

    def _private_acl(self, *, directory=False):
        api = self._api
        acl = api.security.ACL()
        flags = 3 if directory else 0  # OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
        for sid in (self._user, api.security.CreateWellKnownSid(api.security.WinLocalSystemSid),
                    api.security.CreateWellKnownSid(api.security.WinBuiltinAdministratorsSid)):
            acl.AddAccessAllowedAceEx(api.security.ACL_REVISION, flags, 0x1F01FF, sid)
        return acl

    def _set_private(self, handle, *, directory=False):
        api = self._api
        self._check_kind(handle, directory=directory)
        self._security(handle)
        api.security.SetSecurityInfo(handle, api.security.SE_FILE_OBJECT,
            api.security.DACL_SECURITY_INFORMATION | api.security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, self._private_acl(directory=directory), None)
        self._check_private(handle)

    def _check_private(self, handle):
        api = self._api
        acl = self._security(handle).GetSecurityDescriptorDacl()
        if acl is None:
            raise PermissionError("private Windows object has no access restrictions")
        allowed = {str(self._user), str(api.security.CreateWellKnownSid(api.security.WinLocalSystemSid)),
                   str(api.security.CreateWellKnownSid(api.security.WinBuiltinAdministratorsSid))}
        for index in range(acl.GetAceCount()):
            ace = acl.GetAce(index)
            kind, flags = ace[0]
            if flags & 8 or kind == 1:  # INHERIT_ONLY_ACE / ACCESS_DENIED_ACE
                continue
            if kind != 0 or (ace[1] and str(ace[2]) not in allowed):
                raise PermissionError("private Windows object grants access to another principal")

    @_os_errors
    def validate_private(self, path: Path, *, directory=False) -> None:
        with self._opened(path) as handle:
            self._check_kind(handle, directory=directory)
            self._check_private(handle)

    def is_private(self, path: Path) -> bool:
        try:
            self.validate_private(path)
            return True
        except (OSError, self._api.types.error):
            return False

    @_os_errors
    def prepare_directory(self, path: Path) -> None:
        with self._opened(path, write_acl=True) as handle:
            self._set_private(handle, directory=True)

    @_os_errors
    def harden_descriptor(self, descriptor: int, mode: int) -> None:
        del mode  # POSIX permission bits are represented by the private DACL.
        api = self._api
        original = api.crt.get_osfhandle(descriptor)
        identity = self._identity(original)
        # CRT-opened handles need not include WRITE_DAC. Reopen the exact object
        # without following a reparse point and compare its identity first.
        path = api.file.GetFinalPathNameByHandle(original, 0)
        with self._opened(path, write_acl=True) as handle:
            if self._identity(handle) != identity:
                raise PermissionError("managed Windows file changed during permission update")
            self._set_private(handle)

    @_os_errors
    def create_temporary(self, parent: Path, prefix: str) -> tuple[int, str]:
        api = self._api
        attributes = api.types.SECURITY_ATTRIBUTES()
        descriptor = api.security.SECURITY_DESCRIPTOR()
        descriptor.SetSecurityDescriptorDacl(True, self._private_acl(), False)
        descriptor.SetSecurityDescriptorControl(0x1000, 0x1000)  # SE_DACL_PROTECTED
        attributes.SECURITY_DESCRIPTOR = descriptor
        for _ in range(32):
            path = parent / (prefix + secrets.token_hex(12))
            try:
                handle = api.file.CreateFile(str(path), api.con.GENERIC_READ | api.con.GENERIC_WRITE,
                    api.con.FILE_SHARE_READ | api.con.FILE_SHARE_WRITE | api.con.FILE_SHARE_DELETE,
                    attributes, api.con.CREATE_NEW, api.con.FILE_ATTRIBUTE_NORMAL, None)
            except api.types.error as exc:
                if exc.winerror in (80, 183):
                    continue
                raise
            raw = handle.Detach()
            try:
                fd = api.crt.open_osfhandle(raw, os.O_RDWR | os.O_BINARY)
            except BaseException:
                api.api.CloseHandle(raw)
                raise
            return fd, str(path)
        raise FileExistsError("could not allocate private transaction file")

    @_os_errors
    def sync_directory(self, path: Path) -> bool:
        with self._opened(path) as handle:
            self._check_kind(handle, directory=True)
        # Files are flushed before replace. NTFS directory persistence is not
        # confirmed by the POSIX fsync contract; expose that fact to the caller.
        return False
