"""Minimal, atomic updates for the global costume selection in visual.yaml."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

import yaml


_EDITABLE_KEYS = ("costume_mode", "selected_costume")


def write_costume_selection(config_path: str | Path, costume: str) -> None:
    """Persist a fixed costume while preserving unrelated YAML text."""

    path = Path(config_path)
    original = path.read_bytes().decode("utf-8")
    before = _load_mapping(original, path)
    replacements = {
        "costume_mode": "fixed",
        "selected_costume": costume,
    }

    updated = original
    for key in _EDITABLE_KEYS:
        updated = _replace_top_level_scalar(
            updated,
            key=key,
            scalar=_render_scalar(replacements[key]),
            key_existed=key in before,
        )

    after = _load_mapping(updated, path)
    expected = dict(before)
    expected.update(replacements)
    if after != expected:
        raise ValueError(
            "视觉配置最小编辑校验失败：候选 YAML 包含预期服装字段以外的语义变化。"
        )

    _atomic_replace(path, updated.encode("utf-8"))


def _load_mapping(text: str, path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"视觉配置 YAML 无效：{path}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"视觉配置必须是对象：{path}")
    return loaded


def _render_scalar(value: str) -> str:
    if value == "fixed":
        return value
    # JSON strings are single-line YAML scalars too, including names containing
    # punctuation that would be ambiguous in YAML's plain-scalar form.
    return json.dumps(value, ensure_ascii=False)


def _replace_top_level_scalar(
    text: str,
    *,
    key: str,
    scalar: str,
    key_existed: bool,
) -> str:
    lines = text.splitlines(keepends=True)
    pattern = re.compile(rf"^(?P<prefix>{re.escape(key)}[ \t]*:[ \t]*)(?P<body>.*)$")
    matches: list[int] = []
    for index, line in enumerate(lines):
        content, _ending = _split_line_ending(line)
        if pattern.fullmatch(content):
            matches.append(index)

    if len(matches) > 1:
        raise ValueError(f"视觉配置包含重复的顶层字段：{key}")
    if not matches:
        if key_existed:
            raise ValueError(f"视觉配置字段无法安全地最小编辑：{key}")
        return _append_top_level_scalar(text, key=key, scalar=scalar)

    index = matches[0]
    content, ending = _split_line_ending(lines[index])
    match = pattern.fullmatch(content)
    assert match is not None
    body = match.group("body")
    comment_at = _inline_comment_offset(body)
    value_part = body if comment_at is None else body[:comment_at]
    comment = "" if comment_at is None else body[comment_at:]
    trailing_space = value_part[len(value_part.rstrip(" \t")) :]
    lines[index] = f"{match.group('prefix')}{scalar}{trailing_space}{comment}{ending}"
    return "".join(lines)


def _append_top_level_scalar(text: str, *, key: str, scalar: str) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    if not text:
        return f"{key}: {scalar}"
    if text.endswith(("\n", "\r")):
        return f"{text}{key}: {scalar}{newline}"
    return f"{text}{newline}{key}: {scalar}"


def _split_line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1], line[-1]
    return line, ""


def _inline_comment_offset(value: str) -> int | None:
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(value):
        character = value[index]
        if quote == '"':
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
        elif quote == "'":
            if character == "'" and index + 1 < len(value) and value[index + 1] == "'":
                index += 1
            elif character == quote:
                quote = None
        elif character in {'"', "'"}:
            quote = character
        elif character == "#" and (index == 0 or value[index - 1].isspace()):
            return index
        index += 1
    return None


def _atomic_replace(path: Path, payload: bytes) -> None:
    from spica.adapters.config_platform import current_platform_capabilities
    native = current_platform_capabilities().native_files
    original_mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary_name = (native.create_temporary(path.parent, f'.{path.name}.') if native is not None
        else tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent))
    temporary_path = Path(temporary_name)
    try:
        if native is None:
            os.fchmod(descriptor, original_mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        if native is not None:
            # Native publication is atomic; Windows exposes no directory-fsync
            # guarantee equivalent to the POSIX operation below.
            native.sync_directory(path.parent)
            return
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
