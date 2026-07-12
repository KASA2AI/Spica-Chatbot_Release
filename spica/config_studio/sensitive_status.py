"""Read-only health projection for the fixed repository secret document."""

from __future__ import annotations

import io
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv.parser import parse_stream

from spica.config.env_roster import (
    APP_ENV_MAP,
    LEGACY_ENV_VARS,
    SCREEN_ENV_MAP,
    SECRETS_ENV_MAP,
)
from spica.ports.config_studio_platform import PlatformCapabilities
from spica.config.secrets import Secrets
from spica.config_studio.managed_catalog import read_fixed_regular_file


@dataclass(frozen=True, slots=True)
class ManagedOverrideReadStatus:
    environment_variable: str
    affected_fields: tuple[str, ...]
    repo_defined: bool

    def to_wire(self) -> dict[str, Any]:
        return {
            "environment_variable": self.environment_variable,
            "affected_fields": list(self.affected_fields),
            "repo_defined": self.repo_defined,
        }


@dataclass(frozen=True, slots=True)
class SensitiveEnvReadStatus:
    permission_health: str
    parse_health: str
    secret_slots: tuple[tuple[str, bool], ...]
    legacy_entries: tuple[str, ...]
    managed_overrides: tuple[ManagedOverrideReadStatus, ...]

    def to_wire(self) -> dict[str, Any]:
        return {
            "permission_health": self.permission_health,
            "parse_health": self.parse_health,
            "secret_slots": dict(self.secret_slots),
            "legacy_entries": list(self.legacy_entries),
            "managed_overrides": [
                item.to_wire() for item in self.managed_overrides
            ],
        }


@dataclass(frozen=True, slots=True)
class ReadOnlyEnvStatus:
    permission_health: str
    parse_health: str
    legacy_entries: tuple[str, ...]
    defined_names: tuple[str, ...] = field(default=(), repr=False)

    def to_wire(self) -> dict[str, Any]:
        return {
            "permission_health": self.permission_health,
            "parse_health": self.parse_health,
            "legacy_entries": list(self.legacy_entries),
        }


def inspect_sensitive_env_status(
    repo_root: str | Path,
    secrets: Secrets,
    *,
    platform_capabilities: PlatformCapabilities,
) -> SensitiveEnvReadStatus:
    """Inspect fixed metadata without retaining or returning dotenv values."""

    if not isinstance(secrets, Secrets):
        raise TypeError("secrets must be a Secrets value")
    if not isinstance(platform_capabilities, PlatformCapabilities):
        raise TypeError("platform_capabilities must be PlatformCapabilities")
    path = Path(os.path.abspath(Path(repo_root))) / "xiaosan.env"
    document = _inspect_env_document(path, platform_capabilities)
    slots = tuple(
        (slot, bool(getattr(secrets, slot)))
        for slot in SECRETS_ENV_MAP
    )
    return SensitiveEnvReadStatus(
        document.permission_health,
        document.parse_health,
        slots,
        document.legacy_entries,
        tuple(
            ManagedOverrideReadStatus(
                environment_variable=environment_variable,
                affected_fields=(field_path,),
                repo_defined=environment_variable in document.defined_names,
            )
            for field_path, environment_variable in (
                *APP_ENV_MAP.items(),
                *((f"screen.{name}", value) for name, value in SCREEN_ENV_MAP.items()),
            )
        ),
    )


def inspect_readonly_env_status(
    path: str | Path,
    *,
    platform_capabilities: PlatformCapabilities,
) -> ReadOnlyEnvStatus:
    """Project health for a fixed read-only dotenv without values or slots."""

    if not isinstance(platform_capabilities, PlatformCapabilities):
        raise TypeError("platform_capabilities must be PlatformCapabilities")
    return _inspect_env_document(
        Path(os.path.abspath(Path(path))),
        platform_capabilities,
    )


def _inspect_env_document(
    path: Path,
    platform: PlatformCapabilities,
) -> ReadOnlyEnvStatus:
    read = read_fixed_regular_file(
        path,
        platform_capabilities=platform,
    )
    if read.status == "missing":
        return ReadOnlyEnvStatus("MISSING", "MISSING", ())
    if read.content is None:
        permission_health = _permission_health(path, platform)
        return ReadOnlyEnvStatus(permission_health, "UNAVAILABLE", ())

    permission_health = _permission_health(path, platform)
    if permission_health in {"DOCUMENT_UNSAFE", "WRONG_OWNER", "MULTIPLE_LINKS"}:
        return ReadOnlyEnvStatus(permission_health, "UNAVAILABLE", ())
    try:
        text = read.content.decode("utf-8")
    except UnicodeError:
        return ReadOnlyEnvStatus(permission_health, "INVALID", ())
    legacy: set[str] = set()
    defined: set[str] = set()
    parse_valid = True
    for binding in parse_stream(io.StringIO(text)):
        if binding.error:
            parse_valid = False
        if binding.key in LEGACY_ENV_VARS:
            legacy.add(binding.key)
        if binding.key is not None:
            defined.add(binding.key)
    return ReadOnlyEnvStatus(
        permission_health,
        "VALID" if parse_valid else "INVALID",
        tuple(sorted(legacy)),
        tuple(sorted(defined)),
    )


def _permission_health(path: Path, platform: PlatformCapabilities) -> str:
    try:
        info = path.lstat()
    except OSError:
        return "DOCUMENT_UNSAFE"
    if not stat.S_ISREG(info.st_mode):
        return "DOCUMENT_UNSAFE"
    if not platform.posix_permissions:
        return "DACL_UNVERIFIED"
    if info.st_uid != platform.user_id:
        return "WRONG_OWNER"
    if info.st_nlink != 1:
        return "MULTIPLE_LINKS"
    return "PRIVATE" if stat.S_IMODE(info.st_mode) == 0o600 else "TOO_PERMISSIVE"


__all__ = [
    "ManagedOverrideReadStatus",
    "ReadOnlyEnvStatus",
    "SensitiveEnvReadStatus",
    "inspect_readonly_env_status",
    "inspect_sensitive_env_status",
]
