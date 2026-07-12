"""Explicit, immutable configuration environment snapshots.

The snapshot is deliberately constructed from caller-provided mappings.  It
never reads process globals, which lets configuration previews model a changed
dotenv document without inheriting values previously primed into ``os.environ``.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from spica.config.env_roster import (
    APP_ENV_MAP,
    RESPEAKER_ENV_MAP,
    RUNTIME_CACHE_ENV_MAP,
    SCREEN_ENV_MAP,
    SECRETS_ENV_MAP,
)


_ALLOWED_ENV_NAMES = frozenset(
    value
    for mapping in (
        APP_ENV_MAP,
        SCREEN_ENV_MAP,
        RUNTIME_CACHE_ENV_MAP,
        RESPEAKER_ENV_MAP,
    )
    for value in mapping.values()
)
_SECRET_ENV_NAMES = frozenset(SECRETS_ENV_MAP.values()) | {"DEEPSEEK_API_KEY"}


@dataclass(frozen=True)
class EnvironmentValue:
    value: str
    layer: str


class EnvironmentSnapshot:
    """A non-mutating view of configuration override values and provenance."""

    __slots__ = ("_values", "_tainted")

    def __init__(
        self,
        values: Mapping[str, EnvironmentValue],
        tainted: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(values, Mapping):
            raise TypeError("environment values must be a mapping")
        normalized_values: dict[str, EnvironmentValue] = {}
        for name, item in values.items():
            if not isinstance(name, str) or not name:
                raise TypeError("environment names must be non-empty strings")
            if name in _SECRET_ENV_NAMES:
                raise ValueError("secret environment variable is not allowed")
            if name not in _ALLOWED_ENV_NAMES:
                raise ValueError("unsupported configuration environment variable")
            if not isinstance(item, EnvironmentValue):
                raise TypeError("environment entries must be EnvironmentValue objects")
            if not isinstance(item.value, str):
                raise TypeError("environment values must be strings")
            if not isinstance(item.layer, str) or not item.layer:
                raise ValueError("environment layers must be named")
            normalized_values[name] = item
        tainted_values = dict(tainted or {})
        if set(normalized_values) & set(tainted_values):
            raise ValueError("tainted environment names cannot carry values")
        if set(tainted_values) - _ALLOWED_ENV_NAMES:
            raise ValueError("unsupported tainted environment variable")
        if any(not isinstance(layer, str) or not layer for layer in tainted_values.values()):
            raise ValueError("tainted environment layers must be named")
        self._values = MappingProxyType(normalized_values)
        self._tainted = MappingProxyType(tainted_values)

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, str],
        *,
        layer: str,
    ) -> "EnvironmentSnapshot":
        if not isinstance(layer, str) or not layer:
            raise ValueError("environment layer must be named")
        if any(
            not isinstance(name, str)
            or not name
            or not isinstance(value, str)
            for name, value in values.items()
        ):
            raise TypeError("environment names and values must be strings")
        secret_names = sorted(set(values) & _SECRET_ENV_NAMES)
        if secret_names:
            raise ValueError(
                "secret environment variable is not allowed in EnvironmentSnapshot"
            )
        unsupported_names = sorted(set(values) - _ALLOWED_ENV_NAMES)
        if unsupported_names:
            raise ValueError(
                f"unsupported configuration environment variable: {unsupported_names[0]}"
            )
        return cls(
            {
                name: EnvironmentValue(value=value, layer=layer)
                for name, value in values.items()
            }
        )

    @classmethod
    def from_layers(
        cls,
        *,
        inherited: Mapping[str, str],
        repo_dotenv: Mapping[str, str],
        parent_dotenv: Mapping[str, str],
        tainted: Mapping[str, str] | None = None,
    ) -> "EnvironmentSnapshot":
        """Apply the production ``override=False`` precedence explicitly."""
        merged: dict[str, EnvironmentValue] = {}
        for layer, values in (
            ("parent_dotenv", parent_dotenv),
            ("repo_dotenv", repo_dotenv),
            ("inherited", inherited),
        ):
            snapshot = cls.from_mapping(values, layer=layer)
            merged.update(snapshot._values)
        return cls(merged, tainted=tainted)

    def get(self, name: str) -> str | None:
        item = self._values.get(name)
        return item.value if item is not None else None

    def layer_for(self, name: str) -> str | None:
        item = self._values.get(name)
        return item.layer if item is not None else self._tainted.get(name)

    def is_tainted(self, name: str) -> bool:
        return name in self._tainted

    @property
    def tainted_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tainted))

    def quarantine(self, tainted: Mapping[str, str]) -> "EnvironmentSnapshot":
        """Return a snapshot where named winners retain source but expose no value."""

        requested = dict(tainted)
        merged_taint = dict(self._tainted)
        merged_taint.update(requested)
        return EnvironmentSnapshot(
            {
                name: item
                for name, item in self._values.items()
                if name not in requested
            },
            tainted=merged_taint,
        )

    def __repr__(self) -> str:
        return (
            "EnvironmentSnapshot("
            f"<{len(self._values)} non-sensitive values; "
            f"{len(self._tainted)} quarantined>)"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, EnvironmentSnapshot):
            return NotImplemented
        return self._values == other._values and self._tainted == other._tainted
