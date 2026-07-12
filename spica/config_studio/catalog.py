"""Schema-driven read model for Config Studio."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
from types import MappingProxyType, UnionType
from typing import Any, Literal, Mapping, Union, get_args, get_origin

from pydantic import BaseModel, TypeAdapter

from agent_tools.function_tools.song.config import (
    DEFAULT_CONFIG as SONG_DEFAULT_CONFIG,
    resolve_effective_song_config,
    song_enabled,
    song_path_kind,
)
from spica.config.manager import ConfigResolution
from spica.config_studio.paths import (
    ConfigFieldPath,
    FieldSegment,
    ListIndexSegment,
    MapKeySegment,
)


_MISSING = object()
_GRAPH_MAX_DEPTH = 32
_GRAPH_MAX_NODES = 4096
_GRAPH_MAX_COLLECTION_ITEMS = 256
_GRAPH_MAX_STRING_CHARS = 8192
_RUNTIME_DERIVED_FIELDS = {
    ("character", "character_id"),
    ("character", "character_profile"),
    ("character", "character_name"),
}
_OWNER_FOLDED_AUTO_FIELDS = {
    ("platform", "os"),
    ("stt", "mic_backend"),
}


@dataclass(frozen=True)
class CatalogDependency:
    path: ConfigFieldPath
    expected_value: Any


@dataclass(frozen=True)
class CatalogPathHealth:
    status: str
    code: str
    expected_kind: str


@dataclass(frozen=True)
class CatalogField:
    path: ConfigFieldPath
    control: str
    default_value: Any
    file_value: Any
    file_present: bool
    next_launch_value: Any
    source_kind: str
    environment_variable: str | None
    environment_layer: str | None
    file_value_shadowed: bool
    editable: bool
    unsupported_reason: str | None
    literal_choices: tuple[Any, ...]
    minimum: int | float | None
    maximum: int | float | None
    owner: str
    effect_policy: str
    description: str
    level: str
    dependencies: tuple[CatalogDependency, ...]
    path_health: CatalogPathHealth | None
    structured_schema: Mapping[str, Any] | None
    nullable: bool
    authoring_complete: bool
    redact_absolute_paths: bool = False


class CatalogSnapshot:
    __slots__ = ("_fields", "_graph_truncation")

    def __init__(
        self,
        fields: Mapping[ConfigFieldPath, CatalogField],
        *,
        graph_truncation: Mapping[str, int] | None = None,
    ) -> None:
        self._fields = MappingProxyType(dict(fields))
        self._graph_truncation = MappingProxyType(dict(graph_truncation or {}))

    @property
    def fields(self) -> tuple[CatalogField, ...]:
        return tuple(self._fields.values())

    def field(self, path: ConfigFieldPath) -> CatalogField:
        return self._fields[path]

    def to_wire(
        self,
        *,
        max_string_chars: int = 2048,
        max_collection_items: int = 256,
        max_depth: int = 8,
        max_total_bytes: int = 512 * 1024,
    ) -> dict[str, Any]:
        if max_total_bytes < 512:
            raise ValueError("catalog response budget must be at least 512 bytes")
        truncation = {
            "strings": self._graph_truncation.get("strings", 0),
            "collections": self._graph_truncation.get("collections", 0),
            "depth": self._graph_truncation.get("depth", 0),
            "unsupported": self._graph_truncation.get("unsupported", 0),
            "total_bytes": 0,
            "cycles": self._graph_truncation.get("cycles", 0),
            "aliases": self._graph_truncation.get("aliases", 0),
            "nodes": self._graph_truncation.get("nodes", 0),
        }
        fields_complete = not any(self._graph_truncation.values())
        rows = self.fields
        if len(rows) > max_collection_items:
            rows = rows[:max_collection_items]
            truncation["collections"] += 1
            fields_complete = False
        fields = [
            _field_to_wire(
                row,
                max_string_chars=max_string_chars,
                max_collection_items=max_collection_items,
                max_depth=max_depth,
                truncation=truncation,
            )
            for row in rows
        ]
        payload = {
            "fields": fields,
            "fields_complete": fields_complete,
            "truncation": truncation,
        }
        while fields and _encoded_size(payload) > max_total_bytes:
            fields.pop()
            payload["fields_complete"] = False
            truncation["total_bytes"] += 1
        if _encoded_size(payload) > max_total_bytes:
            raise ValueError("catalog response budget cannot represent metadata")
        return payload


class ConfigCatalog:
    """Project production schema metadata and owner resolution into UI rows."""

    def __init__(
        self,
        *,
        model_type: type[BaseModel],
        raw_document: Mapping[str, Any],
        resolution: ConfigResolution,
        repo_root: str | Path | None = None,
        song_legacy_path: str | Path | None = None,
        readonly_reasons: Mapping[tuple[str, ...], str] | None = None,
    ) -> None:
        self._model_type = model_type
        graph_state = _GraphProjectionState()
        projected = _bounded_graph_copy(raw_document, state=graph_state, depth=0)
        self._raw_document = projected if isinstance(projected, dict) else {}
        self._graph_truncation = dict(graph_state.truncation)
        self._resolution = resolution
        self._repo_root = (
            None
            if repo_root is None
            else Path(os.path.abspath(os.fspath(repo_root)))
        )
        self._song_legacy_path = song_legacy_path
        self._readonly_reasons = dict(readonly_reasons or {})

    def snapshot(self) -> CatalogSnapshot:
        default_document = self._model_type().model_dump()
        rows: dict[ConfigFieldPath, CatalogField] = {}
        graph_truncation = dict(self._graph_truncation)
        self._walk_model(
            model_type=self._model_type,
            names=(),
            default_node=default_document,
            raw_node=self._raw_document,
            rows=rows,
        )
        self._walk_unknown(
            model_type=self._model_type,
            raw_node=self._raw_document,
            typed_prefix=(),
            rows=rows,
            graph_truncation=graph_truncation,
        )
        return CatalogSnapshot(rows, graph_truncation=graph_truncation)

    def _walk_model(
        self,
        *,
        model_type: type[BaseModel],
        names: tuple[str, ...],
        default_node: Mapping[str, Any],
        raw_node: Any,
        rows: dict[ConfigFieldPath, CatalogField],
    ) -> None:
        raw_mapping = raw_node if isinstance(raw_node, Mapping) else {}
        for name, field_info in model_type.model_fields.items():
            annotation = field_info.annotation
            nested_model = _nested_model_type(annotation)
            child_names = names + (name,)
            default_value = default_node.get(name)
            raw_value = raw_mapping.get(name, _MISSING)
            if nested_model is not None:
                self._walk_model(
                    model_type=nested_model,
                    names=child_names,
                    default_node=default_value if isinstance(default_value, Mapping) else {},
                    raw_node={} if raw_value is _MISSING else raw_value,
                    rows=rows,
                )
                continue

            if child_names == ("song",):
                self._walk_song(
                    default_value=default_value,
                    raw_value={} if raw_value is _MISSING else raw_value,
                    rows=rows,
                )
                continue

            path = ConfigFieldPath.fields(*child_names)
            try:
                resolved = self._resolution.resolved_at(child_names)
            except KeyError:
                resolved = None
            resolution_available = resolved is not None
            runtime_derived = child_names in _RUNTIME_DERIVED_FIELDS
            readonly_reason = self._readonly_reason(child_names)
            legacy_owner_active = readonly_reason == "legacy_owner_active"
            owner_derived = runtime_derived or legacy_owner_active or (
                child_names in _OWNER_FOLDED_AUTO_FIELDS
                and resolved is not None
                and resolved.next_launch_value == "auto"
            )
            editable = (
                resolution_available and not runtime_derived
                and readonly_reason is None
            )
            if not resolution_available:
                unsupported_reason = "resolution_unavailable"
            elif readonly_reason is not None:
                unsupported_reason = readonly_reason
            elif runtime_derived:
                unsupported_reason = "runtime_derived"
            else:
                unsupported_reason = None
            control = _control_for(annotation)
            structured_schema = (
                _compact_authoring_schema(annotation)
                if control == "structured"
                else None
            )
            rows[path] = CatalogField(
                path=path,
                control=control,
                default_value=_defensive(default_value),
                file_value=None if raw_value is _MISSING else _defensive(raw_value),
                file_present=raw_value is not _MISSING,
                next_launch_value=(
                    None
                    if resolved is None or owner_derived
                    else _defensive(resolved.next_launch_value)
                ),
                source_kind=(
                    "unavailable"
                    if resolved is None
                    else "legacy_owner_active"
                    if legacy_owner_active
                    else "owner_derived"
                    if owner_derived
                    else resolved.source.kind
                ),
                environment_variable=(
                    None
                    if resolved is None or owner_derived
                    else resolved.source.environment_variable
                ),
                environment_layer=(
                    None
                    if resolved is None or owner_derived
                    else resolved.source.environment_layer
                ),
                file_value_shadowed=(
                    raw_value is not _MISSING
                    and resolved is not None
                    and (
                        legacy_owner_active
                        or (
                            not owner_derived
                            and resolved.source.kind
                            in {"env_override", "secret_tainted_env_override"}
                        )
                    )
                ),
                editable=editable,
                unsupported_reason=unsupported_reason,
                literal_choices=_literal_choices(annotation),
                minimum=_bounds_for(child_names, field_info)[0],
                maximum=_bounds_for(child_names, field_info)[1],
                owner=(
                    "PluginManifest/AppConfig"
                    if child_names == ("plugins",)
                    else "CharacterPackage/AppHost"
                    if child_names in _RUNTIME_DERIVED_FIELDS
                    else "Legacy configuration owner"
                    if legacy_owner_active
                    else "Production platform fold"
                    if child_names in _OWNER_FOLDED_AUTO_FIELDS
                    else "ConfigManager/AppConfig"
                ),
                effect_policy=(
                    "owner_derived_on_next_launch"
                    if owner_derived and not legacy_owner_active
                    else "legacy_owner_on_next_launch"
                    if legacy_owner_active
                    else "next_spica_launch"
                ),
                description=field_info.description or "Owner has not supplied a description.",
                level=_presentation_level(child_names, control=control),
                dependencies=_dependencies_for_plain_path(child_names),
                path_health=_path_health(
                    resolved.next_launch_value if resolved is not None else default_value,
                    field_info=field_info,
                    repo_root=self._repo_root,
                ),
                structured_schema=structured_schema,
                nullable=_annotation_is_nullable(annotation),
                authoring_complete=(
                    control != "structured" or structured_schema is not None
                ),
            )

    def _readonly_reason(self, names: tuple[str, ...]) -> str | None:
        matches = (
            (prefix, reason)
            for prefix, reason in self._readonly_reasons.items()
            if names[: len(prefix)] == prefix
        )
        selected = sorted(matches, key=lambda item: len(item[0]), reverse=True)
        return selected[0][1] if selected else None

    def _walk_song(
        self,
        *,
        default_value: Any,
        raw_value: Any,
        rows: dict[ConfigFieldPath, CatalogField],
    ) -> None:
        app_config = self._resolution.to_app_config()
        resolved_song = resolve_effective_song_config(
            config=app_config,
            legacy_path=self._song_legacy_path,
        )
        resolved_song = dict(resolved_song)
        resolved_song.pop("_config_path", None)
        resolved_song["enabled"] = _catalog_song_enabled(resolved_song)
        legacy_active = (
            self._song_legacy_path is not None
            and Path(self._song_legacy_path).is_file()
        )
        readonly_reason = self._readonly_reasons.get(("song",))
        for keys, value in _dynamic_leaves(resolved_song):
            raw_leaf = _dynamic_get(raw_value, keys)
            default_leaf = _dynamic_get(SONG_DEFAULT_CONFIG, keys)
            typed_path = ConfigFieldPath(
                (FieldSegment("song"),)
                + tuple(MapKeySegment(str(key)) for key in keys)
            )
            is_enabled = keys == ("enabled",)
            control = "switch" if is_enabled else _control_for(type(value))
            source_kind = (
                "legacy_document"
                if legacy_active
                else "file"
                if raw_leaf is not _MISSING
                else "default"
            )
            path_kind = song_path_kind(keys)
            rows[typed_path] = CatalogField(
                path=typed_path,
                control=control,
                default_value=(
                    None if default_leaf is _MISSING else _defensive(default_leaf)
                ),
                file_value=None if raw_leaf is _MISSING else _defensive(raw_leaf),
                file_present=raw_leaf is not _MISSING,
                next_launch_value=_defensive(value),
                source_kind=source_kind,
                environment_variable=None,
                environment_layer=None,
                file_value_shadowed=(
                    raw_leaf is not _MISSING
                    and legacy_active
                ),
                editable=is_enabled and readonly_reason is None,
                unsupported_reason=(
                    readonly_reason
                    if readonly_reason is not None
                    else None
                    if is_enabled
                    else "owner_schema_unavailable"
                ),
                literal_choices=(),
                minimum=None,
                maximum=None,
                owner="SongConfigOwner/AppConfig",
                effect_policy="next_spica_launch",
                description=(
                    "Owner-backed song assembly switch."
                    if is_enabled
                    else "Song owner has not supplied a canonical typed authoring schema."
                ),
                level="basic" if is_enabled else "advanced",
                dependencies=(
                    ()
                    if is_enabled
                    else (
                        CatalogDependency(
                            path=ConfigFieldPath(
                                (FieldSegment("song"), MapKeySegment("enabled"))
                            ),
                            expected_value=True,
                        ),
                    )
                ),
                path_health=None,
                structured_schema=None,
                nullable=False,
                authoring_complete=control != "structured",
                redact_absolute_paths=path_kind is not None,
            )

    def _walk_unknown(
        self,
        *,
        model_type: type[BaseModel],
        raw_node: Any,
        typed_prefix: tuple[Any, ...],
        rows: dict[ConfigFieldPath, CatalogField],
        graph_truncation: dict[str, int],
    ) -> None:
        if not isinstance(raw_node, Mapping):
            return
        for key, value in raw_node.items():
            field_info = model_type.model_fields.get(str(key))
            if field_info is None:
                for dynamic_keys, leaf_value in _dynamic_leaves(
                    value,
                    truncation=graph_truncation,
                ):
                    control = _control_for(type(leaf_value))
                    path = ConfigFieldPath(
                        typed_prefix
                        + (MapKeySegment(str(key)),)
                        + tuple(MapKeySegment(part) for part in dynamic_keys)
                    )
                    rows[path] = CatalogField(
                        path=path,
                        control=control,
                        default_value=None,
                        file_value=_defensive(leaf_value),
                        file_present=True,
                        next_launch_value=None,
                        source_kind="file",
                        environment_variable=None,
                        environment_layer=None,
                        file_value_shadowed=False,
                        editable=False,
                        unsupported_reason="owner_unrecognized",
                        literal_choices=(),
                        minimum=None,
                        maximum=None,
                        owner="ConfigManager/unrecognized",
                        effect_policy="unavailable",
                        description="Production owner does not recognize this document key.",
                        level="advanced",
                        dependencies=(),
                        path_health=None,
                        structured_schema=None,
                        nullable=False,
                        authoring_complete=control != "structured",
                    )
                continue
            nested = _nested_model_type(field_info.annotation)
            if nested is not None:
                self._walk_unknown(
                    model_type=nested,
                    raw_node=value,
                    typed_prefix=typed_prefix + (FieldSegment(str(key)),),
                    rows=rows,
                    graph_truncation=graph_truncation,
                )


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


def _annotation_is_nullable(annotation: Any) -> bool:
    if annotation is type(None):
        return True
    origin = get_origin(annotation)
    return origin in (Union, UnionType) and type(None) in get_args(annotation)


def _catalog_song_enabled(config: Mapping[str, Any]) -> bool:
    """Use the production strict contract without logging attacker-controlled data."""

    raw = config.get("enabled", True)
    if type(raw) is bool:
        return song_enabled({"enabled": raw})
    if isinstance(raw, str) and raw.strip().lower() in {"true", "false"}:
        return song_enabled({"enabled": raw})
    return False


def _literal_choices(annotation: Any) -> tuple[Any, ...]:
    origin = get_origin(annotation)
    if origin is Literal:
        return get_args(annotation)
    if origin in (Union, UnionType):
        for option in get_args(annotation):
            choices = _literal_choices(option)
            if choices:
                return choices
    return ()


def _compact_authoring_schema(annotation: Any) -> Mapping[str, Any] | None:
    """Project only the Pydantic schema vocabulary used by typed DOM editors."""

    try:
        root = TypeAdapter(annotation).json_schema()
    except (TypeError, ValueError):
        return None
    definitions = root.get("$defs", {})
    if not isinstance(definitions, Mapping):
        return None
    remaining_nodes = [256]

    def project(
        node: Any,
        *,
        depth: int,
        resolving: frozenset[str],
    ) -> dict[str, Any]:
        if depth > 10 or remaining_nodes[0] <= 0 or not isinstance(node, Mapping):
            raise ValueError("authoring schema exceeds its projection budget")
        remaining_nodes[0] -= 1
        reference = node.get("$ref")
        if reference is not None:
            if (
                not isinstance(reference, str)
                or not reference.startswith("#/$defs/")
            ):
                raise ValueError("authoring schema reference is unsupported")
            name = reference.removeprefix("#/$defs/")
            if name in resolving or name not in definitions:
                raise ValueError("authoring schema reference is recursive")
            return project(
                definitions[name],
                depth=depth + 1,
                resolving=resolving | {name},
            )

        result: dict[str, Any] = {}
        supported_types = {
            "array",
            "boolean",
            "integer",
            "null",
            "number",
            "object",
            "string",
        }
        schema_type = node.get("type", _MISSING)
        if schema_type is not _MISSING:
            if schema_type not in supported_types:
                raise ValueError("authoring schema type is invalid")
            result["type"] = schema_type
        default = node.get("default", _MISSING)
        if default is not _MISSING:
            if (
                default is not None
                and type(default) not in {bool, int, float, str}
            ) or (type(default) is float and not math.isfinite(default)):
                raise ValueError("authoring schema default is invalid")
            result["default"] = default
        enum = node.get("enum", _MISSING)
        if enum is not _MISSING:
            if (
                not isinstance(enum, list)
                or not 1 <= len(enum) <= 64
                or any(
                    value is not None
                    and type(value) not in {bool, int, float, str}
                    or type(value) is float
                    and not math.isfinite(value)
                    for value in enum
                )
            ):
                raise ValueError("authoring schema enum is invalid")
            result["enum"] = list(enum)
        any_of = node.get("anyOf", _MISSING)
        if any_of is not _MISSING:
            if (
                not isinstance(any_of, list)
                or not 1 <= len(any_of) <= 8
                or any(not isinstance(branch, Mapping) for branch in any_of)
            ):
                raise ValueError("authoring schema union is invalid")
            result["anyOf"] = [
                project(branch, depth=depth + 1, resolving=resolving)
                for branch in any_of
            ]
        items = node.get("items", _MISSING)
        if items is not _MISSING:
            if not isinstance(items, Mapping):
                raise ValueError("authoring schema items are invalid")
            result["items"] = project(
                items,
                depth=depth + 1,
                resolving=resolving,
            )
        properties = node.get("properties", _MISSING)
        if properties is not _MISSING:
            if (
                not isinstance(properties, Mapping)
                or len(properties) > 64
                or any(
                    not isinstance(name, str)
                    or not 0 < len(name) <= 128
                    or not isinstance(child, Mapping)
                    for name, child in properties.items()
                )
            ):
                raise ValueError("authoring schema properties are invalid")
            result["properties"] = {
                str(name): project(
                    child,
                    depth=depth + 1,
                    resolving=resolving,
                )
                for name, child in properties.items()
            }
        additional = node.get("additionalProperties", _MISSING)
        if additional is not _MISSING:
            if isinstance(additional, Mapping):
                result["additionalProperties"] = project(
                    additional,
                    depth=depth + 1,
                    resolving=resolving,
                )
            elif additional is False:
                result["additionalProperties"] = False
            else:
                raise ValueError(
                    "authoring schema additional properties are invalid"
                )
        required = node.get("required", _MISSING)
        if required is not _MISSING:
            if (
                not isinstance(required, list)
                or len(required) > 64
                or len(set(required)) != len(required)
                or any(
                    not isinstance(name, str) or not 0 < len(name) <= 128
                    for name in required
                )
            ):
                raise ValueError("authoring schema required fields are invalid")
            result["required"] = list(required)
        for name in (
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "minItems",
            "maxItems",
            "minLength",
            "maxLength",
        ):
            value = node.get(name, _MISSING)
            if value is _MISSING:
                continue
            if name in {"minItems", "maxItems", "minLength", "maxLength"}:
                valid = type(value) is int and value >= 0
            else:
                valid = type(value) in {int, float} and math.isfinite(value)
            if not valid:
                raise ValueError("authoring schema numeric constraint is invalid")
            result[name] = value
        if not result:
            raise ValueError("authoring schema has no supported vocabulary")
        return result

    try:
        return MappingProxyType(project(root, depth=0, resolving=frozenset()))
    except (TypeError, ValueError, RecursionError):
        return None


def _control_for(annotation: Any) -> str:
    if _literal_choices(annotation):
        return "select"
    origin = get_origin(annotation)
    options = get_args(annotation) if origin in (Union, UnionType) else (annotation,)
    concrete = {option for option in options if option is not type(None)}
    if concrete == {bool}:
        return "switch"
    if concrete and concrete <= {int, float}:
        return "number"
    if concrete == {str}:
        return "text"
    if any(
        get_origin(option) in (list, tuple, dict)
        or option in (list, tuple, dict)
        for option in concrete
    ):
        return "structured"
    return "text"


def _presentation_level(names: tuple[str, ...], *, control: str) -> str:
    if control in {"switch", "select"}:
        return "basic"
    if names and names[-1] in {
        "provider",
        "backend",
        "mic_backend",
        "model",
        "device",
        "language",
        "reaction_mode",
    }:
        return "basic"
    return "advanced"


def _dependencies_for_plain_path(
    names: tuple[str, ...],
) -> tuple[CatalogDependency, ...]:
    if len(names) > 1 and names[0] in {"anime", "screen"} and names[1] != "enabled":
        return (
            CatalogDependency(
                path=ConfigFieldPath.fields(names[0], "enabled"),
                expected_value=True,
            ),
        )
    return ()


def _defensive(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _defensive(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_defensive(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_defensive(item) for item in value)
    return value


def _path_health(
    value: Any,
    *,
    field_info: Any,
    repo_root: Path | None,
) -> CatalogPathHealth | None:
    extra = field_info.json_schema_extra
    if not isinstance(extra, Mapping):
        return None
    semantics = extra.get("path_semantics")
    if not isinstance(semantics, Mapping):
        return None
    expected_kind = semantics.get("kind")
    base = semantics.get("base")
    expand_user = semantics.get("expand_user", False)
    if expected_kind not in {"directory", "file"} or base not in {
        "repository",
        "launch_working_directory",
    } or type(expand_user) is not bool:
        return CatalogPathHealth("unavailable", "PATH_METADATA_UNSUPPORTED", "unknown")
    return _path_health_from_contract(
        value,
        expected_kind=expected_kind,
        base=base,
        expand_user=expand_user,
        repo_root=repo_root,
    )


def _path_health_from_contract(
    value: Any,
    *,
    expected_kind: str,
    base: str,
    expand_user: bool,
    repo_root: Path | None,
) -> CatalogPathHealth:
    if value is None or value == "":
        return CatalogPathHealth("unset", "PATH_UNSET", expected_kind)
    if not isinstance(value, str) or repo_root is None:
        return CatalogPathHealth("unavailable", "PATH_UNAVAILABLE", expected_kind)

    configured = Path(value)
    if expand_user and value.startswith("~"):
        return CatalogPathHealth(
            "unavailable",
            "PATH_BASE_UNAVAILABLE",
            expected_kind,
        )
    if not configured.is_absolute() and base == "launch_working_directory":
        return CatalogPathHealth(
            "unavailable",
            "PATH_BASE_UNAVAILABLE",
            expected_kind,
        )
    target = Path(
        os.path.abspath(
            os.fspath(configured if configured.is_absolute() else repo_root / configured)
        )
    )
    try:
        inside_root = os.path.commonpath((repo_root, target)) == os.fspath(repo_root)
    except ValueError:
        inside_root = False
    if not inside_root:
        return CatalogPathHealth("unsafe", "PATH_OUTSIDE_ROOT", expected_kind)

    chain = [repo_root]
    current = repo_root
    for part in target.relative_to(repo_root).parts:
        current = current / part
        chain.append(current)
    for index, component in enumerate(chain):
        try:
            info = component.lstat()
        except FileNotFoundError:
            return CatalogPathHealth("missing", "PATH_MISSING", expected_kind)
        except OSError:
            return CatalogPathHealth("unavailable", "PATH_UNAVAILABLE", expected_kind)
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & 0x400
        ):
            return CatalogPathHealth("unsafe", "PATH_SYMLINK_UNSAFE", expected_kind)
        is_target = index == len(chain) - 1
        if not is_target and not stat.S_ISDIR(info.st_mode):
            return CatalogPathHealth("unsafe", "PATH_COMPONENT_UNSAFE", expected_kind)
        if is_target:
            expected = (
                stat.S_ISDIR(info.st_mode)
                if expected_kind == "directory"
                else stat.S_ISREG(info.st_mode)
            )
            if not expected:
                return CatalogPathHealth("unsafe", "PATH_KIND_MISMATCH", expected_kind)
    return CatalogPathHealth("healthy", "PATH_HEALTHY", expected_kind)


class _GraphProjectionState:
    __slots__ = (
        "active_ids",
        "node_limit_reported",
        "nodes_seen",
        "seen_ids",
        "truncation",
    )

    def __init__(self) -> None:
        self.active_ids: set[int] = set()
        self.seen_ids: set[int] = set()
        self.nodes_seen = 0
        self.node_limit_reported = False
        self.truncation = {
            "strings": 0,
            "collections": 0,
            "depth": 0,
            "unsupported": 0,
            "cycles": 0,
            "aliases": 0,
            "nodes": 0,
        }


def _bounded_graph_copy(
    value: Any,
    *,
    state: _GraphProjectionState,
    depth: int,
) -> Any:
    """Copy an untrusted YAML/Python graph into a finite JSON-like tree."""

    if state.nodes_seen >= _GRAPH_MAX_NODES:
        if not state.node_limit_reported:
            state.truncation["nodes"] += 1
            state.node_limit_reported = True
        return "<node-limit>"
    state.nodes_seen += 1

    if depth > _GRAPH_MAX_DEPTH:
        state.truncation["depth"] += 1
        return "<depth-limit>"
    if isinstance(value, str):
        return _bounded_graph_string(value, state=state)
    if type(value) is float and not math.isfinite(value):
        state.truncation["unsupported"] += 1
        return "<non-finite-number>"
    if value is None or type(value) in (bool, int, float):
        return value
    if isinstance(value, Mapping):
        reference = _graph_reference(value, state=state)
        if reference is not None:
            return reference
        node_id = id(value)
        result: dict[str, Any] = {}
        try:
            for index, (key, item) in enumerate(value.items()):
                if index >= _GRAPH_MAX_COLLECTION_ITEMS:
                    state.truncation["collections"] += 1
                    break
                safe_key = _bounded_graph_key(key, index=index, state=state)
                if safe_key in result:
                    state.truncation["unsupported"] += 1
                    safe_key = f"<duplicate-key-{index}>"
                result[safe_key] = _bounded_graph_copy(
                    item,
                    state=state,
                    depth=depth + 1,
                )
        finally:
            state.active_ids.remove(node_id)
        return result
    if isinstance(value, (tuple, list)):
        reference = _graph_reference(value, state=state)
        if reference is not None:
            return reference
        node_id = id(value)
        result: list[Any] = []
        try:
            for index, item in enumerate(value):
                if index >= _GRAPH_MAX_COLLECTION_ITEMS:
                    state.truncation["collections"] += 1
                    break
                result.append(
                    _bounded_graph_copy(
                        item,
                        state=state,
                        depth=depth + 1,
                    )
                )
        finally:
            state.active_ids.remove(node_id)
        return result
    state.truncation["unsupported"] += 1
    return "<unsupported-value>"


def _graph_reference(
    value: Any,
    *,
    state: _GraphProjectionState,
) -> str | None:
    node_id = id(value)
    if node_id in state.active_ids:
        state.truncation["cycles"] += 1
        return "<cycle-reference>"
    if node_id in state.seen_ids:
        state.truncation["aliases"] += 1
        return "<alias-reference>"
    state.seen_ids.add(node_id)
    state.active_ids.add(node_id)
    return None


def _bounded_graph_key(
    key: Any,
    *,
    index: int,
    state: _GraphProjectionState,
) -> str:
    if isinstance(key, str):
        return _bounded_graph_string(key, state=state)
    if key is None or type(key) in (bool, int):
        return str(key)
    if type(key) is float and math.isfinite(key):
        return str(key)
    state.truncation["unsupported"] += 1
    return f"<unsupported-key-{index}>"


def _bounded_graph_string(
    value: str,
    *,
    state: _GraphProjectionState,
) -> str:
    if len(value) <= _GRAPH_MAX_STRING_CHARS:
        return value
    state.truncation["strings"] += 1
    return value[: _GRAPH_MAX_STRING_CHARS - 1] + "…"


def _dynamic_leaves(
    node: Any,
    prefix: tuple[str, ...] = (),
    *,
    truncation: dict[str, int] | None = None,
    active_ids: frozenset[int] = frozenset(),
    seen_ids: set[int] | None = None,
) -> tuple[tuple[tuple[str, ...], Any], ...]:
    if isinstance(node, Mapping) and node:
        node_id = id(node)
        if node_id in active_ids:
            if truncation is not None:
                truncation["cycles"] = truncation.get("cycles", 0) + 1
            return ((prefix, "<cycle-reference>"),)
        if seen_ids is None:
            seen_ids = set()
        if node_id in seen_ids:
            if truncation is not None:
                truncation["aliases"] = truncation.get("aliases", 0) + 1
            return ((prefix, "<alias-reference>"),)
        seen_ids.add(node_id)
        child_active_ids = active_ids | {node_id}
        items: list[tuple[tuple[str, ...], Any]] = []
        for key, value in node.items():
            items.extend(
                _dynamic_leaves(
                    value,
                    prefix + (str(key),),
                    truncation=truncation,
                    active_ids=child_active_ids,
                    seen_ids=seen_ids,
                )
            )
        return tuple(items)
    return ((prefix, node),)


def _dynamic_get(node: Any, keys: tuple[str, ...]) -> Any:
    current = node
    for key in keys:
        if not isinstance(current, Mapping) or key not in current:
            return _MISSING
        current = current[key]
    return current


def _bounds_for(names: tuple[str, ...], field_info: Any) -> tuple[Any, Any]:
    minimum = None
    maximum = None
    for metadata in field_info.metadata:
        if getattr(metadata, "ge", None) is not None:
            minimum = metadata.ge
        if getattr(metadata, "le", None) is not None:
            maximum = metadata.le
    return minimum, maximum


_SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|secret|token|password|cookie)", re.IGNORECASE
)


def _encoded_size(payload: Mapping[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _field_to_wire(
    field: CatalogField,
    *,
    max_string_chars: int,
    max_collection_items: int,
    max_depth: int,
    truncation: dict[str, int],
) -> dict[str, Any]:
    field_truncation = {name: 0 for name in truncation}
    redact_values = _path_looks_secret(field.path) and field.path_health is None
    path_segments = field.path.segments
    if len(path_segments) > max_collection_items:
        path_segments = path_segments[:max_collection_items]
        field_truncation["collections"] += 1
    dependencies = field.dependencies
    if len(dependencies) > max_collection_items:
        dependencies = dependencies[:max_collection_items]
        field_truncation["collections"] += 1
    data_bound = {
        "max_string_chars": max_string_chars,
        "max_collection_items": max_collection_items,
        "max_depth": max_depth,
        "truncation": field_truncation,
        "redact_absolute_paths": (
            field.path_health is not None or field.redact_absolute_paths
        ),
    }
    metadata_bound = {**data_bound, "redact_absolute_paths": False}
    rendered = {
        "path": [
            _segment_to_wire(
                segment,
                max_string_chars=max_string_chars,
                truncation=field_truncation,
            )
            for segment in path_segments
        ],
        "display_path": _bounded_text(
            _display_path(field.path),
            max_string_chars=max_string_chars,
            truncation=field_truncation,
        ),
        "control": field.control,
        "nullable": field.nullable,
        "default_value": (
            "<redacted>"
            if redact_values
            else _bounded_value(field.default_value, depth=0, **data_bound)
        ),
        "file_value": (
            "<redacted>"
            if redact_values
            else _bounded_value(field.file_value, depth=0, **data_bound)
        ),
        "file_present": field.file_present,
        "next_launch_value": (
            "<redacted>"
            if redact_values
            else _bounded_value(field.next_launch_value, depth=0, **data_bound)
        ),
        "source_kind": field.source_kind,
        "environment_variable": field.environment_variable,
        "environment_layer": field.environment_layer,
        "file_value_shadowed": field.file_value_shadowed,
        "editable": field.editable,
        "unsupported_reason": field.unsupported_reason,
        "literal_choices": _bounded_value(
            field.literal_choices,
            depth=0,
            **metadata_bound,
        ),
        "minimum": field.minimum,
        "maximum": field.maximum,
        "owner": field.owner,
        "effect_policy": field.effect_policy,
        "description": _bounded_value(
            field.description,
            depth=0,
            **metadata_bound,
        ),
        "level": field.level,
        "dependencies": [
            {
                "path": [
                    _segment_to_wire(
                        segment,
                        max_string_chars=max_string_chars,
                        truncation=field_truncation,
                    )
                    for segment in dependency.path.segments
                ],
                "display_path": _bounded_text(
                    _display_path(dependency.path),
                    max_string_chars=max_string_chars,
                    truncation=field_truncation,
                ),
                "expected_value": _bounded_value(
                    dependency.expected_value,
                    depth=0,
                    **metadata_bound,
                ),
            }
            for dependency in dependencies
        ],
        "path_health": (
            None
            if field.path_health is None
            else {
                "status": field.path_health.status,
                "code": field.path_health.code,
                "expected_kind": field.path_health.expected_kind,
            }
        ),
        "structured_schema": (
            None
            if field.structured_schema is None
            else _bounded_value(
                field.structured_schema,
                depth=0,
                **metadata_bound,
            )
        ),
    }
    rendered["authoring_complete"] = (
        field.authoring_complete
        and not redact_values
        and not any(field_truncation.values())
    )
    for name, count in field_truncation.items():
        truncation[name] += count
    return rendered


def _path_looks_secret(path: ConfigFieldPath) -> bool:
    for segment in path.segments:
        if isinstance(segment, FieldSegment) and _SECRET_KEY_RE.search(segment.name):
            return True
        if isinstance(segment, MapKeySegment) and _SECRET_KEY_RE.search(segment.key):
            return True
    return False


def _segment_to_wire(
    segment: Any,
    *,
    max_string_chars: int,
    truncation: dict[str, int],
) -> dict[str, Any]:
    if isinstance(segment, FieldSegment):
        return {
            "kind": "field",
            "name": _bounded_text(
                segment.name,
                max_string_chars=max_string_chars,
                truncation=truncation,
            ),
        }
    if isinstance(segment, MapKeySegment):
        return {
            "kind": "map_key",
            "key": _bounded_text(
                segment.key,
                max_string_chars=max_string_chars,
                truncation=truncation,
            ),
        }
    if isinstance(segment, ListIndexSegment):
        return {"kind": "list_index", "index": segment.index}
    raise TypeError("unsupported path segment")


def _display_path(path: ConfigFieldPath) -> str:
    rendered = ""
    for segment in path.segments:
        if isinstance(segment, FieldSegment):
            rendered += ("." if rendered else "") + segment.name
        elif isinstance(segment, MapKeySegment):
            rendered += f"[{segment.key!r}]"
        elif isinstance(segment, ListIndexSegment):
            rendered += f"[{segment.index}]"
    return rendered


def _bounded_value(
    value: Any,
    *,
    depth: int,
    max_string_chars: int,
    max_collection_items: int,
    max_depth: int,
    truncation: dict[str, int],
    redact_absolute_paths: bool,
) -> Any:
    if depth > max_depth:
        truncation["depth"] += 1
        return "<depth-limit>"
    if isinstance(value, str):
        if redact_absolute_paths and _is_absolute_path_text(value):
            truncation["unsupported"] += 1
            return "<external-path>"
        return _bounded_text(
            value,
            max_string_chars=max_string_chars,
            truncation=truncation,
        )
    if type(value) is float and not math.isfinite(value):
        truncation["unsupported"] += 1
        return "<non-finite-number>"
    if value is None or type(value) in (bool, int, float):
        return value
    if isinstance(value, Mapping):
        items = list(value.items())
        if len(items) > max_collection_items:
            items = items[:max_collection_items]
            truncation["collections"] += 1
        result: dict[str, Any] = {}
        for key, item in items:
            safe_key = str(key)
            if _SECRET_KEY_RE.search(safe_key):
                truncation["unsupported"] += 1
                result[safe_key] = "<redacted>"
                continue
            result[safe_key] = _bounded_value(
                item,
                depth=depth + 1,
                max_string_chars=max_string_chars,
                max_collection_items=max_collection_items,
                max_depth=max_depth,
                truncation=truncation,
                redact_absolute_paths=redact_absolute_paths,
            )
        return result
    if isinstance(value, (tuple, list)):
        items = list(value)
        if len(items) > max_collection_items:
            items = items[:max_collection_items]
            truncation["collections"] += 1
        return [
            _bounded_value(
                item,
                depth=depth + 1,
                max_string_chars=max_string_chars,
                max_collection_items=max_collection_items,
                max_depth=max_depth,
                truncation=truncation,
                redact_absolute_paths=redact_absolute_paths,
            )
            for item in items
        ]
    truncation["unsupported"] += 1
    return "<unsupported-value>"


def _is_absolute_path_text(value: str) -> bool:
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _bounded_text(
    value: str,
    *,
    max_string_chars: int,
    truncation: dict[str, int],
) -> str:
    if len(value) <= max_string_chars:
        return value
    truncation["strings"] += 1
    return value[: max(0, max_string_chars - 1)] + "…"
