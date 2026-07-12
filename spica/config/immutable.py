"""Shared immutable-tree helpers for resolved and authored configuration."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


def freeze_config_tree(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: freeze_config_tree(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(freeze_config_tree(item) for item in value)
    return value


def thaw_config_tree(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw_config_tree(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_config_tree(item) for item in value]
    return value


__all__ = ["freeze_config_tree", "thaw_config_tree"]
