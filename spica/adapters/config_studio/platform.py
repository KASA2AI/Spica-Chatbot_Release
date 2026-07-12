"""Host detection and cross-process locking for Config Studio."""

from __future__ import annotations

import os
import sys
import tempfile
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
    verified_linux = valid_posix_user and runtime_name.startswith("linux")
    return PlatformCapabilities(
        os_family=os_family,
        runtime_name=runtime_name,
        user_id=user_id if valid_posix_user else None,
        temp_directory=Path(temp_directory),
        file_lock=_FcntlFileLock() if verified_linux else _UnavailableFileLock(),
        posix_permissions=valid_posix_user,
        managed_document_writes=verified_linux,
        sensitive_document_writes=verified_linux,
        self_check_containment=verified_linux,
    )


@lru_cache(maxsize=1)
def current_platform_capabilities() -> PlatformCapabilities:
    """Detect the process platform at an outer composition boundary."""

    user_id = os.getuid() if os.name == "posix" else None
    return platform_capabilities_for(
        os_family=os.name,
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
