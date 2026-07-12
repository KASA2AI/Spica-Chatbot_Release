"""Bounded, read-only Catalog for Config Studio managed documents.

The sidecar discovers every path from the resolved production configuration or
from a fixed production fallback.  No HTTP-supplied path reaches this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from spica.config.env_roster import RESPEAKER_ENV_MAP, RUNTIME_CACHE_ENV_MAP
from spica.config.environment_snapshot import EnvironmentSnapshot
from spica.config.manager import ConfigResolution
from spica.config.overlay_owner import (
    OVERLAY_FIELD_SPECS,
    OverlayConfig,
    resolve_overlay_config,
)
from spica.core.character import load_character_package
from spica.ports.config_studio_platform import PlatformCapabilities


_MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
_MAX_STRING_CHARS = 2048
_MAX_COLLECTION_ITEMS = 256
_MAX_DEPTH = 8
_MAX_MANAGED_WIRE_BYTES = 160 * 1024
_REPARSE_POINT = 0x0400
_SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|secret|token|password|cookie|credential)", re.IGNORECASE
)

_ENVIRONMENT_ONLY_OWNERS: tuple[
    tuple[str, Mapping[str, str], str], ...
] = (
    ("runtime_cache", RUNTIME_CACHE_ENV_MAP, "spica.config.runtime_env"),
    ("respeaker", RESPEAKER_ENV_MAP, "spica.config.manager/ReSpeaker consumers"),
)
_ENVIRONMENT_PATH_FIELDS = {
    ("runtime_cache", "cache_root"),
    ("respeaker", "tuning_path"),
}


@dataclass(frozen=True, slots=True)
class FixedFileRead:
    content: bytes | None = field(repr=False)
    status: str
    code: str | None


class ManagedCatalogSnapshot:
    """Internal immutable result; only its explicit wire DTO leaves the service."""

    __slots__ = (
        "_documents",
        "_issues",
        "_readonly_reasons",
        "_song_legacy_path",
    )

    def __init__(
        self,
        *,
        documents: list[dict[str, Any]],
        issues: list[dict[str, str]],
        readonly_reasons: Mapping[tuple[str, ...], str],
        song_legacy_path: Path,
    ) -> None:
        self._documents = tuple(documents)
        self._issues = tuple(MappingProxyType(dict(issue)) for issue in issues)
        self._readonly_reasons = MappingProxyType(dict(readonly_reasons))
        self._song_legacy_path = song_legacy_path

    @property
    def issues(self) -> tuple[Mapping[str, str], ...]:
        return self._issues

    @property
    def readonly_reasons(self) -> Mapping[tuple[str, ...], str]:
        return self._readonly_reasons

    @property
    def song_legacy_path(self) -> Path:
        return self._song_legacy_path

    def to_wire(self) -> list[dict[str, Any]]:
        documents = [dict(document) for document in self._documents]
        payload = {"managed_documents": documents}
        while documents and _encoded_size(payload) > _MAX_MANAGED_WIRE_BYTES:
            fields = documents[-1].get("fields")
            if isinstance(fields, list) and fields:
                fields.pop()
                truncation = documents[-1]["truncation"]
                truncation["total_bytes"] += 1
            else:
                documents.pop()
        return documents


class ManagedDocumentCatalog:
    """Read production-owned character/UI documents without constructing hosts."""

    __slots__ = ("_platform", "_repo_root", "_resolution")

    def __init__(
        self,
        *,
        repo_root: str | Path,
        resolution: ConfigResolution,
        platform_capabilities: PlatformCapabilities,
    ) -> None:
        if not isinstance(platform_capabilities, PlatformCapabilities):
            raise TypeError("platform_capabilities must be PlatformCapabilities")
        self._repo_root = _absolute(Path(repo_root))
        self._resolution = resolution
        self._platform = platform_capabilities

    def snapshot(self) -> ManagedCatalogSnapshot:
        issues: list[dict[str, str]] = []
        readonly_reasons: dict[tuple[str, ...], str] = {}
        legacy_paths = _legacy_owner_paths(self._repo_root)
        song_path = legacy_paths["song"]
        plugins_path = legacy_paths["plugins"]
        screen_path = legacy_paths["screen"]

        if _lexically_exists(plugins_path):
            issues.append(
                _issue(
                    "LEGACY_PLUGINS_DOCUMENT_PRESENT",
                    "Retired plugins.yaml is present; app plugin authoring is read-only.",
                )
            )
            readonly_reasons[("plugins",)] = "legacy_owner_active"
        if _lexically_exists(screen_path):
            issues.append(
                _issue(
                    "LEGACY_SCREEN_DOCUMENT_PRESENT",
                    "Retired screen configuration is present and still affects its owner.",
                )
            )
            readonly_reasons[("screen",)] = "legacy_owner_active"

        effective_song_path = self._absent_song_path()
        if _lexically_exists(song_path):
            issues.append(
                _issue(
                    "LEGACY_SONG_DOCUMENT_PRESENT",
                    "Retired song configuration is present and still affects its owner.",
                )
            )
            readonly_reasons[("song",)] = "legacy_owner_active"
            song_read = read_fixed_regular_file(
                song_path,
                platform_capabilities=self._platform,
            )
            if song_read.content is not None and _valid_json_mapping(song_read.content):
                effective_song_path = song_path

        package, tts, visual = self._character_documents()
        overlay = self._overlay_document()
        return ManagedCatalogSnapshot(
            documents=[package, tts, visual, overlay],
            issues=issues,
            readonly_reasons=readonly_reasons,
            song_legacy_path=effective_song_path,
        )

    def _absent_song_path(self) -> Path:
        return self._repo_root / "spica_data" / "config_studio" / ".no-legacy-song"

    def _character_documents(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        app_config = self._resolution.to_app_config()
        configured_package = app_config.character.package_dir
        package_root = (
            _configured_path(self._repo_root, configured_package)
            if configured_package
            else self._repo_root / "spica_data" / "Spica_skill"
        )
        package_source = "app_config" if configured_package else "default_fallback"
        external_package = _is_external(package_root, self._repo_root)
        directory_status = _inspect_path(package_root, target_kind="directory")
        if directory_status.status in {"unsafe", "unavailable"}:
            package = _document(
                document_id="character_package",
                title="Active character package",
                owner="spica.core.character/CharacterPackage",
                effect_policy="next_spica_launch",
                source_kind=package_source,
                external=external_package,
                data=None,
                health=directory_status,
            )
            blocked = FixedFileRead(
                None,
                "unsafe" if directory_status.status == "unsafe" else "missing",
                "MANAGED_DOCUMENT_UNSAFE"
                if directory_status.status == "unsafe"
                else "MANAGED_DOCUMENT_MISSING",
            )
            return (
                package,
                _document(
                    document_id="character_tts",
                    title="Character TTS data",
                    owner="agent_tools.tts/load_tts_config",
                    effect_policy="next_spica_launch",
                    source_kind="package_blocked",
                    external=external_package,
                    data=None,
                    health=blocked,
                ),
                _document(
                    document_id="character_visual",
                    title="Character visual data",
                    owner="agent_tools.visual/VisualDiffService",
                    effect_policy="owner_mtime_reload",
                    source_kind="package_blocked",
                    external=external_package,
                    data=None,
                    health=blocked,
                ),
            )

        if external_package:
            external_health = FixedFileRead(
                None,
                "external_read_only",
                "EXTERNAL_DOCUMENT_READ_ONLY",
            )
            return (
                _document(
                    document_id="character_package",
                    title="Active character package",
                    owner="spica.core.character/CharacterPackage",
                    effect_policy="next_spica_launch",
                    source_kind=package_source,
                    external=True,
                    data=None,
                    health=external_health,
                    basename="meta.json",
                ),
                _document(
                    document_id="character_tts",
                    title="Character TTS data",
                    owner="agent_tools.tts/load_tts_config",
                    effect_policy="next_spica_launch",
                    source_kind="external_package_unresolved",
                    external=True,
                    data=None,
                    health=external_health,
                ),
                _document(
                    document_id="character_visual",
                    title="Character visual data",
                    owner="agent_tools.visual/VisualDiffService",
                    effect_policy="owner_mtime_reload",
                    source_kind="external_package_unresolved",
                    external=True,
                    data=None,
                    health=external_health,
                ),
            )

        meta_path = package_root / "meta.json"
        meta_read = read_fixed_regular_file(
            meta_path,
            platform_capabilities=self._platform,
        )
        meta_data = _parse_mapping(meta_read, suffix=".json")
        package = _document(
            document_id="character_package",
            title="Active character package",
            owner="spica.core.character/CharacterPackage",
            effect_policy="next_spica_launch",
            source_kind=package_source,
            external=external_package,
            data=meta_data,
            health=_parse_health(meta_read, meta_data),
            basename=meta_path.name,
        )

        if meta_read.status in {"unsafe", "unavailable"} or (
            meta_read.code == "MANAGED_DOCUMENT_TOO_LARGE"
        ):
            blocked = FixedFileRead(None, "unsafe", "MANAGED_DOCUMENT_UNSAFE")
            return (
                package,
                _document(
                    document_id="character_tts",
                    title="Character TTS data",
                    owner="agent_tools.tts/load_tts_config",
                    effect_policy="next_spica_launch",
                    source_kind="package_blocked",
                    external=external_package,
                    data=None,
                    health=blocked,
                ),
                _document(
                    document_id="character_visual",
                    title="Character visual data",
                    owner="agent_tools.visual/VisualDiffService",
                    effect_policy="owner_mtime_reload",
                    source_kind="package_blocked",
                    external=external_package,
                    data=None,
                    health=blocked,
                ),
            )

        try:
            if meta_data is None:
                if meta_read.status == "missing" and not configured_package:
                    owner_package = load_character_package(package_root)
                else:
                    owner_package = None
            else:
                owner_package = load_character_package(package_root)
        except (AttributeError, OSError, TypeError, ValueError):
            owner_package = None
        if owner_package is None:
            blocked = FixedFileRead(None, "invalid", "MANAGED_DOCUMENT_INVALID")
            return (
                package,
                _document(
                    document_id="character_tts",
                    title="Character TTS data",
                    owner="agent_tools.tts/load_tts_config",
                    effect_policy="next_spica_launch",
                    source_kind="package_blocked",
                    external=external_package,
                    data=None,
                    health=blocked,
                ),
                _document(
                    document_id="character_visual",
                    title="Character visual data",
                    owner="agent_tools.visual/VisualDiffService",
                    effect_policy="owner_mtime_reload",
                    source_kind="package_blocked",
                    external=external_package,
                    data=None,
                    health=blocked,
                ),
            )

        tts_override = owner_package.tts_config_path
        visual_override = owner_package.visual_config_path
        tts_path = (
            _absolute(Path(tts_override))
            if tts_override
            else self._repo_root / "data" / "config" / "tts.yaml"
        )
        visual_path = (
            _absolute(Path(visual_override))
            if visual_override
            else self._repo_root / "data" / "config" / "visual.yaml"
        )
        return (
            package,
            self._config_document(
                document_id="character_tts",
                title="Character TTS data",
                owner="agent_tools.tts/load_tts_config",
                effect_policy="next_spica_launch",
                source_kind="package_override" if tts_override else "default_fallback",
                path=tts_path,
            ),
            self._config_document(
                document_id="character_visual",
                title="Character visual data",
                owner="agent_tools.visual/VisualDiffService",
                effect_policy="owner_mtime_reload",
                source_kind=(
                    "package_override" if visual_override else "default_fallback"
                ),
                path=visual_path,
            ),
        )

    def _config_document(
        self,
        *,
        document_id: str,
        title: str,
        owner: str,
        effect_policy: str,
        source_kind: str,
        path: Path,
    ) -> dict[str, Any]:
        external = _is_external(path, self._repo_root)
        if external:
            return _document(
                document_id=document_id,
                title=title,
                owner=owner,
                effect_policy=effect_policy,
                source_kind=source_kind,
                external=True,
                data=None,
                health=FixedFileRead(
                    None,
                    "external_read_only",
                    "EXTERNAL_DOCUMENT_READ_ONLY",
                ),
                basename=path.name,
            )
        read = read_fixed_regular_file(
            path,
            platform_capabilities=self._platform,
        )
        data = _parse_mapping(read, suffix=path.suffix.lower())
        return _document(
            document_id=document_id,
            title=title,
            owner=owner,
            effect_policy=effect_policy,
            source_kind=source_kind,
            external=False,
            data=data,
            health=_parse_health(read, data),
            basename=path.name,
        )

    def _overlay_document(self) -> dict[str, Any]:
        path = self._repo_root / "ui" / "overlay_config.json"
        read = read_fixed_regular_file(
            path,
            platform_capabilities=self._platform,
        )
        raw = _parse_mapping(read, suffix=".json")
        raw_mapping = raw if isinstance(raw, Mapping) else {}
        resolved = resolve_overlay_config(raw_mapping)
        current = {
            name: getattr(resolved, name)
            for name in OverlayConfig.__dataclass_fields__
        }
        health = _parse_health(read, raw)
        document = _document(
            document_id="overlay_preferences",
            title="Overlay preferences",
            owner="spica.config.overlay_owner/OverlayConfig",
            effect_policy="next_spica_launch",
            source_kind="ui_owner_document",
            external=False,
            data=current,
            health=health,
            defaults={
                name: spec.default for name, spec in OVERLAY_FIELD_SPECS.items()
            },
            basename=path.name,
        )
        document["editable"] = True
        document["unsupported_reason"] = None
        for field in document["fields"]:
            spec = OVERLAY_FIELD_SPECS.get(field["display_path"])
            if spec is None:
                continue
            field.update(
                {
                    "control": "number",
                    "minimum": spec.minimum,
                    "maximum": spec.maximum,
                    "editable": True,
                    "unsupported_reason": None,
                }
            )
        return document


def environment_only_settings(
    environment_snapshot: EnvironmentSnapshot,
) -> list[dict[str, Any]]:
    """Project rostered owners that deliberately have no ``app.yaml`` field.

    Values remain raw owner inputs rather than being mislabeled as resolved
    ``AppConfig`` leaves. Path-valued inputs are never returned to the browser.
    """

    if not isinstance(environment_snapshot, EnvironmentSnapshot):
        raise TypeError("environment_snapshot must be EnvironmentSnapshot")
    settings: list[dict[str, Any]] = []
    for domain, roster, owner in _ENVIRONMENT_ONLY_OWNERS:
        for field_name, environment_variable in roster.items():
            raw_value = environment_snapshot.get(environment_variable)
            tainted = environment_snapshot.is_tainted(environment_variable)
            configured = tainted or bool(raw_value)
            settings.append(
                {
                    "id": f"{domain}.{field_name}",
                    "environment_variable": environment_variable,
                    "configured": configured,
                    "configured_value": (
                        None
                        if not configured or tainted
                        else "<external-path>"
                        if (domain, field_name) in _ENVIRONMENT_PATH_FIELDS
                        else _bounded_environment_value(raw_value)
                    ),
                    "source_kind": (
                        "secret_tainted_env_override"
                        if tainted
                        else "env_override"
                        if configured
                        else "default"
                    ),
                    "environment_layer": (
                        environment_snapshot.layer_for(environment_variable)
                        if configured
                        else None
                    ),
                    "owner": owner,
                    "effect_policy": "next_spica_launch",
                    "editable": False,
                    "unsupported_reason": "no_app_yaml_owner",
                }
            )
    return settings


def _bounded_environment_value(value: str | None) -> str | None:
    if value is not None and os.path.isabs(value):
        return "<external-path>"
    if value is None or len(value) <= _MAX_STRING_CHARS:
        return value
    return value[: _MAX_STRING_CHARS - 1] + "…"


def plugin_statuses(
    *,
    repo_root: str | Path,
    resolution: ConfigResolution,
    legacy_owner_active: bool,
) -> list[dict[str, Any]]:
    """Inspect configured packages without importing or reading plugin code."""

    root = _absolute(Path(repo_root))
    statuses: list[dict[str, Any]] = []
    for entry in resolution.to_app_config().plugins[:_MAX_COLLECTION_ITEMS]:
        package_status, health_code = _plugin_package_health(root, entry.name)
        projected_name = (
            entry.name
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", entry.name)
            is not None
            else "<invalid-plugin-name>"
        )
        statuses.append(
            {
                "name": projected_name,
                "configured": True,
                "next_launch_enabled": (
                    None if legacy_owner_active else entry.enabled
                ),
                "package_status": package_status,
                "package_health_code": health_code,
                "owner": "spica.plugins.manifest",
                "effect_policy": "next_spica_launch",
            }
        )
    return statuses


def _plugin_package_health(repo_root: Path, name: str) -> tuple[str, str]:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name) is None:
        return "unsafe", "PLUGIN_PACKAGE_UNSAFE"
    package = repo_root / "plugins" / name
    package_health = _inspect_path(package, target_kind="directory")
    init_health = _inspect_path(package / "__init__.py", target_kind="file")
    if "unsafe" in {package_health.status, init_health.status}:
        return "unsafe", "PLUGIN_PACKAGE_UNSAFE"
    if package_health.status != "healthy" or init_health.status != "healthy":
        return "missing", "PLUGIN_PACKAGE_MISSING"
    return "present", "PLUGIN_PACKAGE_PRESENT"


def _document(
    *,
    document_id: str,
    title: str,
    owner: str,
    effect_policy: str,
    source_kind: str,
    external: bool,
    data: Mapping[str, Any] | None,
    health: FixedFileRead,
    defaults: Mapping[str, Any] | None = None,
    basename: str | None = None,
) -> dict[str, Any]:
    truncation = {
        "strings": 0,
        "collections": 0,
        "depth": 0,
        "unsupported": 0,
        "total_bytes": 0,
    }
    fields: list[dict[str, Any]] = []
    if data is not None and not external:
        for path, value in _dynamic_leaves(data, truncation=truncation):
            key_hint = str(path[-1]) if path else ""
            fields.append(
                {
                    "path": [_path_segment(part) for part in path],
                    "display_path": _display_path(path),
                    "current_value": _bounded_value(
                        value,
                        key_hint=key_hint,
                        depth=0,
                        truncation=truncation,
                    ),
                    "default_value": _bounded_value(
                        defaults.get(key_hint) if defaults else None,
                        key_hint=key_hint,
                        depth=0,
                        truncation=truncation,
                    ),
                    "value_type": _value_type(value),
                    "owner": owner,
                    "effect_policy": effect_policy,
                    "editable": False,
                    "unsupported_reason": "owner_schema_unavailable",
                }
            )
    document = {
        "id": document_id,
        "title": title,
        "category": "ui" if document_id == "overlay_preferences" else "character",
        "owner": owner,
        "effect_policy": effect_policy,
        "source_kind": source_kind,
        "external": external,
        "basename": basename,
        "editable": False,
        "unsupported_reason": (
            "external_read_only" if external else "owner_schema_unavailable"
        ),
        "health": {"status": health.status, "code": health.code},
        "fields": fields,
        "truncation": truncation,
    }
    while fields and _encoded_size(document) > _MAX_MANAGED_WIRE_BYTES:
        fields.pop()
        truncation["total_bytes"] += 1
    return document


def _dynamic_leaves(
    value: Any,
    *,
    prefix: tuple[str | int, ...] = (),
    depth: int = 0,
    truncation: dict[str, int],
) -> list[tuple[tuple[str | int, ...], Any]]:
    if depth > _MAX_DEPTH:
        truncation["depth"] += 1
        return [(prefix, "<depth-limit>")]
    if isinstance(value, Mapping):
        items = list(value.items())
        if len(items) > _MAX_COLLECTION_ITEMS:
            items = items[:_MAX_COLLECTION_ITEMS]
            truncation["collections"] += 1
        if not items:
            return [(prefix, {})]
        leaves: list[tuple[tuple[str | int, ...], Any]] = []
        for key, item in items:
            leaves.extend(
                _dynamic_leaves(
                    item,
                    prefix=prefix + (str(key),),
                    depth=depth + 1,
                    truncation=truncation,
                )
            )
        return leaves
    if isinstance(value, list):
        items = value
        if len(items) > _MAX_COLLECTION_ITEMS:
            items = items[:_MAX_COLLECTION_ITEMS]
            truncation["collections"] += 1
        if not items:
            return [(prefix, [])]
        leaves = []
        for index, item in enumerate(items):
            leaves.extend(
                _dynamic_leaves(
                    item,
                    prefix=prefix + (index,),
                    depth=depth + 1,
                    truncation=truncation,
                )
            )
        return leaves
    return [(prefix, value)]


def _bounded_value(
    value: Any,
    *,
    key_hint: str,
    depth: int,
    truncation: dict[str, int],
) -> Any:
    if _SECRET_KEY_RE.search(key_hint):
        return "<redacted>"
    if depth > _MAX_DEPTH:
        truncation["depth"] += 1
        return "<depth-limit>"
    if isinstance(value, str):
        if os.path.isabs(value):
            return "<external-path>"
        if len(value) > _MAX_STRING_CHARS:
            truncation["strings"] += 1
            return value[: _MAX_STRING_CHARS - 1] + "…"
        return value
    if type(value) is float and not math.isfinite(value):
        truncation["unsupported"] += 1
        return "<non-finite-number>"
    if value is None or type(value) in (bool, int, float):
        return value
    if isinstance(value, Mapping):
        items = list(value.items())
        if len(items) > _MAX_COLLECTION_ITEMS:
            items = items[:_MAX_COLLECTION_ITEMS]
            truncation["collections"] += 1
        return {
            str(key): _bounded_value(
                item,
                key_hint=str(key),
                depth=depth + 1,
                truncation=truncation,
            )
            for key, item in items
        }
    if isinstance(value, (list, tuple)):
        items = list(value)
        if len(items) > _MAX_COLLECTION_ITEMS:
            items = items[:_MAX_COLLECTION_ITEMS]
            truncation["collections"] += 1
        return [
            _bounded_value(
                item,
                key_hint=key_hint,
                depth=depth + 1,
                truncation=truncation,
            )
            for item in items
        ]
    truncation["unsupported"] += 1
    return "<unsupported-value>"


def _inspect_path(path: Path, *, target_kind: str) -> FixedFileRead:
    target = _absolute(path)
    chain = list(reversed(target.parents)) + [target]
    for index, component in enumerate(chain):
        if str(component) == component.anchor:
            continue
        try:
            info = component.lstat()
        except FileNotFoundError:
            return FixedFileRead(None, "missing", "MANAGED_DOCUMENT_MISSING")
        except OSError:
            return FixedFileRead(None, "unavailable", "MANAGED_DOCUMENT_UNAVAILABLE")
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
        ):
            return FixedFileRead(None, "unsafe", "MANAGED_DOCUMENT_UNSAFE")
        is_target = index == len(chain) - 1
        if is_target:
            valid = (
                stat.S_ISDIR(info.st_mode)
                if target_kind == "directory"
                else stat.S_ISREG(info.st_mode)
            )
            if not valid:
                return FixedFileRead(None, "unsafe", "MANAGED_DOCUMENT_UNSAFE")
        elif not stat.S_ISDIR(info.st_mode):
            return FixedFileRead(None, "unsafe", "MANAGED_DOCUMENT_UNSAFE")
    return FixedFileRead(b"" if target_kind == "file" else None, "healthy", None)


def read_fixed_regular_file(
    path: Path,
    *,
    platform_capabilities: PlatformCapabilities,
) -> FixedFileRead:
    if not isinstance(platform_capabilities, PlatformCapabilities):
        raise TypeError("platform_capabilities must be PlatformCapabilities")
    inspected = _inspect_path(path, target_kind="file")
    if inspected.status != "healthy":
        return inspected
    target = _absolute(path)
    try:
        before = target.lstat()
    except OSError:
        return FixedFileRead(None, "unavailable", "MANAGED_DOCUMENT_UNAVAILABLE")
    if not _fixed_file_identity_is_safe(before, platform_capabilities):
        return FixedFileRead(None, "unsafe", "MANAGED_DOCUMENT_UNSAFE")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
        try:
            opened = os.fstat(descriptor)
            if not _fixed_file_identity_is_safe(opened, platform_capabilities):
                return FixedFileRead(None, "unsafe", "MANAGED_DOCUMENT_UNSAFE")
            if opened.st_size > _MAX_DOCUMENT_BYTES:
                return FixedFileRead(None, "invalid", "MANAGED_DOCUMENT_TOO_LARGE")
            chunks: list[bytes] = []
            remaining = _MAX_DOCUMENT_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
        content = b"".join(chunks)
        after = target.lstat()
    except OSError:
        return FixedFileRead(None, "unavailable", "MANAGED_DOCUMENT_UNAVAILABLE")
    if len(content) > _MAX_DOCUMENT_BYTES:
        return FixedFileRead(None, "invalid", "MANAGED_DOCUMENT_TOO_LARGE")
    if (
        (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
        or not _fixed_file_identity_is_safe(after, platform_capabilities)
    ):
        return FixedFileRead(None, "unsafe", "MANAGED_DOCUMENT_UNSAFE")
    rechecked = _inspect_path(target, target_kind="file")
    if rechecked.status != "healthy":
        return rechecked
    return FixedFileRead(content, "healthy", None)


def _fixed_file_identity_is_safe(
    file_stat: os.stat_result,
    platform: PlatformCapabilities,
) -> bool:
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
        return False
    if platform.posix_permissions:
        return platform.user_id is not None and file_stat.st_uid == platform.user_id
    return True


def _parse_mapping(read: FixedFileRead, *, suffix: str) -> dict[str, Any] | None:
    if read.content is None:
        return None
    try:
        text = read.content.decode("utf-8")
        loaded = (
            yaml.safe_load(text)
            if suffix in {".yaml", ".yml"}
            else json.loads(text)
        )
    except (UnicodeError, ValueError, RecursionError, yaml.YAMLError):
        return None
    if loaded is None:
        return {}
    return dict(loaded) if isinstance(loaded, Mapping) else None


def _parse_health(
    read: FixedFileRead,
    parsed: Mapping[str, Any] | None,
) -> FixedFileRead:
    if read.status != "healthy":
        return read
    if parsed is None:
        return FixedFileRead(None, "invalid", "MANAGED_DOCUMENT_INVALID")
    return FixedFileRead(None, "healthy", None)


def _valid_json_mapping(content: bytes) -> bool:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(value, Mapping)


def _configured_path(repo_root: Path, value: str) -> Path:
    path = Path(value)
    return _absolute(path if path.is_absolute() else repo_root / path)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_external(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_absolute(path), _absolute(root))) != str(
            _absolute(root)
        )
    except ValueError:
        return True


def _lexically_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _legacy_owner_paths(repo_root: Path) -> dict[str, Path]:
    return {
        "plugins": repo_root / "data" / "config" / "plugins.yaml",
        "screen": repo_root / "config" / "screen_vision_config.json",
        "song": (
            repo_root
            / "agent_tools"
            / "function_tools"
            / "song"
            / "song_config.json"
        ),
    }


def active_legacy_owner_prefixes(
    repo_root: str | Path,
) -> frozenset[str]:
    """Return fixed app sections still shadowed by retired owner documents."""

    root = _absolute(Path(repo_root))
    return frozenset(
        prefix
        for prefix, path in _legacy_owner_paths(root).items()
        if _lexically_exists(path)
    )


def _path_segment(part: str | int) -> dict[str, Any]:
    if isinstance(part, int):
        return {"kind": "list_index", "index": part}
    return {"kind": "map_key", "key": part}


def _display_path(path: tuple[str | int, ...]) -> str:
    rendered = ""
    for part in path:
        if isinstance(part, int):
            rendered += f"[{part}]"
        else:
            rendered += ("." if rendered else "") + part
    return rendered


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float:
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "mapping"
    if isinstance(value, (list, tuple)):
        return "list"
    return "unsupported"


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _encoded_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


__all__ = [
    "FixedFileRead",
    "ManagedDocumentCatalog",
    "active_legacy_owner_prefixes",
    "environment_only_settings",
    "plugin_statuses",
    "read_fixed_regular_file",
]
