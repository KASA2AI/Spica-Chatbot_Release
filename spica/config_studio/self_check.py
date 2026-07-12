"""Safe orchestration boundary for the existing ``scripts/self_check.py`` CLI.

This module deliberately does not import the script or read process environment.
Callers provide a complete, explicit environment mapping to the job manager.
"""

from __future__ import annotations

import json
import math
import re
import secrets
import stat
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from spica.config.env_roster import (
    APP_ENV_MAP,
    LEGACY_ENV_VARS,
    LEGACY_SECRET_ENV_VARS,
    RESPEAKER_ENV_MAP,
    RUNTIME_CACHE_ENV_MAP,
    SCREEN_ENV_MAP,
    SECRETS_ENV_MAP,
)


class SelfCheckMode(str, Enum):
    LIGHT = "light"
    FULL = "full"


class SelfCheckConsent(str, Enum):
    FULL = "full"
    LLM = "llm"
    INCLUDE_DISABLED = "include_disabled"
    MODEL_DOWNLOADS = "model_downloads"


class SelfCheckPlanError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


LIGHT_CHECKS = (
    "config",
    "gpu",
    "secrets",
    "tts",
    "stt",
    "moondream",
    "ocr",
    "song_uvr",
    "song_rvc",
    "llm",
)
HEAVY_CHECKS = (
    "tts",
    "stt",
    "moondream",
    "ocr",
    "song_uvr",
    "song_rvc",
    "llm",
)
REQUIRED_SELF_CHECK_ENV_NAMES = frozenset(
    (
        *APP_ENV_MAP.values(),
        *SCREEN_ENV_MAP.values(),
        *SECRETS_ENV_MAP.values(),
        *RESPEAKER_ENV_MAP.values(),
        *RUNTIME_CACHE_ENV_MAP.values(),
        *LEGACY_ENV_VARS,
    )
)


_PLAN_PROVENANCE = object()


@dataclass(frozen=True, init=False)
class SelfCheckPlan:
    mode: SelfCheckMode
    argv: tuple[str, ...]
    checks: tuple[str, ...]
    _script_path: str = field(repr=False, compare=False)
    _verify_script_file: bool = field(repr=False, compare=False)
    _provenance: object = field(repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("SelfCheckPlan must be created by SelfCheckPlanBuilder")

    @classmethod
    def _from_builder(
        cls,
        *,
        mode: SelfCheckMode,
        argv: tuple[str, ...],
        checks: tuple[str, ...],
        script_path: str,
        verify_script_file: bool,
    ) -> SelfCheckPlan:
        plan = object.__new__(cls)
        object.__setattr__(plan, "mode", mode)
        object.__setattr__(plan, "argv", argv)
        object.__setattr__(plan, "checks", checks)
        object.__setattr__(plan, "_script_path", script_path)
        object.__setattr__(plan, "_verify_script_file", verify_script_file)
        object.__setattr__(plan, "_provenance", _PLAN_PROVENANCE)
        return plan


class SelfCheckPlanBuilder:
    def __init__(
        self, *, script_path: Path, verify_script_file: bool = False
    ) -> None:
        if type(verify_script_file) is not bool:
            raise SelfCheckPlanError("NATIVE_BOOLEAN_REQUIRED")
        self._script_path = script_path.absolute()
        self._verify_script_file = verify_script_file

    def build(
        self,
        *,
        mode: SelfCheckMode = SelfCheckMode.LIGHT,
        only: tuple[str, ...] = (),
        llm: bool = False,
        include_disabled: bool = False,
        allow_model_downloads: bool = False,
        consents: frozenset[SelfCheckConsent] = frozenset(),
    ) -> SelfCheckPlan:
        if self._verify_script_file:
            try:
                script_mode = self._script_path.lstat().st_mode
            except OSError:
                raise SelfCheckPlanError("SELF_CHECK_SCRIPT_UNSAFE") from None
            if not stat.S_ISREG(script_mode):
                raise SelfCheckPlanError("SELF_CHECK_SCRIPT_UNSAFE")
        if any(
            type(flag) is not bool
            for flag in (llm, include_disabled, allow_model_downloads)
        ):
            raise SelfCheckPlanError("NATIVE_BOOLEAN_REQUIRED")
        if not isinstance(mode, SelfCheckMode):
            raise SelfCheckPlanError("INVALID_MODE")
        if isinstance(only, (str, bytes)):
            raise SelfCheckPlanError("CHECK_NOT_ALLOWLISTED")
        try:
            selected_checks = tuple(only)
            confirmed = frozenset(consents)
        except TypeError:
            raise SelfCheckPlanError("INVALID_PLAN_INPUT") from None
        if any(not isinstance(consent, SelfCheckConsent) for consent in confirmed):
            raise SelfCheckPlanError("INVALID_CONFIRMATION")
        if mode is SelfCheckMode.LIGHT and (
            selected_checks or llm or include_disabled or allow_model_downloads
        ):
            raise SelfCheckPlanError("FULL_MODE_REQUIRED")
        if mode is SelfCheckMode.FULL:
            if SelfCheckConsent.FULL not in confirmed:
                raise SelfCheckPlanError("FULL_CONFIRMATION_REQUIRED")
            if any(
                not isinstance(name, str) or name not in HEAVY_CHECKS
                for name in selected_checks
            ):
                raise SelfCheckPlanError("CHECK_NOT_ALLOWLISTED")
            if len(set(selected_checks)) != len(selected_checks):
                raise SelfCheckPlanError("DUPLICATE_CHECK")
            checks = selected_checks or HEAVY_CHECKS
            if llm and "llm" not in checks:
                raise SelfCheckPlanError("LLM_NOT_SELECTED")
            if llm and SelfCheckConsent.LLM not in confirmed:
                raise SelfCheckPlanError("LLM_CONFIRMATION_REQUIRED")
            if include_disabled and SelfCheckConsent.INCLUDE_DISABLED not in confirmed:
                raise SelfCheckPlanError("INCLUDE_DISABLED_CONFIRMATION_REQUIRED")
            if (
                allow_model_downloads
                and SelfCheckConsent.MODEL_DOWNLOADS not in confirmed
            ):
                raise SelfCheckPlanError("MODEL_DOWNLOAD_CONFIRMATION_REQUIRED")
            argv = [sys.executable, str(self._script_path), "--json", "--full"]
            if selected_checks:
                argv.extend(("--only", ",".join(selected_checks)))
            if llm:
                argv.append("--llm")
            if include_disabled:
                argv.append("--all")
            if allow_model_downloads:
                argv.append("--allow-model-downloads")
            return SelfCheckPlan._from_builder(
                mode=mode,
                argv=tuple(argv),
                checks=checks,
                script_path=str(self._script_path),
                verify_script_file=self._verify_script_file,
            )
        return SelfCheckPlan._from_builder(
            mode=SelfCheckMode.LIGHT,
            argv=(sys.executable, str(self._script_path), "--json"),
            checks=LIGHT_CHECKS,
            script_path=str(self._script_path),
            verify_script_file=self._verify_script_file,
        )


def _has_valid_plan_grammar(plan: object) -> bool:
    if type(plan) is not SelfCheckPlan:
        return False
    try:
        mode = plan.mode
        argv = plan.argv
        checks = plan.checks
        script_path = plan._script_path
        verify_script_file = plan._verify_script_file
        provenance = plan._provenance
    except AttributeError:
        return False
    if (
        provenance is not _PLAN_PROVENANCE
        or not isinstance(mode, SelfCheckMode)
        or not isinstance(argv, tuple)
        or any(not isinstance(item, str) for item in argv)
        or not isinstance(checks, tuple)
        or any(not isinstance(item, str) for item in checks)
        or not isinstance(script_path, str)
        or type(verify_script_file) is not bool
        or not Path(script_path).is_absolute()
        or len(argv) < 3
        or argv[0] != sys.executable
        or argv[1] != script_path
    ):
        return False
    if verify_script_file:
        try:
            if not stat.S_ISREG(Path(script_path).lstat().st_mode):
                return False
        except OSError:
            return False
    if mode is SelfCheckMode.LIGHT:
        return argv == (sys.executable, script_path, "--json") and checks == LIGHT_CHECKS
    if mode is not SelfCheckMode.FULL or argv[:4] != (
        sys.executable,
        script_path,
        "--json",
        "--full",
    ):
        return False
    cursor = 4
    if cursor < len(argv) and argv[cursor] == "--only":
        if cursor + 1 >= len(argv):
            return False
        selected = tuple(argv[cursor + 1].split(","))
        if (
            not selected
            or any(not name or name not in HEAVY_CHECKS for name in selected)
            or len(set(selected)) != len(selected)
            or selected != checks
        ):
            return False
        cursor += 2
    elif checks != HEAVY_CHECKS:
        return False
    for optional_flag in ("--llm", "--all", "--allow-model-downloads"):
        if cursor < len(argv) and argv[cursor] == optional_flag:
            if optional_flag == "--llm" and "llm" not in checks:
                return False
            cursor += 1
    return cursor == len(argv)


class SelfCheckJobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PASS = "PASS"
    UNVERIFIED = "UNVERIFIED"
    DEGRADED = "DEGRADED"
    FAIL = "FAIL"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class SelfCheckJobError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


TERMINAL_JOB_STATUSES = frozenset(
    {
        SelfCheckJobStatus.PASS,
        SelfCheckJobStatus.UNVERIFIED,
        SelfCheckJobStatus.DEGRADED,
        SelfCheckJobStatus.FAIL,
        SelfCheckJobStatus.CANCELLED,
        SelfCheckJobStatus.INTERNAL_ERROR,
    }
)


@dataclass(frozen=True)
class SelfCheckStderrSummary:
    progress_names: tuple[str, ...] = ()
    unclassified_line_count: int = 0
    total_line_count: int = 0
    truncated: bool = False
    exact_spica_running_precondition: bool = False


@dataclass(frozen=True)
class SelfCheckProcessOutcome:
    returncode: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)
    cleanup_confirmed: bool
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    stdout_utf8_valid: bool = True
    stderr_summary: SelfCheckStderrSummary | None = None


class SelfCheckProcess(Protocol):
    containment_established: bool

    def wait(self, timeout_s: float) -> SelfCheckProcessOutcome: ...

    def cancel(self) -> bool: ...

    def stderr_snapshot(self) -> SelfCheckStderrSummary: ...


class SelfCheckRunner(Protocol):
    def start(
        self, argv: tuple[str, ...], environment: Mapping[str, str]
    ) -> SelfCheckProcess: ...


@dataclass(frozen=True)
class SelfCheckResult:
    name: str
    status: str
    detail: dict[str, Any]
    reason: str = ""
    duration_s: float | None = None


@dataclass(frozen=True)
class SelfCheckProgress:
    name: str
    status: str = "RUNNING"


@dataclass(frozen=True)
class SelfCheckJobSnapshot:
    job_id: str
    mode: SelfCheckMode
    checks: tuple[str, ...]
    status: SelfCheckJobStatus
    duration_s: float
    results: tuple[SelfCheckResult, ...] = ()
    progress: tuple[SelfCheckProgress, ...] = ()
    error_code: str | None = None
    stderr_line_count: int = 0
    stderr_total_line_count: int = 0
    stderr_truncated: bool = False


@dataclass
class _Job:
    job_id: str
    plan: SelfCheckPlan
    status: SelfCheckJobStatus
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    results: tuple[SelfCheckResult, ...] = ()
    progress: tuple[SelfCheckProgress, ...] = ()
    error_code: str | None = None
    stderr_line_count: int = 0
    stderr_total_line_count: int = 0
    stderr_truncated: bool = False
    process: SelfCheckProcess | None = field(default=None, repr=False)
    launch_resolved: threading.Event = field(
        default_factory=threading.Event,
        repr=False,
    )
    cancel_before_launch: bool = field(default=False, repr=False)


_PROGRESS_RE = re.compile(
    r"^\[self-check\] running "
    r"(tts|stt|moondream|ocr|song_uvr|song_rvc|llm) "
    r"\(timeout ([0-9]+)s\)\.\.\.$"
)
SELF_CHECK_PROGRESS_TIMEOUTS = (
    ("tts", "300"),
    ("stt", "240"),
    ("moondream", "300"),
    ("ocr", "240"),
    ("song_uvr", "300"),
    ("song_rvc", "480"),
    ("llm", "60"),
)
_PROGRESS_TIMEOUTS = dict(SELF_CHECK_PROGRESS_TIMEOUTS)
_RESULT_STATUSES = frozenset(
    {"PASS", "UNVERIFIED", "DEGRADED", "FAIL", "SKIPPED_DISABLED"}
)
_RESULT_KEYS = frozenset({"name", "status", "detail", "reason", "duration_s"})
SPICA_RUNNING_PRECONDITION_STDERR = (
    "[self-check] FATAL: 检测到 Spica(qt_overlay) 正在运行。--full 会真加载模型并与"
    "应用争 GPU/显存——请先关闭应用，或用 --force 强行继续。\n"
)
_SPICA_RUNNING_STDERR = SPICA_RUNNING_PRECONDITION_STDERR


class _ProtocolError(ValueError):
    def __init__(self, code: str = "INVALID_SELF_CHECK_OUTPUT") -> None:
        super().__init__(code)
        self.code = code


def _exit_code_for_results(results: tuple[SelfCheckResult, ...]) -> int:
    statuses = {result.status for result in results}
    if "FAIL" in statuses:
        return 2
    if "DEGRADED" in statuses:
        return 1
    return 0


def _is_spica_running_precondition(
    plan: SelfCheckPlan,
    outcome: SelfCheckProcessOutcome,
    stderr_summary: SelfCheckStderrSummary,
    *,
    stdout_budget_bytes: int,
    max_value_depth: int,
    max_collection_items: int,
    max_string_chars: int,
) -> bool:
    if (
        plan.mode is not SelfCheckMode.FULL
        or outcome.returncode != 3
        or not stderr_summary.exact_spica_running_precondition
        or outcome.stdout_truncated
        or outcome.stderr_truncated
        or outcome.timed_out
        or outcome.stdout_utf8_valid is not True
    ):
        return False
    try:
        _validated_results(
            plan,
            outcome,
            stdout_budget_bytes=stdout_budget_bytes,
            max_value_depth=max_value_depth,
            max_collection_items=max_collection_items,
            max_string_chars=max_string_chars,
            validate_process_returncode=False,
        )
    except _ProtocolError:
        return True
    return False


def _is_well_formed_process_outcome(outcome: object) -> bool:
    return (
        isinstance(outcome, SelfCheckProcessOutcome)
        and type(outcome.returncode) is int
        and isinstance(outcome.stdout, str)
        and isinstance(outcome.stderr, str)
        and type(outcome.cleanup_confirmed) is bool
        and type(outcome.stdout_truncated) is bool
        and type(outcome.stderr_truncated) is bool
        and type(outcome.timed_out) is bool
        and type(outcome.stdout_utf8_valid) is bool
        and _stderr_summary_for_outcome(outcome) is not None
    )


def _stderr_summary_for_outcome(
    outcome: SelfCheckProcessOutcome,
) -> SelfCheckStderrSummary | None:
    supplied = outcome.stderr_summary
    if supplied is not None:
        if outcome.stderr or outcome.stderr_truncated != supplied.truncated:
            return None
        return supplied if _is_well_formed_stderr_summary(supplied) else None
    if not isinstance(outcome.stderr, str):
        return None
    progress_names: list[str] = []
    progressed: set[str] = set()
    unclassified = 0
    total = 0
    for line in outcome.stderr.splitlines():
        if not line:
            continue
        total += 1
        matched = _PROGRESS_RE.fullmatch(line)
        if (
            matched
            and _PROGRESS_TIMEOUTS.get(matched.group(1)) == matched.group(2)
            and matched.group(1) not in progressed
        ):
            progressed.add(matched.group(1))
            progress_names.append(matched.group(1))
        else:
            unclassified += 1
    return SelfCheckStderrSummary(
        progress_names=tuple(progress_names),
        unclassified_line_count=unclassified,
        total_line_count=total,
        truncated=outcome.stderr_truncated,
        exact_spica_running_precondition=outcome.stderr == _SPICA_RUNNING_STDERR,
    )


def _is_well_formed_stderr_summary(summary: object) -> bool:
    if not isinstance(summary, SelfCheckStderrSummary):
        return False
    progress_names = summary.progress_names
    return (
        isinstance(progress_names, tuple)
        and all(name in HEAVY_CHECKS for name in progress_names)
        and len(set(progress_names)) == len(progress_names)
        and type(summary.unclassified_line_count) is int
        and summary.unclassified_line_count >= 0
        and type(summary.total_line_count) is int
        and summary.total_line_count
        == summary.unclassified_line_count + len(progress_names)
        and type(summary.truncated) is bool
        and type(summary.exact_spica_running_precondition) is bool
        and (
            not summary.exact_spica_running_precondition
            or (
                summary.progress_names == ()
                and summary.unclassified_line_count == 1
                and summary.total_line_count == 1
            )
        )
    )


def _project_stderr_summary(
    plan: SelfCheckPlan, summary: SelfCheckStderrSummary
) -> tuple[tuple[SelfCheckProgress, ...], int]:
    planned = tuple(name for name in summary.progress_names if name in plan.checks)
    unplanned_count = len(summary.progress_names) - len(planned)
    return (
        tuple(SelfCheckProgress(name=name) for name in planned),
        summary.unclassified_line_count + unplanned_count,
    )


def _validated_results(
    plan: SelfCheckPlan,
    outcome: SelfCheckProcessOutcome,
    *,
    stdout_budget_bytes: int,
    max_value_depth: int,
    max_collection_items: int,
    max_string_chars: int,
    validate_process_returncode: bool = True,
) -> tuple[SelfCheckResult, ...]:
    if outcome.stdout_truncated:
        raise _ProtocolError("SELF_CHECK_OUTPUT_TRUNCATED")
    if outcome.timed_out:
        raise _ProtocolError("SELF_CHECK_TIMEOUT")
    if outcome.stdout_utf8_valid is not True:
        raise _ProtocolError
    if not isinstance(outcome.stdout, str):
        raise _ProtocolError
    if len(outcome.stdout.encode("utf-8")) > stdout_budget_bytes:
        raise _ProtocolError("SELF_CHECK_OUTPUT_LIMIT_EXCEEDED")
    try:
        payload = json.loads(outcome.stdout)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise _ProtocolError from exc
    if not isinstance(payload, dict) or set(payload) != {"mode", "results", "exit_code"}:
        raise _ProtocolError
    if payload["mode"] != plan.mode.value:
        raise _ProtocolError
    raw_results = payload["results"]
    if not isinstance(raw_results, list):
        raise _ProtocolError
    parsed: list[SelfCheckResult] = []
    names: list[str] = []
    for item in raw_results:
        if (
            not isinstance(item, dict)
            or not {"name", "status", "detail"}.issubset(item)
            or not set(item).issubset(_RESULT_KEYS)
        ):
            raise _ProtocolError
        name = item["name"]
        status = item["status"]
        detail = item["detail"]
        reason = item.get("reason", "")
        duration_s = item.get("duration_s")
        if (
            not isinstance(name, str)
            or not isinstance(status, str)
            or status not in _RESULT_STATUSES
            or not isinstance(detail, dict)
            or not isinstance(reason, str)
        ):
            raise _ProtocolError
        if len(name) > max_string_chars or len(reason) > max_string_chars:
            raise _ProtocolError
        _validate_json_value(
            detail,
            depth=0,
            max_depth=max_value_depth,
            max_collection_items=max_collection_items,
            max_string_chars=max_string_chars,
        )
        if duration_s is not None and (
            isinstance(duration_s, bool)
            or not isinstance(duration_s, (int, float))
            or not math.isfinite(duration_s)
            or duration_s < 0
        ):
            raise _ProtocolError
        names.append(name)
        parsed.append(
            SelfCheckResult(
                name=name,
                status=status,
                detail=detail,
                reason=reason,
                duration_s=float(duration_s) if duration_s is not None else None,
            )
        )
    if len(set(names)) != len(names) or set(names) != set(plan.checks):
        raise _ProtocolError
    results = tuple(parsed)
    recomputed = _exit_code_for_results(results)
    reported = payload["exit_code"]
    if (
        isinstance(reported, bool)
        or not isinstance(reported, int)
        or reported != recomputed
        or (validate_process_returncode and outcome.returncode != recomputed)
    ):
        raise _ProtocolError
    return results


def _validate_json_value(
    value: Any,
    *,
    depth: int,
    max_depth: int,
    max_collection_items: int,
    max_string_chars: int,
) -> None:
    if depth > max_depth:
        raise _ProtocolError
    if isinstance(value, str):
        if len(value) > max_string_chars:
            raise _ProtocolError
        return
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise _ProtocolError
        return
    if isinstance(value, dict):
        if len(value) > max_collection_items:
            raise _ProtocolError
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > max_string_chars:
                raise _ProtocolError
            _validate_json_value(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_collection_items=max_collection_items,
                max_string_chars=max_string_chars,
            )
        return
    if isinstance(value, list):
        if len(value) > max_collection_items:
            raise _ProtocolError
        for item in value:
            _validate_json_value(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_collection_items=max_collection_items,
                max_string_chars=max_string_chars,
            )
        return
    raise _ProtocolError


def _secret_variants(value: str) -> tuple[str, ...]:
    variants = {value}
    try:
        variants.add(value.encode("unicode_escape").decode("ascii"))
        variants.add(json.dumps(value, ensure_ascii=True)[1:-1])
        variants.add(json.dumps(value, ensure_ascii=False)[1:-1])
        variants.add(repr(value)[1:-1])
    except (UnicodeError, ValueError):
        pass
    return tuple(sorted((item for item in variants if item), key=len, reverse=True))


def _redact_text(
    text: str,
    secrets_by_name: tuple[tuple[str, str], ...],
    secret_material_sanitizer: Callable[[str], str] | None = None,
) -> str:
    if secret_material_sanitizer is not None:
        try:
            text = secret_material_sanitizer(text)
        except Exception:  # noqa: BLE001 -- owner failures become a stable job code
            raise _ProtocolError("SELF_CHECK_SECRET_SANITIZATION_FAILED") from None
        if not isinstance(text, str):
            raise _ProtocolError("SELF_CHECK_SECRET_SANITIZATION_FAILED")
    variants: dict[str, str] = {}
    for env_name, secret in secrets_by_name:
        for variant in _secret_variants(secret):
            variants.setdefault(variant, env_name)
    if not variants:
        return text
    pattern = re.compile(
        "|".join(
            re.escape(item)
            for item in sorted(variants, key=len, reverse=True)
        )
    )
    return pattern.sub(lambda matched: f"«REDACTED:{variants[matched.group(0)]}»", text)


def _redact_value(
    value: Any,
    secrets_by_name: tuple[tuple[str, str], ...],
    secret_material_sanitizer: Callable[[str], str] | None = None,
) -> Any:
    if isinstance(value, str):
        return _redact_text(
            value,
            secrets_by_name,
            secret_material_sanitizer,
        )
    if isinstance(value, dict):
        return {
            _redact_text(
                key,
                secrets_by_name,
                secret_material_sanitizer,
            ): _redact_value(
                item,
                secrets_by_name,
                secret_material_sanitizer,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _redact_value(
                item,
                secrets_by_name,
                secret_material_sanitizer,
            )
            for item in value
        ]
    if type(value) in (bool, int, float):
        try:
            canonical = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            raise _ProtocolError("INVALID_SELF_CHECK_OUTPUT") from None
        redacted = _redact_text(
            canonical,
            secrets_by_name,
            secret_material_sanitizer,
        )
        return value if redacted == canonical else redacted
    return value


def _redacted_results(
    results: tuple[SelfCheckResult, ...],
    secrets_by_name: tuple[tuple[str, str], ...],
    secret_material_sanitizer: Callable[[str], str] | None = None,
) -> tuple[SelfCheckResult, ...]:
    return tuple(
        SelfCheckResult(
            name=result.name,
            status=result.status,
            detail=_redact_value(
                result.detail,
                secrets_by_name,
                secret_material_sanitizer,
            ),
            reason=_redact_text(
                result.reason,
                secrets_by_name,
                secret_material_sanitizer,
            ),
            duration_s=result.duration_s,
        )
        for result in results
    )


def _validate_redacted_results(
    results: tuple[SelfCheckResult, ...],
    *,
    stdout_budget_bytes: int,
    max_value_depth: int,
    max_collection_items: int,
    max_string_chars: int,
) -> None:
    wire_results: list[dict[str, Any]] = []
    for result in results:
        if len(result.reason) > max_string_chars:
            raise _ProtocolError
        _validate_json_value(
            result.detail,
            depth=0,
            max_depth=max_value_depth,
            max_collection_items=max_collection_items,
            max_string_chars=max_string_chars,
        )
        wire_results.append(
            {
                "name": result.name,
                "status": result.status,
                "detail": result.detail,
                "reason": result.reason,
                "duration_s": result.duration_s,
            }
        )
    encoded = json.dumps(wire_results, ensure_ascii=False).encode("utf-8")
    if len(encoded) > stdout_budget_bytes:
        raise _ProtocolError


class SelfCheckJobManager:
    def __init__(
        self,
        *,
        runner: SelfCheckRunner,
        hard_timeout_s: float = 900.0,
        stdout_budget_bytes: int = 256_000,
        max_value_depth: int = 8,
        max_collection_items: int = 128,
        max_string_chars: int = 4_096,
        max_terminal_jobs: int = 20,
        shutdown_launch_timeout_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] = lambda: secrets.token_urlsafe(18),
    ) -> None:
        self._runner = runner
        self._hard_timeout_s = hard_timeout_s
        self._stdout_budget_bytes = stdout_budget_bytes
        self._max_value_depth = max_value_depth
        self._max_collection_items = max_collection_items
        self._max_string_chars = max_string_chars
        if max_terminal_jobs < 1:
            raise ValueError("max_terminal_jobs must be positive")
        if shutdown_launch_timeout_s <= 0:
            raise ValueError("shutdown launch timeout must be positive")
        self._max_terminal_jobs = max_terminal_jobs
        self._shutdown_launch_timeout_s = shutdown_launch_timeout_s
        self._clock = clock
        self._id_factory = id_factory
        self._lock = threading.RLock()
        self._jobs: dict[str, _Job] = {}
        self._active_id: str | None = None
        self._terminal_ids: deque[str] = deque()
        self._terminal_recorded: set[str] = set()
        self._unsafe = False
        self._shutdown = False

    def start(
        self,
        plan: SelfCheckPlan,
        environment: Mapping[str, str],
        *,
        secret_material_sanitizer: Callable[[str], str] | None = None,
    ) -> SelfCheckJobSnapshot:
        with self._lock:
            if self._shutdown:
                raise SelfCheckJobError("SELF_CHECK_MANAGER_SHUTDOWN") from None
            if self._unsafe:
                raise SelfCheckJobError("SELF_CHECK_MANAGER_UNSAFE") from None
        if not _has_valid_plan_grammar(plan):
            raise SelfCheckJobError("SELF_CHECK_PLAN_INVALID") from None
        try:
            explicit_environment = dict(environment)
        except Exception:  # noqa: BLE001 -- never expose mapping repr/details
            raise SelfCheckJobError("INVALID_CHILD_ENVIRONMENT") from None
        if any(
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\x00" in key
            or not isinstance(value, str)
            or "\x00" in value
            for key, value in explicit_environment.items()
        ):
            raise SelfCheckJobError("INVALID_CHILD_ENVIRONMENT")
        if not REQUIRED_SELF_CHECK_ENV_NAMES.issubset(explicit_environment):
            raise SelfCheckJobError("INVALID_CHILD_ENVIRONMENT")
        if secret_material_sanitizer is not None and not callable(
            secret_material_sanitizer
        ):
            raise SelfCheckJobError("INVALID_CHILD_ENVIRONMENT")
        secrets_by_name = tuple(
            sorted(
                (
                    (env_name, explicit_environment.get(env_name, ""))
                    for env_name in (
                        *SECRETS_ENV_MAP.values(),
                        *LEGACY_SECRET_ENV_VARS,
                    )
                    if explicit_environment.get(env_name, "")
                ),
                key=lambda item: len(item[1]),
                reverse=True,
            )
        )
        with self._lock:
            if self._shutdown:
                raise SelfCheckJobError("SELF_CHECK_MANAGER_SHUTDOWN") from None
            if self._unsafe:
                raise SelfCheckJobError("SELF_CHECK_MANAGER_UNSAFE") from None
            if self._active_id is not None:
                raise SelfCheckJobError("SELF_CHECK_BUSY")
            job = _Job(
                job_id=self._new_id(),
                plan=plan,
                status=SelfCheckJobStatus.QUEUED,
                created_at=self._clock(),
            )
            self._jobs[job.job_id] = job
            self._active_id = job.job_id
            queued_snapshot = self._snapshot(job)
        try:
            process = self._runner.start(plan.argv, explicit_environment)
        except Exception:  # noqa: BLE001 -- boundary exceptions never reach the API
            with self._lock:
                if job.cancel_before_launch:
                    if job.status is not SelfCheckJobStatus.CANCELLED:
                        job.launch_resolved.set()
                        return self._snapshot(job)
                    if self._active_id == job.job_id:
                        self._active_id = None
                    self._record_terminal(job)
                else:
                    self._finish_internal(job, "PROCESS_START_FAILED")
                job.launch_resolved.set()
            return self._snapshot(job)
        if process.containment_established is not True:
            try:
                cleanup_confirmed = process.cancel()
            except Exception:  # noqa: BLE001 -- stable API error below
                cleanup_confirmed = False
            with self._lock:
                self._finish_internal(
                    job,
                    "PROCESS_CONTAINMENT_UNAVAILABLE"
                    if cleanup_confirmed is True
                    else "CONTAINMENT_CLEANUP_UNCONFIRMED",
                    unsafe=cleanup_confirmed is not True,
                )
                job.launch_resolved.set()
                return self._snapshot(job)
        with self._lock:
            cancelled_before_process = job.cancel_before_launch
            if not cancelled_before_process:
                job.process = process
                job.started_at = self._clock()
                job.status = SelfCheckJobStatus.RUNNING
                job.launch_resolved.set()
        if cancelled_before_process:
            try:
                cleanup_confirmed = process.cancel()
            except Exception:  # noqa: BLE001 -- stable API error below
                cleanup_confirmed = False
            with self._lock:
                if cleanup_confirmed is True:
                    if job.status is SelfCheckJobStatus.CANCELLED:
                        if self._active_id == job.job_id:
                            self._active_id = None
                        self._record_terminal(job)
                else:
                    self._finish_internal(
                        job, "CONTAINMENT_CANCEL_UNCONFIRMED", unsafe=True
                    )
                job.launch_resolved.set()
                return self._snapshot(job)
        thread = threading.Thread(
            target=self._wait_for_process,
            args=(job.job_id, secrets_by_name, secret_material_sanitizer),
            name=f"config-studio-self-check-{job.job_id}",
            daemon=True,
        )
        thread.start()
        return queued_snapshot

    def get(self, job_id: str) -> SelfCheckJobSnapshot:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise SelfCheckJobError("SELF_CHECK_JOB_NOT_FOUND") from None
            return self._snapshot(job)

    def list(self) -> tuple[SelfCheckJobSnapshot, ...]:
        """Return the active job followed by newest retained terminal jobs."""
        with self._lock:
            ordered_ids: list[str] = []
            if self._active_id is not None:
                ordered_ids.append(self._active_id)
            ordered_ids.extend(reversed(self._terminal_ids))
            return tuple(self._snapshot(self._jobs[job_id]) for job_id in ordered_ids)

    def shutdown(self) -> tuple[SelfCheckJobSnapshot, ...]:
        """Idempotently close the manager via the normal cancellation path."""
        with self._lock:
            self._shutdown = True
            active_id = self._active_id
            active_job = self._jobs.get(active_id) if active_id is not None else None
        if active_id is not None:
            self.cancel(active_id)
        if active_job is not None and not active_job.launch_resolved.wait(
            self._shutdown_launch_timeout_s
        ):
            with self._lock:
                if not active_job.launch_resolved.is_set():
                    active_job.cancel_before_launch = True
                    self._finish_internal(
                        active_job,
                        "PROCESS_START_SHUTDOWN_TIMEOUT",
                        unsafe=True,
                    )
        return self.list()

    def cancel(self, job_id: str) -> SelfCheckJobSnapshot:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise SelfCheckJobError("SELF_CHECK_JOB_NOT_FOUND") from None
            if job.status in TERMINAL_JOB_STATUSES:
                return self._snapshot(job)
            if job.status is SelfCheckJobStatus.QUEUED and job.process is None:
                job.cancel_before_launch = True
                job.status = SelfCheckJobStatus.CANCELLED
                job.finished_at = self._clock()
                return self._snapshot(job)
            job.status = SelfCheckJobStatus.CANCELLING
            process = job.process
        cleanup_confirmed = False
        if process is not None:
            try:
                cleanup_confirmed = process.cancel()
            except Exception:  # noqa: BLE001 -- stable API error below
                cleanup_confirmed = False
        with self._lock:
            if job.status is SelfCheckJobStatus.CANCELLING:
                if cleanup_confirmed is True:
                    job.status = SelfCheckJobStatus.CANCELLED
                    job.finished_at = self._clock()
                    if self._active_id == job.job_id:
                        self._active_id = None
                    self._record_terminal(job)
                else:
                    self._finish_internal(
                        job, "CONTAINMENT_CANCEL_UNCONFIRMED", unsafe=True
                    )
            return self._snapshot(job)

    def _wait_for_process(
        self,
        job_id: str,
        secrets_by_name: tuple[tuple[str, str], ...],
        secret_material_sanitizer: Callable[[str], str] | None,
    ) -> None:
        with self._lock:
            job = self._jobs[job_id]
            process = job.process
        if process is None:
            return
        try:
            outcome = process.wait(self._hard_timeout_s)
        except TimeoutError:
            with self._lock:
                if job.status is not SelfCheckJobStatus.RUNNING:
                    return
            try:
                cleanup_confirmed = process.cancel()
            except Exception:  # noqa: BLE001 -- stable API error below
                cleanup_confirmed = False
            with self._lock:
                if job.status is not SelfCheckJobStatus.RUNNING:
                    return
                self._finish_internal(
                    job,
                    "SELF_CHECK_TIMEOUT"
                    if cleanup_confirmed is True
                    else "CONTAINMENT_CANCEL_UNCONFIRMED",
                    unsafe=cleanup_confirmed is not True,
                )
            return
        except Exception:  # noqa: BLE001 -- normalized below
            with self._lock:
                if job.status is not SelfCheckJobStatus.RUNNING:
                    return
            try:
                cleanup_confirmed = process.cancel()
            except Exception:  # noqa: BLE001 -- stable API error below
                cleanup_confirmed = False
            with self._lock:
                if job.status is not SelfCheckJobStatus.RUNNING:
                    return
                self._finish_internal(
                    job,
                    "PROCESS_WAIT_FAILED"
                    if cleanup_confirmed is True
                    else "CONTAINMENT_CANCEL_UNCONFIRMED",
                    unsafe=cleanup_confirmed is not True,
                )
            return
        if not _is_well_formed_process_outcome(outcome):
            try:
                cleanup_confirmed = process.cancel()
            except Exception:  # noqa: BLE001 -- stable API error below
                cleanup_confirmed = False
            with self._lock:
                if job.status is not SelfCheckJobStatus.RUNNING:
                    return
                self._finish_internal(
                    job,
                    "INVALID_PROCESS_OUTCOME"
                    if cleanup_confirmed is True
                    else "CONTAINMENT_CLEANUP_UNCONFIRMED",
                    unsafe=cleanup_confirmed is not True,
                )
            return
        stderr_summary = _stderr_summary_for_outcome(outcome)
        if stderr_summary is None:
            return
        if outcome.cleanup_confirmed is not True:
            try:
                cleanup_confirmed = process.cancel()
            except Exception:  # noqa: BLE001 -- stable API error below
                cleanup_confirmed = False
            with self._lock:
                if job.status is not SelfCheckJobStatus.RUNNING:
                    return
                if cleanup_confirmed is not True:
                    self._finish_internal(
                        job, "CONTAINMENT_CLEANUP_UNCONFIRMED", unsafe=True
                    )
                    return
        with self._lock:
            if job.status is not SelfCheckJobStatus.RUNNING:
                return
            progress, unclassified_lines = _project_stderr_summary(
                job.plan, stderr_summary
            )
            job.progress = progress
            job.stderr_line_count = unclassified_lines
            job.stderr_total_line_count = stderr_summary.total_line_count
            job.stderr_truncated = stderr_summary.truncated
            if _is_spica_running_precondition(
                job.plan,
                outcome,
                stderr_summary,
                stdout_budget_bytes=self._stdout_budget_bytes,
                max_value_depth=self._max_value_depth,
                max_collection_items=self._max_collection_items,
                max_string_chars=self._max_string_chars,
            ):
                self._finish_internal(job, "PRECONDITION_SPICA_RUNNING")
                return
            try:
                results = _validated_results(
                    job.plan,
                    outcome,
                    stdout_budget_bytes=self._stdout_budget_bytes,
                    max_value_depth=self._max_value_depth,
                    max_collection_items=self._max_collection_items,
                    max_string_chars=self._max_string_chars,
                )
            except _ProtocolError as exc:
                self._finish_internal(job, exc.code)
                return
            try:
                results = _redacted_results(
                    results,
                    secrets_by_name,
                    secret_material_sanitizer,
                )
            except _ProtocolError as exc:
                self._finish_internal(job, exc.code)
                return
            try:
                _validate_redacted_results(
                    results,
                    stdout_budget_bytes=self._stdout_budget_bytes,
                    max_value_depth=self._max_value_depth,
                    max_collection_items=self._max_collection_items,
                    max_string_chars=self._max_string_chars,
                )
            except _ProtocolError:
                self._finish_internal(
                    job, "SELF_CHECK_REDACTED_OUTPUT_LIMIT_EXCEEDED"
                )
                return
            job.results = results
            statuses = {result.status for result in results}
            if "FAIL" in statuses:
                job.status = SelfCheckJobStatus.FAIL
            elif "DEGRADED" in statuses:
                job.status = SelfCheckJobStatus.DEGRADED
            elif statuses & {"UNVERIFIED", "SKIPPED_DISABLED"}:
                job.status = SelfCheckJobStatus.UNVERIFIED
            else:
                job.status = SelfCheckJobStatus.PASS
            job.finished_at = self._clock()
            self._active_id = None
            self._record_terminal(job)

    def _new_id(self) -> str:
        for _ in range(8):
            try:
                candidate = self._id_factory()
            except Exception:  # noqa: BLE001 -- random boundary details are private
                break
            if (
                isinstance(candidate, str)
                and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", candidate)
                and candidate not in self._jobs
            ):
                return candidate
        raise SelfCheckJobError("SELF_CHECK_JOB_ID_UNAVAILABLE") from None

    def _finish_internal(self, job: _Job, code: str, *, unsafe: bool = False) -> None:
        job.status = SelfCheckJobStatus.INTERNAL_ERROR
        job.error_code = code
        job.finished_at = self._clock()
        if unsafe:
            self._unsafe = True
        if self._active_id == job.job_id:
            self._active_id = None
        self._record_terminal(job)

    def _record_terminal(self, job: _Job) -> None:
        if job.job_id in self._terminal_recorded:
            return
        job.process = None
        self._terminal_recorded.add(job.job_id)
        self._terminal_ids.append(job.job_id)
        while len(self._terminal_ids) > self._max_terminal_jobs:
            expired_id = self._terminal_ids.popleft()
            self._terminal_recorded.discard(expired_id)
            self._jobs.pop(expired_id, None)

    def _snapshot(self, job: _Job) -> SelfCheckJobSnapshot:
        end = job.finished_at if job.finished_at is not None else self._clock()
        start = job.started_at if job.started_at is not None else job.created_at
        progress = job.progress
        stderr_line_count = job.stderr_line_count
        stderr_total_line_count = job.stderr_total_line_count
        stderr_truncated = job.stderr_truncated
        if job.status not in TERMINAL_JOB_STATUSES and job.process is not None:
            try:
                live_summary = job.process.stderr_snapshot()
            except Exception:  # noqa: BLE001 -- live progress is best-effort metadata
                live_summary = None
            if _is_well_formed_stderr_summary(live_summary):
                progress, stderr_line_count = _project_stderr_summary(
                    job.plan, live_summary
                )
                stderr_total_line_count = live_summary.total_line_count
                stderr_truncated = live_summary.truncated
        return SelfCheckJobSnapshot(
            job_id=job.job_id,
            mode=job.plan.mode,
            checks=job.plan.checks,
            status=job.status,
            duration_s=max(0.0, end - start),
            results=job.results,
            progress=progress,
            error_code=job.error_code,
            stderr_line_count=stderr_line_count,
            stderr_total_line_count=stderr_total_line_count,
            stderr_truncated=stderr_truncated,
        )
