"""FastAPI application boundary for the local Config Studio."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any, Mapping, Protocol, Sequence

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from spica.config.env_roster import APP_ENV_MAP, SCREEN_ENV_MAP, SECRETS_ENV_MAP
from spica.config_studio.assets import load_static_ui_assets
from spica.config_studio.authoring import AuthoringOperation, SetValue, UnsetValue
from spica.config_studio.overlay_contract import OverlaySetValue
from spica.config_studio.paths import (
    ConfigFieldPath,
    FieldSegment,
    ListIndexSegment,
    MapKeySegment,
)
from spica.config_studio.security import (
    BOOTSTRAP_HEADER_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    SecurityContext,
)
from spica.config_studio.services import ConfigStudioServiceError
from spica.config_studio.sensitive_env import (
    ClearMappedOverride,
    ClearSecret,
    SetSecret,
)
from spica.config_studio.self_check import (
    HEAVY_CHECKS,
    LIGHT_CHECKS,
    SelfCheckJobError,
    SelfCheckJobStatus,
    SelfCheckMode,
    SelfCheckPlanError,
)
from spica.config_studio.self_check_service import SelfCheckAcknowledgements


_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": (
        "camera=(), display-capture=(), geolocation=(), microphone=(), "
        "payment=(), usb=()"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'none'; object-src 'none'; connect-src 'self'; "
        "img-src 'self' data:; style-src 'self'; script-src 'self'"
    ),
}


OverlaySetValueRequest = OverlaySetValue


class ConfigStudioServices(Protocol):
    def meta(self) -> Mapping[str, Any]: ...

    def catalog(self) -> Mapping[str, Any]: ...

    def capability_enabled(self, capability: str) -> bool: ...

    def self_check_jobs_available(self) -> bool: ...

    def preview_app(
        self,
        operations: tuple[AuthoringOperation, ...],
        *,
        session_id: str,
    ) -> Mapping[str, Any]: ...

    def commit_app_preview(
        self,
        preview_id: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]: ...

    def list_app_restore_points(
        self,
        *,
        session_id: str,
    ) -> Sequence[Mapping[str, Any]]: ...

    def prepare_app_rollback(
        self,
        restore_point_id: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]: ...

    def rollback_app(
        self,
        confirmation_receipt: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]: ...

    def preview_overlay(
        self,
        command: OverlaySetValueRequest,
        *,
        session_id: str,
    ) -> Mapping[str, Any]: ...

    def commit_overlay_preview(
        self,
        preview_id: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]: ...

    def list_overlay_restore_points(
        self,
        *,
        session_id: str,
    ) -> Sequence[Mapping[str, Any]]: ...

    def prepare_overlay_rollback(
        self,
        restore_point_id: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]: ...

    def rollback_overlay(
        self,
        confirmation_receipt: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]: ...

    def sensitive_status(self, *, session_id: str) -> Mapping[str, Any]: ...

    def preview_sensitive(
        self,
        command: ClearMappedOverride | SetSecret | ClearSecret,
        *,
        session_id: str,
    ) -> Mapping[str, Any]: ...

    def confirm_sensitive_secret_clear(
        self,
        preview_id: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]: ...

    def commit_sensitive_preview(
        self,
        preview_id: str,
        confirmation_receipt: str | None,
        *,
        session_id: str,
    ) -> Mapping[str, Any]: ...

    def list_sensitive_restore_points(
        self,
        *,
        session_id: str,
    ) -> Sequence[Mapping[str, Any]]: ...

    def prepare_sensitive_rollback(
        self,
        restore_point_id: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]: ...

    def rollback_sensitive(
        self,
        confirmation_receipt: str,
        *,
        session_id: str,
    ) -> Mapping[str, Any]: ...

    def start_self_check(
        self, command: SelfCheckStartRequest
    ) -> Mapping[str, Any]: ...

    def prepare_heavy_self_check(
        self,
        command: SelfCheckStartRequest,
        *,
        acknowledgements: SelfCheckAcknowledgements,
        session_id: str,
    ) -> Mapping[str, Any]: ...

    def start_confirmed_self_check(
        self,
        command: SelfCheckStartRequest,
        *,
        session_id: str,
        confirmation_receipt: str,
    ) -> Mapping[str, Any]: ...

    def list_self_checks(self) -> Sequence[Mapping[str, Any]]: ...

    def get_self_check(self, job_id: str) -> Mapping[str, Any]: ...

    def cancel_self_check(self, job_id: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class SelfCheckStartRequest:
    mode: SelfCheckMode = SelfCheckMode.LIGHT
    only: tuple[str, ...] = ()
    llm: bool = False
    include_disabled: bool = False
    allow_model_downloads: bool = False


_SELF_CHECK_COMMAND_KEYS = frozenset(
    {
        "mode",
        "only",
        "llm",
        "include_disabled",
        "allow_model_downloads",
    }
)
_SELF_CHECK_START_KEYS = _SELF_CHECK_COMMAND_KEYS | {"confirmation_receipt"}
_SELF_CHECK_ACKNOWLEDGEMENT_KEYS = frozenset(
    {"full", "llm", "include_disabled", "model_downloads"}
)
_SELF_CHECK_MODES = frozenset(item.value for item in SelfCheckMode)
_SELF_CHECK_JOB_KEYS = (
    "job_id",
    "mode",
    "checks",
    "status",
    "duration_s",
    "results",
    "progress",
    "error_code",
    "stderr_line_count",
    "stderr_total_line_count",
    "stderr_truncated",
)
_SELF_CHECK_RESULT_STATUSES = frozenset(
    {"PASS", "UNVERIFIED", "DEGRADED", "FAIL", "SKIPPED_DISABLED"}
)
_SELF_CHECK_JOB_STATUSES = frozenset(item.value for item in SelfCheckJobStatus)
_SELF_CHECK_WIRE_BUDGET = 64 * 1024
_SELF_CHECK_COLLECTION_BUDGET = 256 * 1024
_SELF_CHECK_COLLECTION_MAX_JOBS = 21  # one active plus 20 retained terminals
_SECRET_KEY_PARTS = frozenset(
    {"secret", "token", "password", "authorization", "cookie", "credential"}
)
_SELF_CHECK_CONFIRMATION_ERRORS = frozenset(
    {
        "FULL_CONFIRMATION_REQUIRED",
        "LLM_CONFIRMATION_REQUIRED",
        "INCLUDE_DISABLED_CONFIRMATION_REQUIRED",
        "MODEL_DOWNLOAD_CONFIRMATION_REQUIRED",
        "SELF_CHECK_CONFIRMATION_INVALID",
        "SELF_CHECK_CONFIRMATION_MISMATCH",
        "SELF_CHECK_CONFIRMATION_EXPIRED",
    }
)
_SELF_CHECK_UNAVAILABLE_ERRORS = frozenset(
    {
        "SELF_CHECK_MANAGER_UNSAFE",
        "SELF_CHECK_MANAGER_SHUTDOWN",
        "SELF_CHECK_JOB_ID_UNAVAILABLE",
        "INVALID_CHILD_ENVIRONMENT",
    }
)
_OPAQUE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_SLOT_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ENVIRONMENT_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_CONFIG_PATH = re.compile(
    r"[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*){0,15}\Z"
)
_STABLE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_SOURCE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MANAGED_OVERRIDE_NAMES = frozenset(
    (*APP_ENV_MAP.values(), *SCREEN_ENV_MAP.values())
)
_MAX_REQUEST_BYTES = 64 * 1024
_MAX_JSON_STRING = 16 * 1024
_MAX_JSON_ITEMS = 128
_MAX_JSON_DEPTH = 8
_MAX_ROLLBACK_FIELDS = 128

_SERVICE_ERROR_STATUS = {
    "CAPABILITY_UNAVAILABLE": 403,
    "CONFIRMATION_UNAVAILABLE": 503,
    "CONFIRMATION_REQUIRED": 409,
    "DOCUMENT_BUSY": 423,
    "DOCUMENT_CONFLICT": 409,
    "DOCUMENT_INVALID": 400,
    "DOCUMENT_UNSAFE": 409,
    "DOTENV_INVALID": 409,
    "ENVIRONMENT_REFRESH_UNAVAILABLE": 503,
    "NO_VALID_RESTORE_POINT": 409,
    "PERMISSION_HARDENING_FAILED": 503,
    "PREVIEW_UNAVAILABLE": 503,
    "RECOVERY_ONLY": 409,
    "SENSITIVE_WRITES_UNVERIFIED_ON_WINDOWS": 503,
    "UNKNOWN_FIELD": 400,
    "WRITES_UNVERIFIED_ON_WINDOWS": 503,
}


class _WireError(ValueError):
    pass


class _JsonContentTypeError(ValueError):
    pass


def _session_identity(session_token: str) -> str:
    digest = hashlib.sha256(
        b"spica-config-studio-session\0" + session_token.encode("utf-8")
    ).hexdigest()
    return f"session_{digest}"


def _session_id(request: Request) -> str:
    identity = getattr(request.state, "config_studio_session_id", None)
    if not isinstance(identity, str) or not identity:
        raise RuntimeError("authenticated session identity is unavailable")
    return identity


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _WireError("duplicate JSON member")
        result[key] = value
    return result


async def _bounded_json_payload(request: Request) -> object:
    if not _is_utf8_application_json(_single_header(request, "content-type")):
        raise _JsonContentTypeError("request content type is not supported")
    content_length = _single_header(request, "content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
            if declared_length < 0 or declared_length > _MAX_REQUEST_BYTES:
                raise _WireError("request body is too large")
        except ValueError as exc:
            raise _WireError("invalid content length") from exc
    body_buffer = bytearray()
    async for chunk in request.stream():
        if len(body_buffer) + len(chunk) > _MAX_REQUEST_BYTES:
            raise _WireError("request body is too large")
        body_buffer.extend(chunk)
    body = bytes(body_buffer)
    if not body:
        raise _WireError("request body is empty or too large")
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                _WireError("non-finite JSON number")
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _WireError("invalid JSON") from exc
    _bounded_json_value(payload)
    return payload


def _is_utf8_application_json(value: str | None) -> bool:
    if value is None:
        return False
    parts = [part.strip() for part in value.split(";")]
    if not parts or parts[0].lower() != "application/json":
        return False
    parameters: dict[str, str] = {}
    for part in parts[1:]:
        name, separator, parameter_value = part.partition("=")
        name = name.strip().lower()
        parameter_value = parameter_value.strip().strip('"').lower()
        if (
            not separator
            or name != "charset"
            or name in parameters
            or parameter_value != "utf-8"
        ):
            return False
        parameters[name] = parameter_value
    return True


def _bounded_json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > _MAX_JSON_DEPTH:
        raise _WireError("JSON nesting is too deep")
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _WireError("number must be finite")
        return value
    if isinstance(value, str):
        if len(value) > _MAX_JSON_STRING or "\x00" in value:
            raise _WireError("string is invalid")
        return value
    if isinstance(value, list):
        if len(value) > _MAX_JSON_ITEMS:
            raise _WireError("collection is too large")
        return [_bounded_json_value(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        if len(value) > _MAX_JSON_ITEMS:
            raise _WireError("collection is too large")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128 or "\x00" in key:
                raise _WireError("object key is invalid")
            result[key] = _bounded_json_value(item, depth=depth + 1)
        return result
    raise _WireError("unsupported JSON value")


def _opaque_id(value: object) -> str:
    if not isinstance(value, str) or _OPAQUE_ID.fullmatch(value) is None:
        raise _WireError("opaque identifier is invalid")
    return value


def _app_operations(payload: object) -> tuple[AuthoringOperation, ...]:
    if not isinstance(payload, dict) or set(payload) != {"operations"}:
        raise _WireError("app preview request is invalid")
    operations = payload["operations"]
    if not isinstance(operations, list) or not 1 <= len(operations) <= 64:
        raise _WireError("app operations are invalid")
    parsed: list[AuthoringOperation] = []
    for operation in operations:
        if not isinstance(operation, dict):
            raise _WireError("app operation is invalid")
        kind = operation.get("kind")
        if kind == "set" and set(operation) == {"kind", "path", "value"}:
            parsed.append(
                SetValue(
                    path=_config_field_path(operation["path"]),
                    value=_bounded_json_value(operation["value"]),
                )
            )
        elif kind == "unset" and set(operation) == {"kind", "path"}:
            parsed.append(UnsetValue(path=_config_field_path(operation["path"])))
        else:
            raise _WireError("app operation kind is invalid")
    return tuple(parsed)


def _config_field_path(value: object) -> ConfigFieldPath:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise _WireError("typed path is invalid")
    segments = []
    for item in value:
        if not isinstance(item, dict) or "kind" not in item:
            raise _WireError("typed path segment is invalid")
        kind = item["kind"]
        if kind == "field" and set(item) == {"kind", "name"}:
            name = item["name"]
            if not isinstance(name, str) or _FIELD_NAME.fullmatch(name) is None:
                raise _WireError("field segment is invalid")
            segments.append(FieldSegment(name))
        elif kind == "map_key" and set(item) == {"kind", "key"}:
            key = item["key"]
            if not isinstance(key, str) or not key or len(key) > 128 or "\x00" in key:
                raise _WireError("map key segment is invalid")
            segments.append(MapKeySegment(key))
        elif kind == "list_index" and set(item) == {"kind", "index"}:
            index = item["index"]
            if type(index) is not int or not 0 <= index <= 4095:
                raise _WireError("list index segment is invalid")
            segments.append(ListIndexSegment(index))
        else:
            raise _WireError("typed path segment is invalid")
    return ConfigFieldPath(tuple(segments))


def _typed_path_dto(value: object) -> list[dict[str, Any]]:
    path = _config_field_path(value)
    result: list[dict[str, Any]] = []
    for segment in path.segments:
        if isinstance(segment, FieldSegment):
            result.append({"kind": "field", "name": segment.name})
        elif isinstance(segment, MapKeySegment):
            result.append({"kind": "map_key", "key": segment.key})
        else:
            result.append({"kind": "list_index", "index": segment.index})
    return result


def _required_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("service returned an invalid DTO")
    return value


def _bounded_service_string(value: object, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise TypeError("service returned an invalid string")
    return value


def _app_preview_dto(value: object) -> dict[str, Any]:
    preview = _required_mapping(value)
    changes = preview.get("changes")
    if not isinstance(changes, Sequence) or isinstance(changes, (str, bytes)):
        raise TypeError("service returned invalid app changes")
    if len(changes) > 64:
        raise TypeError("service returned too many app changes")
    rendered_changes: list[dict[str, Any]] = []
    for item in changes:
        change = _required_mapping(item)
        warning = change.get("semantic_warning")
        if warning is not None:
            warning = _bounded_service_string(warning, maximum=1024)
        rendered_changes.append(
            {
                "path": _typed_path_dto(change.get("path")),
                "display_path": _bounded_service_string(
                    change.get("display_path"), maximum=256
                ),
                "file_value_before": _bounded_json_value(
                    change.get("file_value_before")
                ),
                "file_value_after": _bounded_json_value(
                    change.get("file_value_after")
                ),
                "next_launch_value_before": _bounded_json_value(
                    change.get("next_launch_value_before")
                ),
                "next_launch_value_after": _bounded_json_value(
                    change.get("next_launch_value_after")
                ),
                "source_before": _bounded_service_string(
                    change.get("source_before"), maximum=64
                ),
                "source_after": _bounded_service_string(
                    change.get("source_after"), maximum=64
                ),
                "file_value_shadowed": (
                    change.get("file_value_shadowed")
                    if type(change.get("file_value_shadowed")) is bool
                    else _raise_type_error("invalid shadow flag")
                ),
                "semantic_warning": warning,
            }
        )
    if type(preview.get("changed")) is not bool:
        raise TypeError("service returned invalid changed flag")
    effect_policy = preview.get("effect_policy")
    if effect_policy not in {"next_spica_launch", "owner_mtime_reload"}:
        raise TypeError("service returned invalid effect policy")
    return {
        "preview_id": _opaque_id(preview.get("preview_id")),
        "changed": preview["changed"],
        "effect_policy": effect_policy,
        "changes": rendered_changes,
    }


def _raise_type_error(message: str) -> Any:
    raise TypeError(message)


def _app_commit_dto(value: object) -> dict[str, Any]:
    commit = _required_mapping(value)
    status = commit.get("status")
    if status not in {"saved", "unchanged"}:
        raise TypeError("service returned invalid commit status")
    effect_policy = commit.get("effect_policy")
    if effect_policy not in {"next_spica_launch", "owner_mtime_reload"}:
        raise TypeError("service returned invalid effect policy")
    restore_point_id = commit.get("restore_point_id")
    if restore_point_id is not None:
        restore_point_id = _opaque_id(restore_point_id)
    maintenance_code = commit.get("maintenance_code")
    if maintenance_code is not None:
        maintenance_code = _bounded_service_string(maintenance_code, maximum=128)
    return {
        "status": status,
        "effect_policy": effect_policy,
        "restore_point_id": restore_point_id,
        "maintenance_code": maintenance_code,
    }


def _ordinary_restore_points_dto(value: object) -> dict[str, Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("service returned invalid restore points")
    if len(value) > 16:
        raise TypeError("service returned too many restore points")
    points = []
    for item in value:
        point = _required_mapping(item)
        points.append(
            {
                "restore_point_id": _opaque_id(point.get("restore_point_id")),
                "created_at_ns": _nonnegative_int(
                    point.get("created_at_ns"), maximum=10**30
                ),
            }
        )
    return {"restore_points": points}


def _semantic_field_names(value: object) -> tuple[list[str], int]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("service returned invalid rollback fields")
    total = len(value)
    fields: list[str] = []
    seen: set[str] = set()
    for index in range(min(total, _MAX_ROLLBACK_FIELDS)):
        field = _bounded_service_string(value[index], maximum=128)
        if any(character in field for character in ("/", "\\", "\r", "\n")):
            raise TypeError("service returned an invalid rollback field")
        if field in seen:
            raise TypeError("service returned duplicate rollback fields")
        seen.add(field)
        fields.append(field)
    return fields, max(0, total - len(fields))


def _effect_policy(value: object) -> str:
    if value not in {"next_spica_launch", "owner_mtime_reload"}:
        raise TypeError("service returned invalid effect policy")
    return value


def _app_rollback_confirmation_dto(value: object) -> dict[str, Any]:
    confirmation = _required_mapping(value)
    changed_fields, changed_fields_omitted = _semantic_field_names(
        confirmation.get("changed_fields")
    )
    next_launch_fields, next_launch_fields_omitted = _semantic_field_names(
        confirmation.get("next_launch_changed_fields")
    )
    return {
        "confirmation_receipt": _opaque_id(
            confirmation.get("confirmation_receipt")
        ),
        "restore_point_id": _opaque_id(confirmation.get("restore_point_id")),
        "effect_policy": _effect_policy(confirmation.get("effect_policy")),
        "changed_fields": changed_fields,
        "next_launch_changed_fields": next_launch_fields,
        "unmanaged_content_changed": _exact_bool(
            confirmation.get("unmanaged_content_changed")
        ),
        "unmanaged_change_count": _nonnegative_int(
            confirmation.get("unmanaged_change_count")
        ),
        "resolution_error_before": _exact_bool(
            confirmation.get("resolution_error_before")
        ),
        "resolution_error_after": _exact_bool(
            confirmation.get("resolution_error_after")
        ),
        "truncation": {
            "truncated": bool(
                changed_fields_omitted or next_launch_fields_omitted
            ),
            "changed_fields_omitted": changed_fields_omitted,
            "next_launch_changed_fields_omitted": next_launch_fields_omitted,
        },
    }


def _overlay_rollback_confirmation_dto(value: object) -> dict[str, Any]:
    confirmation = _required_mapping(value)
    changed_fields, changed_fields_omitted = _semantic_field_names(
        confirmation.get("changed_fields")
    )
    return {
        "confirmation_receipt": _opaque_id(
            confirmation.get("confirmation_receipt")
        ),
        "restore_point_id": _opaque_id(confirmation.get("restore_point_id")),
        "effect_policy": _effect_policy(confirmation.get("effect_policy")),
        "changed_fields": changed_fields,
        "unmanaged_content_changed": _exact_bool(
            confirmation.get("unmanaged_content_changed")
        ),
        "unmanaged_change_count": _nonnegative_int(
            confirmation.get("unmanaged_change_count")
        ),
        "resolution_error_before": _exact_bool(
            confirmation.get("resolution_error_before")
        ),
        "resolution_error_after": _exact_bool(
            confirmation.get("resolution_error_after")
        ),
        "truncation": {
            "truncated": bool(changed_fields_omitted),
            "changed_fields_omitted": changed_fields_omitted,
        },
    }


def _ordinary_rollback_commit_dto(value: object) -> dict[str, Any]:
    commit = _required_mapping(value)
    if commit.get("status") != "restored":
        raise TypeError("service returned invalid rollback status")
    restore_point_id = commit.get("restore_point_id")
    if restore_point_id is not None:
        restore_point_id = _opaque_id(restore_point_id)
    maintenance_code = commit.get("maintenance_code")
    if maintenance_code is not None:
        maintenance_code = _stable_code(maintenance_code)
    return {
        "status": "restored",
        "effect_policy": _effect_policy(commit.get("effect_policy")),
        "restore_point_id": restore_point_id,
        "maintenance_code": maintenance_code,
    }


def _overlay_command(payload: object) -> OverlaySetValueRequest:
    if not isinstance(payload, dict) or set(payload) != {"key", "value"}:
        raise _WireError("overlay preview request is invalid")
    key = payload["key"]
    value = payload["value"]
    if not isinstance(key, str) or _FIELD_NAME.fullmatch(key) is None:
        raise _WireError("overlay key is invalid")
    if type(value) not in (int, float) or not math.isfinite(value):
        raise _WireError("overlay value is invalid")
    return OverlaySetValueRequest(key=key, value=float(value))


def _overlay_preview_dto(
    value: object,
    *,
    command: OverlaySetValueRequest,
) -> dict[str, Any]:
    preview = _required_mapping(value)
    if preview.get("key") != command.key:
        raise TypeError("service returned mismatched overlay semantics")
    before = preview.get("file_value_before")
    after = preview.get("file_value_after")
    if (
        type(before) not in (int, float)
        or not math.isfinite(before)
        or type(after) not in (int, float)
        or not math.isfinite(after)
    ):
        raise TypeError("service returned invalid overlay values")
    effect_policy = preview.get("effect_policy")
    if effect_policy not in {"next_spica_launch", "owner_mtime_reload"}:
        raise TypeError("service returned invalid effect policy")
    return {
        "preview_id": _opaque_id(preview.get("preview_id")),
        "key": command.key,
        "file_value_before": before,
        "file_value_after": after,
        "changed": _exact_bool(preview.get("changed")),
        "effect_policy": effect_policy,
    }


def _sensitive_command(
    payload: object,
) -> ClearMappedOverride | SetSecret | ClearSecret:
    if not isinstance(payload, dict) or set(payload) != {"command"}:
        raise _WireError("sensitive preview request is invalid")
    command = payload["command"]
    if not isinstance(command, dict):
        raise _WireError("sensitive command is invalid")
    kind = command.get("kind")
    if kind == "set_secret" and set(command) == {"kind", "slot", "value"}:
        slot = _slot_name(command["slot"])
        value = command["value"]
        if not isinstance(value, str) or not value or len(value) > 16 * 1024:
            raise _WireError("secret value is invalid")
        return SetSecret(slot=slot, value=value)
    if kind == "clear_secret" and set(command) == {"kind", "slot"}:
        return ClearSecret(slot=_slot_name(command["slot"]))
    if kind == "clear_mapped_override" and set(command) == {
        "kind",
        "environment_variable",
    }:
        environment_name = command["environment_variable"]
        if (
            not isinstance(environment_name, str)
            or _ENVIRONMENT_NAME.fullmatch(environment_name) is None
            or environment_name not in _MANAGED_OVERRIDE_NAMES
        ):
            raise _WireError("environment variable is invalid")
        return ClearMappedOverride(environment_name)
    raise _WireError("sensitive command is invalid")


def _slot_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or _SLOT_NAME.fullmatch(value) is None
        or value not in SECRETS_ENV_MAP
    ):
        raise _WireError("secret slot is invalid")
    return value


def _stable_code(value: object) -> str:
    if not isinstance(value, str) or _STABLE_CODE.fullmatch(value) is None:
        raise TypeError("service returned an invalid stable code")
    return value


def _source_name(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SOURCE_NAME.fullmatch(value) is None:
        raise TypeError("service returned an invalid source")
    return value


def _exact_bool(value: object) -> bool:
    if type(value) is not bool:
        raise TypeError("service returned an invalid boolean")
    return value


def _nonnegative_int(value: object, *, maximum: int = 1_000_000) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise TypeError("service returned an invalid integer")
    return value


def _sensitive_status_dto(value: object) -> dict[str, Any]:
    status = _required_mapping(value)
    slots = status.get("secret_slots")
    if not isinstance(slots, Sequence) or isinstance(slots, (str, bytes)):
        raise TypeError("service returned invalid secret slots")
    if len(slots) > 32:
        raise TypeError("service returned too many secret slots")
    rendered = []
    seen: set[str] = set()
    for item in slots:
        slot = _required_mapping(item)
        name = _slot_name(slot.get("slot"))
        if name in seen:
            raise TypeError("service returned duplicate secret slots")
        seen.add(name)
        rendered.append(
            {"slot": name, "configured": _exact_bool(slot.get("configured"))}
        )
    return {
        "secret_slots": rendered,
        "permission_health": _stable_code(status.get("permission_health")),
    }


def _affected_fields(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("service returned invalid affected fields")
    if len(value) > 32:
        raise TypeError("service returned too many affected fields")
    rendered: list[str] = []
    for field in value:
        if not isinstance(field, str) or _CONFIG_PATH.fullmatch(field) is None:
            raise TypeError("service returned an invalid affected field")
        rendered.append(field)
    return rendered


def _sensitive_preview_dto(
    value: object,
    *,
    command: ClearMappedOverride | SetSecret | ClearSecret,
) -> dict[str, Any]:
    preview = _required_mapping(value)
    command_kind = {
        SetSecret: "set_secret",
        ClearSecret: "clear_secret",
        ClearMappedOverride: "clear_mapped_override",
    }[type(command)]
    target = (
        command.environment_variable
        if isinstance(command, ClearMappedOverride)
        else command.slot
    )
    if preview.get("command_kind") != command_kind or preview.get("target") != target:
        raise TypeError("service returned mismatched sensitive semantics")
    secret_change = preview.get("secret_change")
    if secret_change not in {None, "unchanged", "will_set", "will_clear", "will_replace"}:
        raise TypeError("service returned invalid secret change")
    result = {
        "preview_id": _opaque_id(preview.get("preview_id")),
        "command_kind": command_kind,
        "target": target,
        "affected_fields": _affected_fields(preview.get("affected_fields")),
        "winning_source_before": _source_name(
            preview.get("winning_source_before")
        ),
        "winning_source_after": _source_name(preview.get("winning_source_after")),
        "still_shadowed": _exact_bool(preview.get("still_shadowed")),
        "permission_hardening": _exact_bool(
            preview.get("permission_hardening")
        ),
        "changed": _exact_bool(preview.get("changed")),
        "secret_change": secret_change,
        "resolution_error_before": _exact_bool(
            preview.get("resolution_error_before")
        ),
        "resolution_error_after": _exact_bool(
            preview.get("resolution_error_after")
        ),
    }
    if isinstance(command, ClearMappedOverride):
        result["before_next_launch"] = _bounded_json_value(
            preview.get("before_next_launch")
        )
        result["after_next_launch"] = _bounded_json_value(
            preview.get("after_next_launch")
        )
    return result


def _sensitive_clear_confirmation_dto(value: object) -> dict[str, Any]:
    confirmation = _required_mapping(value)
    if confirmation.get("command_kind") != "clear_secret":
        raise TypeError("service returned invalid clear confirmation")
    return {
        "confirmation_receipt": _opaque_id(
            confirmation.get("confirmation_receipt")
        ),
        "preview_id": _opaque_id(confirmation.get("preview_id")),
        "command_kind": "clear_secret",
        "target": _slot_name(confirmation.get("target")),
        "secret_change": (
            confirmation.get("secret_change")
            if confirmation.get("secret_change")
            in {"unchanged", "will_clear"}
            else _raise_type_error("invalid clear semantics")
        ),
    }


def _sensitive_commit_dto(value: object) -> dict[str, Any]:
    commit = _required_mapping(value)
    if commit.get("status") not in {"saved", "unchanged", "restored"}:
        raise TypeError("service returned invalid sensitive commit status")
    restore_point_id = commit.get("restore_point_id")
    if restore_point_id is not None:
        restore_point_id = _opaque_id(restore_point_id)
    maintenance_code = commit.get("maintenance_code")
    if maintenance_code is not None:
        maintenance_code = _stable_code(maintenance_code)
    return {
        "status": commit["status"],
        "restore_point_id": restore_point_id,
        "permission_health": _stable_code(commit.get("permission_health")),
        "maintenance_code": maintenance_code,
    }


def _sensitive_restore_points_dto(value: object) -> dict[str, Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("service returned invalid restore points")
    if len(value) > 16:
        raise TypeError("service returned too many restore points")
    points = []
    for item in value:
        point = _required_mapping(item)
        points.append(
            {
                "restore_point_id": _opaque_id(point.get("restore_point_id")),
                "created_at_ns": _nonnegative_int(
                    point.get("created_at_ns"), maximum=10**30
                ),
            }
        )
    return {"restore_points": points}


def _secret_changes(value: object) -> list[dict[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("service returned invalid secret changes")
    if len(value) > 32:
        raise TypeError("service returned too many secret changes")
    result = []
    for item in value:
        change = _required_mapping(item)
        semantic = change.get("change")
        if semantic not in {"unchanged", "will_set", "will_clear", "will_replace"}:
            raise TypeError("service returned invalid secret rollback change")
        result.append({"slot": _slot_name(change.get("slot")), "change": semantic})
    return result


def _override_changes(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("service returned invalid override changes")
    if len(value) > 64:
        raise TypeError("service returned too many override changes")
    result = []
    for item in value:
        change = _required_mapping(item)
        environment_name = change.get("environment_variable")
        if (
            not isinstance(environment_name, str)
            or _ENVIRONMENT_NAME.fullmatch(environment_name) is None
        ):
            raise TypeError("service returned invalid environment name")
        result.append(
            {
                "environment_variable": environment_name,
                "affected_fields": _affected_fields(change.get("affected_fields")),
                "before_next_launch": _bounded_json_value(
                    change.get("before_next_launch")
                ),
                "after_next_launch": _bounded_json_value(
                    change.get("after_next_launch")
                ),
                "winning_source_before": _source_name(
                    change.get("winning_source_before")
                ),
                "winning_source_after": _source_name(
                    change.get("winning_source_after")
                ),
                "still_shadowed": _exact_bool(change.get("still_shadowed")),
            }
        )
    return result


def _sensitive_rollback_confirmation_dto(value: object) -> dict[str, Any]:
    confirmation = _required_mapping(value)
    return {
        "confirmation_receipt": _opaque_id(
            confirmation.get("confirmation_receipt")
        ),
        "restore_point_id": _opaque_id(confirmation.get("restore_point_id")),
        "secret_changes": _secret_changes(confirmation.get("secret_changes")),
        "override_changes": _override_changes(confirmation.get("override_changes")),
        "unmanaged_content_changed": _exact_bool(
            confirmation.get("unmanaged_content_changed")
        ),
        "unmanaged_change_count": _nonnegative_int(
            confirmation.get("unmanaged_change_count")
        ),
        "permission_hardening": _exact_bool(
            confirmation.get("permission_hardening")
        ),
        "resolution_error_before": _exact_bool(
            confirmation.get("resolution_error_before")
        ),
        "resolution_error_after": _exact_bool(
            confirmation.get("resolution_error_after")
        ),
    }


def _json_error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code}})


def _service_error(exc: ConfigStudioServiceError) -> JSONResponse:
    status_code = _SERVICE_ERROR_STATUS.get(exc.code)
    if status_code is None:
        return _json_error(500, "INTERNAL_ERROR")
    return _json_error(status_code, exc.code)


def _add_security_headers(response: Any) -> Any:
    for name, value in _SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


def _single_header(request: Request, name: str) -> str | None:
    encoded_name = name.lower().encode("ascii")
    values = [
        value.decode("latin-1")
        for key, value in request.scope.get("headers", ())
        if key.lower() == encoded_name
    ]
    if len(values) != 1:
        return None
    return values[0]


def _single_session_cookie(request: Request) -> str | None:
    cookie_header = _single_header(request, "cookie")
    if cookie_header is None:
        return None
    matches: list[str] = []
    for item in cookie_header.split(";"):
        name, separator, value = item.strip().partition("=")
        if separator and name == SESSION_COOKIE_NAME:
            matches.append(value)
    if len(matches) != 1:
        return None
    return matches[0]


def _self_check_command(payload: object) -> SelfCheckStartRequest | None:
    if not isinstance(payload, dict) or not set(payload).issubset(
        _SELF_CHECK_COMMAND_KEYS
    ):
        return None
    mode = payload.get("mode", "light")
    only = payload.get("only", [])
    booleans = (
        payload.get("llm", False),
        payload.get("include_disabled", False),
        payload.get("allow_model_downloads", False),
    )
    if (
        not isinstance(mode, str)
        or mode not in _SELF_CHECK_MODES
        or not isinstance(only, list)
        or any(not isinstance(item, str) for item in only)
        or len(set(only)) != len(only)
        or any(type(item) is not bool for item in booleans)
    ):
        return None
    return SelfCheckStartRequest(
        mode=SelfCheckMode(mode),
        only=tuple(only),
        llm=booleans[0],
        include_disabled=booleans[1],
        allow_model_downloads=booleans[2],
    )


def _self_check_start_request(
    payload: object,
) -> tuple[SelfCheckStartRequest, str | None] | None:
    if not isinstance(payload, dict) or not set(payload).issubset(
        _SELF_CHECK_START_KEYS
    ):
        return None
    command = _self_check_command(
        {key: value for key, value in payload.items() if key != "confirmation_receipt"}
    )
    if command is None:
        return None
    receipt = payload.get("confirmation_receipt")
    if receipt is not None:
        try:
            receipt = _opaque_id(receipt)
        except _WireError:
            return None
    if command.mode is SelfCheckMode.LIGHT and receipt is not None:
        return None
    return command, receipt


def _self_check_confirmation_request(
    payload: object,
) -> tuple[SelfCheckStartRequest, SelfCheckAcknowledgements] | None:
    if not isinstance(payload, dict) or "acknowledgements" not in payload:
        return None
    command = _self_check_command(
        {
            key: value
            for key, value in payload.items()
            if key != "acknowledgements"
        }
    )
    acknowledgements = payload.get("acknowledgements")
    if (
        command is None
        or not isinstance(acknowledgements, dict)
        or set(acknowledgements) != _SELF_CHECK_ACKNOWLEDGEMENT_KEYS
        or any(type(value) is not bool for value in acknowledgements.values())
    ):
        return None
    return command, SelfCheckAcknowledgements(
        full=acknowledgements["full"],
        llm=acknowledgements["llm"],
        include_disabled=acknowledgements["include_disabled"],
        model_downloads=acknowledgements["model_downloads"],
    )


def _self_check_job_dto(job: object) -> dict[str, Any]:
    value = _required_mapping(job)
    mode = value.get("mode")
    if not isinstance(mode, str) or mode not in _SELF_CHECK_MODES:
        raise TypeError("self-check service returned invalid mode")
    status = value.get("status")
    if not isinstance(status, str) or status not in _SELF_CHECK_JOB_STATUSES:
        raise TypeError("self-check service returned invalid status")
    checks = _self_check_names(value.get("checks"), maximum=16)
    duration = _finite_duration(value.get("duration_s"), optional=False)
    results = value.get("results")
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
        raise TypeError("self-check service returned invalid results")
    if len(results) > 16:
        raise TypeError("self-check service returned too many results")
    progress = value.get("progress")
    if not isinstance(progress, Sequence) or isinstance(progress, (str, bytes)):
        raise TypeError("self-check service returned invalid progress")
    if len(progress) > 16:
        raise TypeError("self-check service returned too much progress")
    error_code = value.get("error_code")
    if error_code is not None:
        error_code = _stable_code(error_code)
    dto = {
        "job_id": _opaque_id(value.get("job_id")),
        "mode": mode,
        "checks": checks,
        "status": status,
        "duration_s": duration,
        "results": [_self_check_result_dto(item) for item in results],
        "progress": [_self_check_progress_dto(item) for item in progress],
        "error_code": error_code,
        "stderr_line_count": _nonnegative_int(value.get("stderr_line_count")),
        "stderr_total_line_count": _nonnegative_int(
            value.get("stderr_total_line_count")
        ),
        "stderr_truncated": _exact_bool(value.get("stderr_truncated")),
    }
    _enforce_self_check_wire_budget(dto, maximum=_SELF_CHECK_WIRE_BUDGET)
    return dto


def _self_check_names(value: object, *, maximum: int) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("self-check service returned invalid names")
    if len(value) > maximum:
        raise TypeError("self-check service returned too many names")
    allowed = frozenset(LIGHT_CHECKS)
    names: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in allowed:
            raise TypeError("self-check service returned an invalid check name")
        names.append(item)
    if len(set(names)) != len(names):
        raise TypeError("self-check service returned duplicate check names")
    return names


def _finite_duration(value: object, *, optional: bool) -> float | None:
    if value is None and optional:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise TypeError("self-check service returned invalid duration")
    return float(value)


def _self_check_result_dto(value: object) -> dict[str, Any]:
    result = _required_mapping(value)
    name = result.get("name")
    if not isinstance(name, str) or name not in frozenset(LIGHT_CHECKS):
        raise TypeError("self-check service returned invalid result name")
    status = result.get("status")
    if not isinstance(status, str) or status not in _SELF_CHECK_RESULT_STATUSES:
        raise TypeError("self-check service returned invalid result status")
    reason = result.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 2048 or "\x00" in reason:
        raise TypeError("self-check service returned invalid result reason")
    return {
        "name": name,
        "status": status,
        "detail": _self_check_detail(result.get("detail", {})),
        "reason": reason,
        "duration_s": _finite_duration(
            result.get("duration_s"), optional=True
        ),
    }


def _self_check_progress_dto(value: object) -> dict[str, Any]:
    progress = _required_mapping(value)
    name = progress.get("name")
    if not isinstance(name, str) or name not in frozenset(LIGHT_CHECKS):
        raise TypeError("self-check service returned invalid progress name")
    status = progress.get("status")
    if status != "RUNNING":
        raise TypeError("self-check service returned invalid progress status")
    return {"name": name, "status": "RUNNING"}


def _self_check_detail(value: object, *, depth: int = 0) -> Any:
    if depth > 5:
        raise TypeError("self-check detail is too deep")
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise TypeError("self-check detail contains a non-finite number")
        return value
    if isinstance(value, str):
        if len(value) > 2048 or "\x00" in value:
            raise TypeError("self-check detail string is invalid")
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) > 64:
            raise TypeError("self-check detail collection is too large")
        return [_self_check_detail(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        if len(value) > 32:
            raise TypeError("self-check detail mapping is too large")
        rendered: dict[str, Any] = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 128
                or "\x00" in key
            ):
                raise TypeError("self-check detail key is invalid")
            if _looks_secret_key(key):
                rendered[key] = "<redacted>"
            else:
                rendered[key] = _self_check_detail(item, depth=depth + 1)
        return rendered
    raise TypeError("self-check detail contains an unsupported object")


def _looks_secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    parts = frozenset(part for part in normalized.split("_") if part)
    return normalized in {"api_key", "apikey"} or bool(parts & _SECRET_KEY_PARTS)


def _enforce_self_check_wire_budget(value: object, *, maximum: int) -> None:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TypeError("self-check DTO is not JSON safe") from exc
    if len(rendered) > maximum:
        raise TypeError("self-check DTO exceeds its wire budget")


def _self_check_confirmation_dto(
    value: object,
    *,
    command: SelfCheckStartRequest,
) -> dict[str, Any]:
    confirmation = _required_mapping(value)
    semantic = _required_mapping(confirmation.get("semantic"))
    mode = semantic.get("mode")
    if mode != "full" or command.mode is not SelfCheckMode.FULL:
        raise TypeError("service returned invalid confirmation mode")
    checks = _self_check_names(semantic.get("checks"), maximum=16)
    expected_checks = list(command.only) or list(HEAVY_CHECKS)
    if checks != expected_checks:
        raise TypeError("service returned mismatched confirmation checks")
    booleans = {
        "llm": command.llm,
        "include_disabled": command.include_disabled,
        "allow_model_downloads": command.allow_model_downloads,
    }
    for key, expected in booleans.items():
        if semantic.get(key) is not expected:
            raise TypeError("service returned mismatched confirmation semantics")
    expires_in_s = confirmation.get("expires_in_s")
    if (
        type(expires_in_s) not in (int, float)
        or not math.isfinite(expires_in_s)
        or not 0 < expires_in_s <= 15 * 60
    ):
        raise TypeError("service returned invalid confirmation lifetime")
    return {
        "confirmation_receipt": _opaque_id(
            confirmation.get("confirmation_receipt")
        ),
        "expires_in_s": float(expires_in_s),
        "semantic": {"mode": "full", "checks": checks, **booleans},
    }


def _self_check_service_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, SelfCheckPlanError):
        if exc.code in _SELF_CHECK_CONFIRMATION_ERRORS:
            return _json_error(409, "CONFIRMATION_REQUIRED")
        return _json_error(400, "SELF_CHECK_PLAN_INVALID")
    if isinstance(exc, SelfCheckJobError):
        if exc.code == "SELF_CHECK_BUSY":
            return _json_error(409, "SELF_CHECK_BUSY")
        if exc.code == "SELF_CHECK_JOB_NOT_FOUND":
            return _json_error(404, "SELF_CHECK_JOB_NOT_FOUND")
        if exc.code == "SELF_CHECK_PLAN_INVALID":
            return _json_error(400, "SELF_CHECK_PLAN_INVALID")
        if exc.code in _SELF_CHECK_UNAVAILABLE_ERRORS:
            return _json_error(503, "SELF_CHECK_UNAVAILABLE")
    return _json_error(500, "INTERNAL_ERROR")


def create_config_studio_app(
    services: ConfigStudioServices,
    security_context: SecurityContext,
) -> FastAPI:
    """Create the fixed local API with injected production owner services."""

    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        swagger_ui_oauth2_redirect_url=None,
    )
    static_assets = load_static_ui_assets()

    @app.middleware("http")
    async def security_boundary(request: Request, call_next: Any) -> Any:
        host = _single_header(request, "host")
        if host != security_context.authority:
            return _add_security_headers(_json_error(403, "ORIGIN_REJECTED"))

        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if _single_header(request, "origin") != security_context.origin:
                return _add_security_headers(_json_error(403, "ORIGIN_REJECTED"))

        is_nonbootstrap_api = (
            request.url.path.startswith("/api/")
            and request.url.path != "/api/v1/session/bootstrap"
        )
        session_token = _single_session_cookie(request)
        if is_nonbootstrap_api and not security_context.session_is_valid(session_token):
            return _add_security_headers(_json_error(401, "SESSION_REQUIRED"))
        if is_nonbootstrap_api and session_token is not None:
            request.state.config_studio_session_id = _session_identity(session_token)
        if (
            is_nonbootstrap_api
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and not security_context.csrf_is_valid(
                session_token,
                _single_header(request, CSRF_HEADER_NAME),
            )
        ):
            return _add_security_headers(_json_error(403, "CSRF_INVALID"))

        try:
            response = await call_next(request)
        except _JsonContentTypeError:
            response = _json_error(415, "JSON_CONTENT_TYPE_REQUIRED")
        except ConfigStudioServiceError as exc:
            response = _service_error(exc)
        except Exception:
            response = _json_error(500, "INTERNAL_ERROR")
        return _add_security_headers(response)

    @app.post("/api/v1/session/bootstrap")
    async def bootstrap(request: Request) -> JSONResponse:
        token = _single_header(request, BOOTSTRAP_HEADER_NAME)
        if token is None:
            return _json_error(401, "BOOTSTRAP_INVALID")
        credentials = security_context.exchange_bootstrap(token)
        if credentials is None:
            return _json_error(401, "BOOTSTRAP_INVALID")

        response = JSONResponse(
            {"csrf_token": credentials.csrf_token, "clear_fragment": True}
        )
        response.set_cookie(
            SESSION_COOKIE_NAME,
            credentials.session_token,
            httponly=True,
            secure=False,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/")
    async def index() -> Response:
        return Response(static_assets.index_html, media_type="text/html; charset=utf-8")

    @app.get("/assets/studio.css")
    async def stylesheet() -> Response:
        return Response(static_assets.stylesheet, media_type="text/css; charset=utf-8")

    @app.get("/assets/studio.js")
    async def javascript() -> Response:
        return Response(
            static_assets.javascript,
            media_type="text/javascript; charset=utf-8",
        )

    @app.get("/assets/background.png")
    async def background() -> Response:
        if static_assets.background.content is None:
            return _json_error(404, "BACKGROUND_ASSET_INVALID")
        return Response(static_assets.background.content, media_type="image/png")

    @app.get("/api/v1/meta")
    async def meta() -> JSONResponse:
        return JSONResponse(dict(services.meta()))

    @app.get("/api/v1/session/csrf")
    async def rotate_csrf(request: Request) -> JSONResponse:
        csrf_token = security_context.rotate_csrf(_single_session_cookie(request))
        if csrf_token is None:
            return _json_error(401, "SESSION_REQUIRED")
        return JSONResponse({"csrf_token": csrf_token})

    @app.get("/api/v1/catalog")
    async def catalog() -> JSONResponse:
        return JSONResponse(dict(services.catalog()))

    @app.post("/api/v1/app/previews")
    async def preview_app(request: Request) -> JSONResponse:
        if not services.capability_enabled("app_config_write"):
            return _json_error(403, "CAPABILITY_UNAVAILABLE")
        try:
            operations = _app_operations(await _bounded_json_payload(request))
        except _WireError:
            return _json_error(400, "DOCUMENT_INVALID")
        preview = services.preview_app(
            operations,
            session_id=_session_id(request),
        )
        return JSONResponse(_app_preview_dto(preview))

    @app.post("/api/v1/app/commits")
    async def commit_app(request: Request) -> JSONResponse:
        if not services.capability_enabled("app_config_write"):
            return _json_error(403, "CAPABILITY_UNAVAILABLE")
        try:
            payload = await _bounded_json_payload(request)
            if not isinstance(payload, dict) or set(payload) != {"preview_id"}:
                raise _WireError("app commit request is invalid")
            preview_id = _opaque_id(payload["preview_id"])
        except _WireError:
            return _json_error(400, "DOCUMENT_INVALID")
        commit = services.commit_app_preview(
            preview_id,
            session_id=_session_id(request),
        )
        return JSONResponse(_app_commit_dto(commit))

    @app.get("/api/v1/app/restore-points")
    async def list_app_restore_points(request: Request) -> JSONResponse:
        if not (
            services.capability_enabled("app_config_write")
            and services.capability_enabled("rollback")
        ):
            return _json_error(403, "CAPABILITY_UNAVAILABLE")
        return JSONResponse(
            _ordinary_restore_points_dto(
                services.list_app_restore_points(
                    session_id=_session_id(request),
                )
            )
        )

    @app.post("/api/v1/app/restore-points/{restore_point_id}/prepare-rollback")
    async def prepare_app_rollback(
        restore_point_id: str,
        request: Request,
    ) -> JSONResponse:
        if not (
            services.capability_enabled("app_config_write")
            and services.capability_enabled("rollback")
        ):
            return _json_error(403, "CAPABILITY_UNAVAILABLE")
        try:
            bounded_restore_point_id = _opaque_id(restore_point_id)
        except _WireError:
            return _json_error(400, "RESTORE_POINT_INVALID")
        confirmation = services.prepare_app_rollback(
            bounded_restore_point_id,
            session_id=_session_id(request),
        )
        return JSONResponse(_app_rollback_confirmation_dto(confirmation))

    @app.post("/api/v1/app/rollbacks")
    async def rollback_app(request: Request) -> JSONResponse:
        if not (
            services.capability_enabled("app_config_write")
            and services.capability_enabled("rollback")
        ):
            return _json_error(403, "CAPABILITY_UNAVAILABLE")
        try:
            payload = await _bounded_json_payload(request)
            if not isinstance(payload, dict) or set(payload) != {
                "confirmation_receipt"
            }:
                raise _WireError("app rollback request is invalid")
            receipt = _opaque_id(payload["confirmation_receipt"])
        except _WireError:
            return _json_error(400, "CONFIRMATION_INVALID")
        committed = services.rollback_app(
            receipt,
            session_id=_session_id(request),
        )
        return JSONResponse(_ordinary_rollback_commit_dto(committed))

    @app.post("/api/v1/overlay/previews")
    async def preview_overlay(request: Request) -> JSONResponse:
        if not services.capability_enabled("overlay_write"):
            return _json_error(403, "CAPABILITY_UNAVAILABLE")
        try:
            command = _overlay_command(await _bounded_json_payload(request))
        except _WireError:
            return _json_error(400, "OVERLAY_COMMAND_INVALID")
        preview = services.preview_overlay(
            command,
            session_id=_session_id(request),
        )
        return JSONResponse(_overlay_preview_dto(preview, command=command))

    @app.post("/api/v1/overlay/commits")
    async def commit_overlay(request: Request) -> JSONResponse:
        if not services.capability_enabled("overlay_write"):
            return _json_error(403, "CAPABILITY_UNAVAILABLE")
        try:
            payload = await _bounded_json_payload(request)
            if not isinstance(payload, dict) or set(payload) != {"preview_id"}:
                raise _WireError("overlay commit request is invalid")
            preview_id = _opaque_id(payload["preview_id"])
        except _WireError:
            return _json_error(400, "CONFIRMATION_INVALID")
        commit = services.commit_overlay_preview(
            preview_id,
            session_id=_session_id(request),
        )
        return JSONResponse(_app_commit_dto(commit))

    @app.get("/api/v1/overlay/restore-points")
    async def list_overlay_restore_points(request: Request) -> JSONResponse:
        if not (
            services.capability_enabled("overlay_write")
            and services.capability_enabled("rollback")
        ):
            return _json_error(403, "CAPABILITY_UNAVAILABLE")
        return JSONResponse(
            _ordinary_restore_points_dto(
                services.list_overlay_restore_points(
                    session_id=_session_id(request),
                )
            )
        )

    @app.post(
        "/api/v1/overlay/restore-points/{restore_point_id}/prepare-rollback"
    )
    async def prepare_overlay_rollback(
        restore_point_id: str,
        request: Request,
    ) -> JSONResponse:
        if not (
            services.capability_enabled("overlay_write")
            and services.capability_enabled("rollback")
        ):
            return _json_error(403, "CAPABILITY_UNAVAILABLE")
        try:
            bounded_restore_point_id = _opaque_id(restore_point_id)
        except _WireError:
            return _json_error(400, "RESTORE_POINT_INVALID")
        confirmation = services.prepare_overlay_rollback(
            bounded_restore_point_id,
            session_id=_session_id(request),
        )
        return JSONResponse(_overlay_rollback_confirmation_dto(confirmation))

    @app.post("/api/v1/overlay/rollbacks")
    async def rollback_overlay(request: Request) -> JSONResponse:
        if not (
            services.capability_enabled("overlay_write")
            and services.capability_enabled("rollback")
        ):
            return _json_error(403, "CAPABILITY_UNAVAILABLE")
        try:
            payload = await _bounded_json_payload(request)
            if not isinstance(payload, dict) or set(payload) != {
                "confirmation_receipt"
            }:
                raise _WireError("overlay rollback request is invalid")
            receipt = _opaque_id(payload["confirmation_receipt"])
        except _WireError:
            return _json_error(400, "CONFIRMATION_INVALID")
        committed = services.rollback_overlay(
            receipt,
            session_id=_session_id(request),
        )
        return JSONResponse(_ordinary_rollback_commit_dto(committed))

    @app.get("/api/v1/sensitive/status")
    async def sensitive_status(request: Request) -> JSONResponse:
        return JSONResponse(
            _sensitive_status_dto(
                services.sensitive_status(session_id=_session_id(request))
            )
        )

    @app.post("/api/v1/sensitive/previews")
    async def preview_sensitive(request: Request) -> JSONResponse:
        if not services.capability_enabled("sensitive_write"):
            return _json_error(403, "CAPABILITY_UNAVAILABLE")
        try:
            command = _sensitive_command(await _bounded_json_payload(request))
        except _WireError:
            return _json_error(400, "SENSITIVE_COMMAND_INVALID")
        preview = services.preview_sensitive(
            command,
            session_id=_session_id(request),
        )
        return JSONResponse(_sensitive_preview_dto(preview, command=command))

    @app.post("/api/v1/sensitive/previews/{preview_id}/confirm-clear")
    async def confirm_sensitive_clear(
        preview_id: str,
        request: Request,
    ) -> JSONResponse:
        if not services.capability_enabled("sensitive_write"):
            return _json_error(403, "CAPABILITY_UNAVAILABLE")
        try:
            bounded_preview_id = _opaque_id(preview_id)
        except _WireError:
            return _json_error(400, "CONFIRMATION_INVALID")
        confirmation = services.confirm_sensitive_secret_clear(
            bounded_preview_id,
            session_id=_session_id(request),
        )
        return JSONResponse(_sensitive_clear_confirmation_dto(confirmation))

    @app.post("/api/v1/sensitive/commits")
    async def commit_sensitive(request: Request) -> JSONResponse:
        if not services.capability_enabled("sensitive_write"):
            return _json_error(403, "CAPABILITY_UNAVAILABLE")
        try:
            payload = await _bounded_json_payload(request)
            if not isinstance(payload, dict) or not set(payload).issubset(
                {"preview_id", "confirmation_receipt"}
            ) or "preview_id" not in payload:
                raise _WireError("sensitive commit request is invalid")
            preview_id = _opaque_id(payload["preview_id"])
            receipt = payload.get("confirmation_receipt")
            if receipt is not None:
                receipt = _opaque_id(receipt)
        except _WireError:
            return _json_error(400, "CONFIRMATION_INVALID")
        committed = services.commit_sensitive_preview(
            preview_id,
            receipt,
            session_id=_session_id(request),
        )
        return JSONResponse(_sensitive_commit_dto(committed))

    @app.get("/api/v1/sensitive/restore-points")
    async def list_sensitive_restore_points(request: Request) -> JSONResponse:
        if not (
            services.capability_enabled("sensitive_write")
            and services.capability_enabled("rollback")
        ):
            return _json_error(403, "CAPABILITY_UNAVAILABLE")
        return JSONResponse(
            _sensitive_restore_points_dto(
                services.list_sensitive_restore_points(
                    session_id=_session_id(request)
                )
            )
        )

    @app.post(
        "/api/v1/sensitive/restore-points/{restore_point_id}/prepare-rollback"
    )
    async def prepare_sensitive_rollback(
        restore_point_id: str,
        request: Request,
    ) -> JSONResponse:
        if not (
            services.capability_enabled("sensitive_write")
            and services.capability_enabled("rollback")
        ):
            return _json_error(403, "CAPABILITY_UNAVAILABLE")
        try:
            bounded_restore_point_id = _opaque_id(restore_point_id)
        except _WireError:
            return _json_error(400, "RESTORE_POINT_INVALID")
        confirmation = services.prepare_sensitive_rollback(
            bounded_restore_point_id,
            session_id=_session_id(request),
        )
        return JSONResponse(_sensitive_rollback_confirmation_dto(confirmation))

    @app.post("/api/v1/sensitive/rollbacks")
    async def rollback_sensitive(request: Request) -> JSONResponse:
        if not (
            services.capability_enabled("sensitive_write")
            and services.capability_enabled("rollback")
        ):
            return _json_error(403, "CAPABILITY_UNAVAILABLE")
        try:
            payload = await _bounded_json_payload(request)
            if not isinstance(payload, dict) or set(payload) != {
                "confirmation_receipt"
            }:
                raise _WireError("rollback request is invalid")
            receipt = _opaque_id(payload["confirmation_receipt"])
        except _WireError:
            return _json_error(400, "CONFIRMATION_INVALID")
        committed = services.rollback_sensitive(
            receipt,
            session_id=_session_id(request),
        )
        return JSONResponse(_sensitive_commit_dto(committed))

    @app.post("/api/v1/self-check/confirm")
    async def confirm_self_check(request: Request) -> JSONResponse:
        if not services.capability_enabled("self_check"):
            return _json_error(403, "CAPABILITY_UNAVAILABLE")
        try:
            parsed_confirmation = _self_check_confirmation_request(
                await _bounded_json_payload(request)
            )
        except _WireError:
            parsed_confirmation = None
        if parsed_confirmation is None:
            return _json_error(400, "SELF_CHECK_PLAN_INVALID")
        command, acknowledgements = parsed_confirmation
        if command.mode is not SelfCheckMode.FULL:
            return _json_error(400, "SELF_CHECK_PLAN_INVALID")
        try:
            confirmation = services.prepare_heavy_self_check(
                command,
                acknowledgements=acknowledgements,
                session_id=_session_id(request),
            )
        except (SelfCheckPlanError, SelfCheckJobError) as exc:
            return _self_check_service_error(exc)
        return JSONResponse(
            _self_check_confirmation_dto(confirmation, command=command)
        )

    @app.post("/api/v1/self-check/jobs")
    async def start_self_check(request: Request) -> JSONResponse:
        if not services.capability_enabled("self_check"):
            return _json_error(403, "CAPABILITY_UNAVAILABLE")
        try:
            payload = await _bounded_json_payload(request)
        except _WireError:
            return _json_error(400, "SELF_CHECK_PLAN_INVALID")
        parsed = _self_check_start_request(payload)
        if parsed is None:
            return _json_error(400, "SELF_CHECK_PLAN_INVALID")
        command, confirmation_receipt = parsed
        if command.mode is SelfCheckMode.FULL and confirmation_receipt is None:
            return _json_error(409, "CONFIRMATION_REQUIRED")
        try:
            if command.mode is SelfCheckMode.FULL:
                assert confirmation_receipt is not None
                job = services.start_confirmed_self_check(
                    command,
                    session_id=_session_id(request),
                    confirmation_receipt=confirmation_receipt,
                )
            else:
                job = services.start_self_check(command)
        except (SelfCheckPlanError, SelfCheckJobError) as exc:
            return _self_check_service_error(exc)
        return JSONResponse(_self_check_job_dto(job), status_code=202)

    @app.get("/api/v1/self-check/jobs")
    async def list_self_checks() -> JSONResponse:
        if not services.self_check_jobs_available():
            return _json_error(403, "CAPABILITY_UNAVAILABLE")
        try:
            jobs = services.list_self_checks()
            if len(jobs) > _SELF_CHECK_COLLECTION_MAX_JOBS:
                raise TypeError("self-check service returned too many jobs")
            content = [_self_check_job_dto(job) for job in jobs]
            _enforce_self_check_wire_budget(
                {"jobs": content}, maximum=_SELF_CHECK_COLLECTION_BUDGET
            )
        except (SelfCheckPlanError, SelfCheckJobError) as exc:
            return _self_check_service_error(exc)
        return JSONResponse({"jobs": content})

    @app.get("/api/v1/self-check/jobs/{job_id}")
    async def get_self_check(job_id: str) -> JSONResponse:
        if not services.self_check_jobs_available():
            return _json_error(403, "CAPABILITY_UNAVAILABLE")
        try:
            job = services.get_self_check(_opaque_id(job_id))
        except _WireError:
            return _json_error(400, "SELF_CHECK_JOB_INVALID")
        except (SelfCheckPlanError, SelfCheckJobError) as exc:
            return _self_check_service_error(exc)
        return JSONResponse(_self_check_job_dto(job))

    @app.post("/api/v1/self-check/jobs/{job_id}/cancel")
    async def cancel_self_check(job_id: str) -> JSONResponse:
        if not services.self_check_jobs_available():
            return _json_error(403, "CAPABILITY_UNAVAILABLE")
        try:
            job = services.cancel_self_check(_opaque_id(job_id))
        except _WireError:
            return _json_error(400, "SELF_CHECK_JOB_INVALID")
        except (SelfCheckPlanError, SelfCheckJobError) as exc:
            return _self_check_service_error(exc)
        return JSONResponse(_self_check_job_dto(job))

    return app
