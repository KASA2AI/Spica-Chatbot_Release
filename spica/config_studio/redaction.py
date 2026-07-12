"""Explicit secret-canary redaction for Config Studio wire DTOs."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Callable

from spica.config.secrets import Secrets


def secret_canaries(
    secrets: Secrets,
    extra_canaries: tuple[tuple[str, str], ...] = (),
) -> tuple[tuple[str, str], ...]:
    values = (
        ("OPENAI_API_KEY", secrets.openai_api_key),
        ("JUDGE_API_KEY", secrets.judge_api_key),
        ("BILIBILI_COOKIE", secrets.bilibili_cookie),
        ("QBITTORRENT_PASSWORD", secrets.qbittorrent_password),
    )
    return tuple((name, value) for name, value in values if value) + tuple(
        (name, value) for name, value in extra_canaries if value
    )


def redact_wire_value(
    value: Any,
    secrets: Secrets,
    extra_canaries: tuple[tuple[str, str], ...] = (),
) -> Any:
    """Recursively redact explicit secret values from both values and JSON keys."""

    redact_text, _, visit_data = _redactors(secrets, extra_canaries)
    if redact_text is None:
        return value
    return visit_data(value)


def redact_catalog_payload(
    payload: Mapping[str, Any],
    secrets: Secrets,
    extra_canaries: tuple[tuple[str, str], ...] = (),
    *,
    text_sanitizer: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Redact catalog data slots while preserving the fixed wire-schema keys."""

    redact_text, visit, visit_data = _redactors(
        secrets,
        extra_canaries,
        text_sanitizer=text_sanitizer,
    )
    if redact_text is None:
        return dict(payload)
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if key == "fields" and isinstance(value, (tuple, list)):
            result[key] = [
                _redact_catalog_field(item, redact_text, visit_data)
                for item in value
            ]
        elif key == "truncation" and isinstance(value, Mapping):
            result[key] = dict(value)
        elif key == "managed_documents" and isinstance(value, (tuple, list)):
            result[key] = [
                _redact_managed_document(
                    item,
                    redact_text,
                    visit,
                    visit_data,
                )
                for item in value
            ]
        elif key in {"environment_only_settings", "plugin_statuses"} and isinstance(
            value, (tuple, list)
        ):
            result[key] = [
                {
                    str(child_key): visit(child_value)
                    for child_key, child_value in item.items()
                }
                if isinstance(item, Mapping)
                else visit(item)
                for item in value
            ]
        else:
            result[key] = visit(value)
    return result


def enforce_catalog_wire_budget(
    payload: dict[str, Any],
    *,
    max_total_bytes: int = 512 * 1024,
) -> dict[str, Any]:
    """Trim already-redacted rows so replacement markers cannot exceed budget."""

    if max_total_bytes < 1024:
        raise ValueError("catalog wire budget must be at least 1024 bytes")
    fields = payload.get("fields")
    documents = payload.get("managed_documents")
    while _encoded_size(payload) > max_total_bytes:
        removed = False
        if isinstance(documents, list):
            for document in reversed(documents):
                document_fields = document.get("fields")
                if isinstance(document_fields, list) and document_fields:
                    document_fields.pop()
                    truncation = document.get("truncation")
                    if isinstance(truncation, dict):
                        truncation["total_bytes"] = (
                            int(truncation.get("total_bytes", 0)) + 1
                        )
                    removed = True
                    break
        if not removed and isinstance(fields, list) and fields:
            fields.pop()
            payload["fields_complete"] = False
            truncation = payload.get("truncation")
            if isinstance(truncation, dict):
                truncation["total_bytes"] = int(
                    truncation.get("total_bytes", 0)
                ) + 1
            removed = True
        if not removed and isinstance(documents, list) and documents:
            documents.pop()
            truncation = payload.get("truncation")
            if isinstance(truncation, dict):
                truncation["total_bytes"] = int(
                    truncation.get("total_bytes", 0)
                ) + 1
            removed = True
        if not removed:
            raise ValueError("catalog response budget cannot represent metadata")
    return payload


def _redact_catalog_field(
    field: Any,
    redact_text: Any,
    visit: Any,
) -> Any:
    if not isinstance(field, Mapping):
        return visit(field)
    result: dict[str, Any] = {}
    authoring_projection_changed = False
    for key, value in field.items():
        if key == "path" and isinstance(value, (tuple, list)):
            segments = []
            for segment in value:
                if not isinstance(segment, Mapping):
                    segments.append(visit(segment))
                    continue
                rendered_segment = dict(segment)
                if (
                    segment.get("kind") == "map_key"
                    and isinstance(segment.get("key"), str)
                ):
                    redacted_key = redact_text(segment["key"])
                    rendered_segment["key"] = redacted_key
                    authoring_projection_changed |= redacted_key != segment["key"]
                segments.append(rendered_segment)
            result[key] = segments
        elif key == "display_path" and isinstance(value, str):
            result[key] = redact_text(value)
        elif key in {"file_value", "next_launch_value", "current_value"}:
            redacted_value = visit(value)
            result[key] = redacted_value
            authoring_projection_changed |= redacted_value != value
        else:
            # The remaining entries are fixed owner/schema metadata.  Treating
            # common-word canaries as data here would corrupt JSON Schema,
            # Literal choices, path kinds, or other authoring contracts.
            result[key] = value
    if authoring_projection_changed and "authoring_complete" in result:
        result["authoring_complete"] = False
    return result


def _redact_managed_document(
    document: Any,
    redact_text: Any,
    visit: Any,
    visit_data: Any,
) -> Any:
    if not isinstance(document, Mapping):
        return visit(document)
    result: dict[str, Any] = {}
    for key, value in document.items():
        if key == "fields" and isinstance(value, (tuple, list)):
            result[key] = [
                _redact_catalog_field(field, redact_text, visit_data)
                for field in value
            ]
        elif key in {"health", "truncation"} and isinstance(value, Mapping):
            result[key] = {
                str(child_key): visit(child_value)
                for child_key, child_value in value.items()
            }
        else:
            result[key] = visit(value)
    return result


def _redactors(
    secrets: Secrets,
    extra_canaries: tuple[tuple[str, str], ...] = (),
    *,
    text_sanitizer: Callable[[str], str] | None = None,
) -> tuple[Any, Any, Any]:
    replacements = _replacement_map(secret_canaries(secrets, extra_canaries))
    if not replacements and text_sanitizer is None:
        identity = lambda value: value
        return None, identity, identity
    pattern = (
        re.compile(
            "|".join(
                re.escape(candidate)
                for candidate in sorted(replacements, key=len, reverse=True)
            )
        )
        if replacements
        else None
    )

    def redact_text(text: str) -> str:
        redacted = (
            text_sanitizer(text)
            if text_sanitizer is not None
            else text
        )
        if pattern is None:
            return redacted
        return pattern.sub(
            lambda match: f"«REDACTED:{replacements[match.group(0)]}»",
            redacted,
        )

    def visit(item: Any) -> Any:
        if isinstance(item, str):
            return redact_text(item)
        if isinstance(item, Mapping):
            return {
                redact_text(str(key)): visit(child)
                for key, child in item.items()
            }
        if isinstance(item, (tuple, list)):
            return [visit(child) for child in item]
        if item is None or type(item) in (bool, int, float):
            return item
        return "<unsupported-value>"

    def visit_data(item: Any) -> Any:
        if isinstance(item, str):
            return redact_text(item)
        if isinstance(item, Mapping):
            return {
                redact_text(str(key)): visit_data(child)
                for key, child in item.items()
            }
        if isinstance(item, (tuple, list)):
            return [visit_data(child) for child in item]
        if item is None:
            return None
        if type(item) in (bool, int, float):
            try:
                canonical = json.dumps(
                    item,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError):
                return "<unsupported-value>"
            redacted = redact_text(canonical)
            return item if redacted == canonical else redacted
        return "<unsupported-value>"

    return redact_text, visit, visit_data


def _replacement_map(
    canaries: tuple[tuple[str, str], ...],
) -> dict[str, str]:
    replacements: dict[str, str] = {}
    for name, value in canaries:
        variants = {value}
        try:
            variants.add(value.encode("unicode_escape").decode("ascii"))
            variants.add(json.dumps(value, ensure_ascii=True)[1:-1])
            variants.add(json.dumps(value, ensure_ascii=False)[1:-1])
            variants.add(repr(value)[1:-1])
        except (UnicodeError, ValueError):
            pass
        for variant in variants:
            if variant:
                replacements.setdefault(variant, name)
    return replacements


def _encoded_size(payload: Mapping[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
