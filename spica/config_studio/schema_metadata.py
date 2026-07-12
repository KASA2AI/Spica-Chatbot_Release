"""Read Config Studio presentation policy from the production AppConfig schema."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from pydantic import BaseModel

from spica.config.schema import AppConfig


EXTERNAL_PATH_MARKER = "<external-path>"


def redact_external_schema_path(
    field_names: tuple[str, ...],
    value: Any,
) -> Any:
    """Hide absolute paths only when their AppConfig field declares path semantics."""

    if (
        isinstance(value, str)
        and _is_schema_path(field_names)
        and _is_absolute_path(value)
    ):
        return EXTERNAL_PATH_MARKER
    return value


def _is_schema_path(field_names: tuple[str, ...]) -> bool:
    model_type: type[BaseModel] = AppConfig
    field_info: Any = None
    for index, field_name in enumerate(field_names):
        field_info = model_type.model_fields.get(field_name)
        if field_info is None:
            return False
        if index == len(field_names) - 1:
            break
        nested = field_info.annotation
        if not isinstance(nested, type) or not issubclass(nested, BaseModel):
            return False
        model_type = nested
    if field_info is None:
        return False
    extra = field_info.json_schema_extra
    return isinstance(extra, Mapping) and isinstance(
        extra.get("path_semantics"),
        Mapping,
    )


def _is_absolute_path(value: str) -> bool:
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


__all__ = ["EXTERNAL_PATH_MARKER", "redact_external_schema_path"]
