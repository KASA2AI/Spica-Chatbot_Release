"""Host detection and cross-process locking for Config Studio."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from spica.ports.config_studio_platform import PlatformCapabilities


class _UnavailableFileLock:
    def try_acquire(self, descriptor: int) -> bool:
        del descriptor
        raise RuntimeError("managed document locking is unavailable")

    def release(self, descriptor: int) -> None:
        del descriptor
        raise RuntimeError("managed document locking is unavailable")


class _FcntlFileLock:
    """POSIX flock adapter, imported lazily so this module imports on Windows."""

    def try_acquire(self, descriptor: int) -> bool:
        import fcntl

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def release(self, descriptor: int) -> None:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


@dataclass(frozen=True, slots=True)
class _PosixFileIdentity:
    device: int = field(repr=False)
    inode: int = field(repr=False)
    owner_id: int = field(repr=False)


class _UnavailableStableFileIdentity:
    def capture_descriptor(self, descriptor: int) -> object:
        del descriptor
        raise RuntimeError("stable file identity is unavailable")

    def path_matches_no_follow(self, path: Path, identity: object) -> bool:
        del path, identity
        raise RuntimeError("stable file identity is unavailable")

    def same(self, left: object, right: object) -> bool:
        del left, right
        raise RuntimeError("stable file identity is unavailable")


class _PosixStableFileIdentity:
    def __init__(self, owner_id: int) -> None:
        self._owner_id = owner_id

    def capture_descriptor(self, descriptor: int) -> object:
        file_stat = os.fstat(descriptor)
        return _PosixFileIdentity(
            file_stat.st_dev,
            file_stat.st_ino,
            file_stat.st_uid,
        )

    def path_matches_no_follow(self, path: Path, identity: object) -> bool:
        if not isinstance(identity, _PosixFileIdentity):
            return False
        try:
            path_stat = path.lstat()
        except OSError:
            return False
        return (
            stat.S_ISREG(path_stat.st_mode)
            and path_stat.st_nlink == 1
            and identity.owner_id == self._owner_id
            and (
                path_stat.st_dev,
                path_stat.st_ino,
                path_stat.st_uid,
            )
            == (identity.device, identity.inode, identity.owner_id)
        )

    def same(self, left: object, right: object) -> bool:
        return (
            isinstance(left, _PosixFileIdentity)
            and isinstance(right, _PosixFileIdentity)
            and left == right
        )


def platform_capabilities_for(
    *,
    os_family: str,
    runtime_name: str,
    user_id: int | None,
    temp_directory: str | Path,
) -> PlatformCapabilities:
    """Build explicit capabilities; only the verified Linux lane may write."""

    if not isinstance(os_family, str) or not isinstance(runtime_name, str):
        raise TypeError("platform names must be strings")
    valid_posix_user = (
        os_family == "posix"
        and isinstance(user_id, int)
        and not isinstance(user_id, bool)
        and user_id >= 0
    )
    verified_linux = valid_posix_user and runtime_name == "linux"
    return PlatformCapabilities(
        os_family=os_family,
        runtime_name=runtime_name,
        user_id=user_id if valid_posix_user else None,
        temp_directory=Path(temp_directory),
        file_lock=_FcntlFileLock() if verified_linux else _UnavailableFileLock(),
        file_identity=(
            _PosixStableFileIdentity(user_id)
            if verified_linux
            else _UnavailableStableFileIdentity()
        ),
        posix_permissions=valid_posix_user,
        managed_document_writes=verified_linux,
        sensitive_document_writes=verified_linux,
        self_check_containment=verified_linux,
    )


@lru_cache(maxsize=1)
def current_platform_capabilities() -> PlatformCapabilities:
    """Detect the process platform at an outer composition boundary."""

    os_family = os.name
    user_id = os.getuid() if os_family == "posix" else None
    return platform_capabilities_for(
        os_family=os_family,
        runtime_name=sys.platform,
        user_id=user_id,
        temp_directory=tempfile.gettempdir(),
    )


def linux_self_check_base_environment(
    platform: PlatformCapabilities,
) -> dict[str, str]:
    """Build the fixed child-process base for the verified Linux adapter."""

    import pwd

    if not platform.self_check_containment or platform.user_id is None:
        raise ValueError("self-check platform containment is unavailable")
    home = pwd.getpwuid(platform.user_id).pw_dir
    if not isinstance(home, str) or not Path(home).is_absolute():
        raise ValueError("self-check account home is unavailable")
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": home,
        "TMPDIR": "/tmp",
        "TMP": "/tmp",
        "TEMP": "/tmp",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }


__all__ = [
    "current_platform_capabilities",
    "linux_self_check_base_environment",
    "platform_capabilities_for",
]
