"""Strict authoring validation layered in front of production config owners."""

from __future__ import annotations

import copy
import math
import os
import re
import stat
from collections.abc import Mapping as ABCMapping
from dataclasses import dataclass, field
from types import UnionType
from pathlib import Path
from typing import Any, Literal, Mapping, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from spica.config.environment_snapshot import EnvironmentSnapshot
from spica.config.immutable import freeze_config_tree, thaw_config_tree
from spica.config.manager import ConfigManager, ConfigResolution
from spica.config.schema import AppConfig
from spica.config_studio.paths import (
    ConfigFieldPath,
    FieldSegment,
    ListIndexSegment,
    MapKeySegment,
    PathSegment,
)
from spica.config_studio.yaml_owner import YamlOwnerError, reject_yaml_alias_graph


_READ_ONLY_PATHS = {
    (FieldSegment("character"), FieldSegment("character_id")),
    (FieldSegment("character"), FieldSegment("character_profile")),
    (FieldSegment("character"), FieldSegment("character_name")),
}


class AuthoringError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SetValue:
    path: ConfigFieldPath
    value: Any = field(repr=False)


@dataclass(frozen=True)
class UnsetValue:
    path: ConfigFieldPath


AuthoringOperation = SetValue | UnsetValue


@dataclass(frozen=True, repr=False)
class AuthoringValidation:
    resolution: ConfigResolution = field(repr=False)
    _candidate: Any = field(repr=False)

    def to_app_config(self) -> AppConfig:
        return self.resolution.to_app_config()

    def candidate_document(self) -> dict[str, Any]:
        return thaw_config_tree(self._candidate)

    def __repr__(self) -> str:
        return "AuthoringValidation(<validated>)"


class ConfigAuthoringValidator:
    def __init__(
        self,
        *,
        manager: ConfigManager | None = None,
        environment_snapshot: EnvironmentSnapshot | None = None,
        model_type: type[BaseModel] = AppConfig,
        plugin_root: str | Path | None = None,
    ) -> None:
        self._manager = manager or ConfigManager()
        self._environment_snapshot = environment_snapshot or EnvironmentSnapshot.from_mapping(
            {}, layer="inherited"
        )
        self._model_type = model_type
        self._plugin_root = (
            Path(os.path.abspath(plugin_root)) if plugin_root is not None else None
        )

    def validate(
        self,
        base_document: Mapping[str, Any],
        candidate_document: Mapping[str, Any],
        operations: tuple[AuthoringOperation, ...],
    ) -> AuthoringValidation:
        if not isinstance(base_document, Mapping) or not isinstance(
            candidate_document, Mapping
        ):
            raise AuthoringError("DOCUMENT_INVALID", "documents must be mappings")
        try:
            reject_yaml_alias_graph(base_document)
            reject_yaml_alias_graph(candidate_document)
        except YamlOwnerError as exc:
            code = (
                "DOCUMENT_ALIAS_UNSUPPORTED"
                if exc.code == "YAML_ALIAS_UNSUPPORTED"
                else "DOCUMENT_INVALID"
            )
            raise AuthoringError(code, "document graph is unsafe for authoring") from exc

        expected = copy.deepcopy(dict(base_document))
        plugins_touched = False
        for operation in operations:
            if not isinstance(operation, (SetValue, UnsetValue)):
                raise AuthoringError("OPERATION_INVALID", "unsupported operation")
            segments, annotation, metadata = self._field_annotation(operation.path)
            names = operation.path.plain_values()
            if operation.path.segments and operation.path.segments[0] == FieldSegment(
                "plugins"
            ):
                plugins_touched = True
            if isinstance(operation, SetValue):
                self._validate_strict_value(annotation, operation.value)
                _validate_schema_constraints(metadata, operation.value)
                _validate_owner_constraints(names, operation.value)
                if names == ("plugins",):
                    _validate_plugins(operation.value)
                _set_nested(expected, segments, copy.deepcopy(operation.value))
            else:
                _unset_nested(expected, segments)

        candidate = copy.deepcopy(dict(candidate_document))
        base_unknown = _unknown_values(dict(base_document), self._model_type)
        candidate_unknown = _unknown_values(candidate, self._model_type)
        if base_unknown != candidate_unknown:
            raise AuthoringError(
                "UNKNOWN_FIELD",
                "unknown fields must be preserved exactly and cannot be introduced",
            )
        if not _strict_equal(expected, candidate):
            raise AuthoringError(
                "UNDECLARED_CHANGE",
                "candidate document does not match declared operations",
            )
        if plugins_touched:
            plugins = candidate.get("plugins", [])
            _validate_plugins(plugins)
            if self._plugin_root is not None:
                _validate_plugin_packages(plugins, self._plugin_root)

        try:
            resolution = self._manager.resolve_snapshot(
                candidate,
                self._environment_snapshot,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise AuthoringError("DOCUMENT_INVALID", "candidate validation failed") from exc
        return AuthoringValidation(
            resolution=resolution,
            _candidate=freeze_config_tree(candidate),
        )

    def _field_annotation(
        self,
        path: ConfigFieldPath,
    ) -> tuple[tuple[PathSegment, ...], Any, tuple[Any, ...]]:
        if path.segments and any(
            read_only[: len(path.segments)] == path.segments
            for read_only in _READ_ONLY_PATHS
        ):
            raise AuthoringError("READ_ONLY_FIELD", "field is resolved at runtime")
        if path.segments == (FieldSegment("song"), MapKeySegment("enabled")):
            return path.segments, bool, ()
        if path.segments and path.segments[0] == FieldSegment("song"):
            raise AuthoringError(
                "READ_ONLY_FIELD", "song fields require a canonical owner schema"
            )
        if not path.segments:
            raise AuthoringError("PATH_INVALID", "path must not be empty")
        annotation: Any = self._model_type
        metadata: tuple[Any, ...] = ()
        for segment in path.segments:
            if isinstance(segment, FieldSegment):
                model_type = _nested_model_type(annotation)
                if model_type is None:
                    raise AuthoringError("PATH_INVALID", "field segment crosses a leaf")
                field_info = model_type.model_fields.get(segment.name)
                if field_info is None:
                    raise AuthoringError("UNKNOWN_FIELD", "path is not owned by the schema")
                annotation = field_info.annotation
                metadata = tuple(field_info.metadata)
            elif isinstance(segment, MapKeySegment):
                concrete = _strip_optional(annotation)
                origin = get_origin(concrete)
                if origin not in (dict, Mapping, ABCMapping):
                    raise AuthoringError("PATH_INVALID", "map key crosses a non-map field")
                annotation = get_args(concrete)[1]
                metadata = ()
            elif isinstance(segment, ListIndexSegment):
                concrete = _strip_optional(annotation)
                origin = get_origin(concrete)
                if origin not in (list, tuple):
                    raise AuthoringError("PATH_INVALID", "list index crosses a non-list field")
                annotation = get_args(concrete)[0]
                metadata = ()
            else:
                raise AuthoringError("PATH_INVALID", "unsupported typed path segment")
        if _nested_model_type(annotation) is not None:
            raise AuthoringError(
                "PATH_INVALID",
                "nested models must be authored through owned leaf paths",
            )
        return path.segments, annotation, metadata

    @staticmethod
    def _validate_strict_value(annotation: Any, value: Any) -> None:
        if value is None and type(None) in _union_options(annotation):
            return
        concrete = _strip_optional(annotation)
        origin = get_origin(concrete)
        if origin is Literal:
            choices = get_args(concrete)
            if not any(type(value) is type(choice) and value == choice for choice in choices):
                raise AuthoringError("TYPE_MISMATCH", "value is not a literal choice")
            return
        nested = _nested_model_type(concrete)
        if nested is not None:
            if not isinstance(value, dict):
                raise AuthoringError("TYPE_MISMATCH", "model value must be an object")
            for name, item in value.items():
                field_info = nested.model_fields.get(name)
                if field_info is not None:
                    ConfigAuthoringValidator._validate_strict_value(
                        field_info.annotation,
                        item,
                    )
            return
        arguments = get_args(concrete)
        if origin in (list, tuple):
            if not isinstance(value, list):
                raise AuthoringError("TYPE_MISMATCH", "list value must be a JSON array")
            item_annotation = arguments[0] if arguments else Any
            for item in value:
                ConfigAuthoringValidator._validate_strict_value(
                    item_annotation,
                    item,
                )
            return
        if origin in (dict, Mapping, ABCMapping):
            if not isinstance(value, dict):
                raise AuthoringError("TYPE_MISMATCH", "map value must be a JSON object")
            key_annotation, value_annotation = (
                arguments if len(arguments) == 2 else (Any, Any)
            )
            for key, item in value.items():
                ConfigAuthoringValidator._validate_strict_value(key_annotation, key)
                ConfigAuthoringValidator._validate_strict_value(value_annotation, item)
            return
        options = _concrete_types(concrete)
        if Any in options:
            return
        if bool in options and type(value) is not bool:
            raise AuthoringError("TYPE_MISMATCH", "boolean value must be a JSON boolean")
        if int in options and float not in options and type(value) is not int:
            raise AuthoringError("TYPE_MISMATCH", "integer value must be a JSON integer")
        if float in options:
            if type(value) not in (int, float):
                raise AuthoringError("TYPE_MISMATCH", "number must be a JSON number")
            if not math.isfinite(value):
                raise AuthoringError("VALUE_OUT_OF_RANGE", "number must be finite")
        if str in options and type(value) is not str:
            raise AuthoringError("TYPE_MISMATCH", "string value must be a JSON string")


def _nested_model_type(annotation: Any) -> type[BaseModel] | None:
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        for option in get_args(annotation):
            nested = _nested_model_type(option)
            if nested is not None:
                return nested
        return None
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _concrete_types(annotation: Any) -> set[Any]:
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        return {option for option in get_args(annotation) if option is not type(None)}
    return {annotation}


def _union_options(annotation: Any) -> set[Any]:
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        return set(get_args(annotation))
    return {annotation}


def _set_nested(
    document: dict[str, Any],
    segments: tuple[PathSegment, ...],
    value: Any,
) -> None:
    current: Any = document
    for index, segment in enumerate(segments[:-1]):
        next_segment = segments[index + 1]
        if isinstance(segment, (FieldSegment, MapKeySegment)):
            if not isinstance(current, dict):
                raise AuthoringError("PATH_INVALID", "path crosses a non-mapping value")
            key = segment.name if isinstance(segment, FieldSegment) else segment.key
            child = current.get(key)
            if child is None:
                child = [] if isinstance(next_segment, ListIndexSegment) else {}
                current[key] = child
            current = child
        elif isinstance(segment, ListIndexSegment):
            if not isinstance(current, list) or segment.index >= len(current):
                raise AuthoringError("PATH_INVALID", "list index is outside the document")
            current = current[segment.index]
    final = segments[-1]
    if isinstance(final, (FieldSegment, MapKeySegment)):
        if not isinstance(current, dict):
            raise AuthoringError("PATH_INVALID", "path crosses a non-mapping value")
        key = final.name if isinstance(final, FieldSegment) else final.key
        current[key] = value
    elif isinstance(final, ListIndexSegment):
        if not isinstance(current, list) or final.index >= len(current):
            raise AuthoringError("PATH_INVALID", "list index is outside the document")
        current[final.index] = value


def _unset_nested(
    document: dict[str, Any],
    segments: tuple[PathSegment, ...],
) -> None:
    if not segments:
        raise AuthoringError("PATH_INVALID", "path must not be empty")
    current: Any = document
    for segment in segments[:-1]:
        if isinstance(segment, (FieldSegment, MapKeySegment)):
            if not isinstance(current, dict):
                raise AuthoringError("PATH_INVALID", "path crosses a non-mapping value")
            key = segment.name if isinstance(segment, FieldSegment) else segment.key
            if key not in current:
                return
            current = current[key]
        elif isinstance(segment, ListIndexSegment):
            if not isinstance(current, list) or segment.index >= len(current):
                return
            current = current[segment.index]
    final = segments[-1]
    if isinstance(final, (FieldSegment, MapKeySegment)):
        if not isinstance(current, dict):
            raise AuthoringError("PATH_INVALID", "path crosses a non-mapping value")
        key = final.name if isinstance(final, FieldSegment) else final.key
        current.pop(key, None)
    elif isinstance(final, ListIndexSegment):
        if not isinstance(current, list):
            raise AuthoringError("PATH_INVALID", "path crosses a non-list value")
        if final.index < len(current):
            current.pop(final.index)


def _unknown_values(
    document: Mapping[str, Any],
    model_type: type[BaseModel],
    prefix: tuple[Any, ...] = (),
) -> dict[tuple[Any, ...], Any]:
    return _unknown_for_annotation(document, model_type, prefix)


def _unknown_for_annotation(
    value: Any,
    annotation: Any,
    prefix: tuple[Any, ...],
) -> dict[tuple[Any, ...], Any]:
    nested = _nested_model_type(annotation)
    if nested is not None:
        if not isinstance(value, Mapping):
            return {}
        unknown: dict[tuple[Any, ...], Any] = {}
        for key, item in value.items():
            field_info = nested.model_fields.get(str(key))
            path = prefix + (str(key),)
            if field_info is None:
                unknown[path] = copy.deepcopy(item)
            else:
                unknown.update(
                    _unknown_for_annotation(item, field_info.annotation, path)
                )
        return unknown

    concrete = _strip_optional(annotation)
    origin = get_origin(concrete)
    arguments = get_args(concrete)
    if origin in (dict, Mapping, ABCMapping) and isinstance(value, Mapping):
        value_annotation = arguments[1] if len(arguments) == 2 else Any
        unknown = {}
        for key, item in value.items():
            unknown.update(
                _unknown_for_annotation(
                    item,
                    value_annotation,
                    prefix + (str(key),),
                )
            )
        return unknown
    if origin in (list, tuple) and isinstance(value, (list, tuple)):
        item_annotation = arguments[0] if arguments else Any
        unknown = {}
        for index, item in enumerate(value):
            unknown.update(
                _unknown_for_annotation(
                    item,
                    item_annotation,
                    prefix + (index,),
                )
            )
        return unknown
    return {}


def _strip_optional(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        options = tuple(option for option in get_args(annotation) if option is not type(None))
        if len(options) == 1:
            return options[0]
    return annotation


def _validate_plugins(value: Any) -> None:
    if not isinstance(value, list):
        raise AuthoringError("TYPE_MISMATCH", "plugins must be a JSON array")
    names: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise AuthoringError("TYPE_MISMATCH", "plugin entry must be an object")
        if not set(item).issubset({"name", "enabled"}):
            raise AuthoringError(
                "UNKNOWN_FIELD", "plugin entry contains an unsupported field"
            )
        name = item.get("name")
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name) is None
        ):
            raise AuthoringError("PLUGIN_NAME_INVALID", "plugin name is invalid")
        enabled = item.get("enabled", True)
        if type(enabled) is not bool:
            raise AuthoringError("TYPE_MISMATCH", "plugin enabled must be a JSON boolean")
        if name in names:
            raise AuthoringError("PLUGIN_DUPLICATE", "plugin names must be unique")
        names.add(name)


def _validate_plugin_packages(value: Any, plugin_root: Path) -> None:
    try:
        _validate_directory_chain(plugin_root)
    except FileNotFoundError as exc:
        if any(item.get("enabled", True) for item in value):
            raise AuthoringError(
                "PLUGIN_PACKAGE_MISSING", "plugin package root is missing"
            ) from exc
        return
    for item in value:
        if not item.get("enabled", True):
            continue
        package = plugin_root / item["name"]
        init_file = package / "__init__.py"
        try:
            package_stat = package.lstat()
            init_stat = init_file.lstat()
        except FileNotFoundError as exc:
            raise AuthoringError(
                "PLUGIN_PACKAGE_MISSING", "enabled plugin package is missing"
            ) from exc
        if (
            not stat.S_ISDIR(package_stat.st_mode)
            or _is_reparse_point(package_stat)
            or not stat.S_ISREG(init_stat.st_mode)
            or _is_reparse_point(init_stat)
        ):
            raise AuthoringError(
                "PLUGIN_PACKAGE_UNSAFE", "enabled plugin package is unsafe"
            )


def _validate_directory_chain(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        current_stat = current.lstat()
        if not stat.S_ISDIR(current_stat.st_mode) or _is_reparse_point(current_stat):
            raise AuthoringError(
                "PLUGIN_PACKAGE_UNSAFE", "plugin root contains an unsafe component"
            )


def _is_reparse_point(path_stat: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(path_stat.st_mode) or bool(
        getattr(path_stat, "st_file_attributes", 0) & reparse_flag
    )


def _strict_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _strict_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(
            _strict_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return left == right


def _validate_schema_constraints(metadata: tuple[Any, ...], value: Any) -> None:
    if value is None or type(value) not in (int, float):
        return
    for constraint in metadata:
        ge = getattr(constraint, "ge", None)
        le = getattr(constraint, "le", None)
        gt = getattr(constraint, "gt", None)
        lt = getattr(constraint, "lt", None)
        if (
            (ge is not None and value < ge)
            or (le is not None and value > le)
            or (gt is not None and value <= gt)
            or (lt is not None and value >= lt)
        ):
            raise AuthoringError(
                "VALUE_OUT_OF_RANGE",
                "value is outside the production schema boundary",
            )


def _validate_owner_constraints(names: tuple[str, ...], value: Any) -> None:
    if names == ("screen", "infer_timeout_sec") and value <= 0:
        raise AuthoringError(
            "VALUE_OUT_OF_RANGE", "screen.infer_timeout_sec must be positive"
        )
