"""Platform capability values consumed by Config Studio owners.

This port is deliberately free of host detection and operating-system APIs.
Concrete file locking and capability detection live under ``spica.adapters``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class CrossProcessFileLockPort(Protocol):
    """Non-blocking stable lock over an already-open lock file."""

    def try_acquire(self, descriptor: int) -> bool:
        ...

    def release(self, descriptor: int) -> None:
        ...


@dataclass(frozen=True, slots=True)
class PlatformCapabilities:
    """Immutable, injectable decisions for privileged local operations."""

    os_family: str
    runtime_name: str
    user_id: int | None = field(repr=False)
    temp_directory: Path = field(repr=False)
    file_lock: CrossProcessFileLockPort = field(repr=False, compare=False)
    posix_permissions: bool
    managed_document_writes: bool
    sensitive_document_writes: bool
    self_check_containment: bool

    @property
    def default_lock_root(self) -> Path:
        if self.posix_permissions and self.user_id is not None:
            return self.temp_directory / f"spica-config-studio-locks-{self.user_id}"
        return self.temp_directory / "spica-config-studio-locks"


__all__ = ["CrossProcessFileLockPort", "PlatformCapabilities"]
