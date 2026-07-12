"""Production service seam for bounded Config Studio self-check jobs.

The service owns plan construction, transient child-environment synthesis and
job DTO projection.  It never reads process globals: callers provide the latest
non-sensitive environment snapshot and separately held secret slots for each
job start.
"""

from __future__ import annotations

import hmac
import json
import math
import re
import secrets as token_secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from spica.config.environment_snapshot import EnvironmentSnapshot
from spica.config.env_roster import (
    LEGACY_SECRET_ENV_VARS,
    SECRETS_ENV_MAP,
)
from spica.ports.config_studio_platform import PlatformCapabilities
from spica.config.secrets import Secrets
from spica.config_studio.self_check import (
    REQUIRED_SELF_CHECK_ENV_NAMES,
    SelfCheckConsent,
    SelfCheckJobError,
    SelfCheckJobManager,
    SelfCheckJobSnapshot,
    SelfCheckMode,
    SelfCheckPlanBuilder,
    SelfCheckPlanError,
    SelfCheckPlan,
    SelfCheckRunner,
)


@dataclass(frozen=True, slots=True)
class SelfCheckEnvironmentInputs:
    """References to the latest explicit config and separately owned secrets."""

    environment_snapshot: EnvironmentSnapshot
    secrets: Secrets
    legacy_secret_canaries: tuple[tuple[str, str], ...] = ()
    secret_material_sanitizer: Callable[[str], str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.environment_snapshot, EnvironmentSnapshot):
            raise TypeError("environment_snapshot must be EnvironmentSnapshot")
        if not isinstance(self.secrets, Secrets):
            raise TypeError("secrets must be Secrets")
        if self.secret_material_sanitizer is not None and not callable(
            self.secret_material_sanitizer
        ):
            raise TypeError("secret material sanitizer must be callable")
        names: set[str] = set()
        for item in self.legacy_secret_canaries:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or item[0] not in LEGACY_SECRET_ENV_VARS
                or not isinstance(item[1], str)
                or item[0] in names
            ):
                raise TypeError("legacy secret canaries must be unique roster pairs")
            names.add(item[0])

    def __repr__(self) -> str:
        return "SelfCheckEnvironmentInputs(<redacted>)"


@dataclass(frozen=True, slots=True)
class SelfCheckAcknowledgements:
    """Independent user acknowledgements presented before receipt issuance."""

    full: bool = False
    llm: bool = False
    include_disabled: bool = False
    model_downloads: bool = False

    def __post_init__(self) -> None:
        if any(
            type(value) is not bool
            for value in (
                self.full,
                self.llm,
                self.include_disabled,
                self.model_downloads,
            )
        ):
            raise TypeError("self-check acknowledgements must be native booleans")


@dataclass(frozen=True, slots=True)
class _CommandSemantic:
    mode: SelfCheckMode
    only: tuple[str, ...]
    llm: bool
    include_disabled: bool
    allow_model_downloads: bool


@dataclass(frozen=True, slots=True)
class _ReceiptRecord:
    session_id: str
    semantic: _CommandSemantic
    expires_at: float


class SelfCheckService:
    """Compose safe plans and project manager snapshots to bounded wire DTOs."""

    def __init__(
        self,
        *,
        script_path: Path,
        job_manager: SelfCheckJobManager,
        environment_inputs: Callable[[], SelfCheckEnvironmentInputs],
        base_child_environment: Mapping[str, str] | None = None,
        receipt_ttl_s: float = 120.0,
        max_receipts: int = 32,
        clock: Callable[[], float] = time.monotonic,
        receipt_factory: Callable[[], str] = lambda: token_secrets.token_urlsafe(24),
    ) -> None:
        if not isinstance(job_manager, SelfCheckJobManager):
            raise TypeError("job_manager must be SelfCheckJobManager")
        if not callable(environment_inputs):
            raise TypeError("environment_inputs must be callable")
        self._plan_builder = SelfCheckPlanBuilder(
            script_path=Path(script_path),
            verify_script_file=True,
        )
        # Verify the production owner script before advertising the capability.
        self._plan_builder.build()
        self._job_manager = job_manager
        self._environment_inputs = environment_inputs
        self._base_child_environment = _validated_base_environment(
            base_child_environment or {}
        )
        if not math.isfinite(receipt_ttl_s) or receipt_ttl_s <= 0:
            raise ValueError("receipt_ttl_s must be finite and positive")
        if type(max_receipts) is not int or max_receipts < 1:
            raise ValueError("max_receipts must be positive")
        if not callable(clock) or not callable(receipt_factory):
            raise TypeError("clock and receipt_factory must be callable")
        self._receipt_ttl_s = receipt_ttl_s
        self._max_receipts = max_receipts
        self._clock = clock
        self._receipt_factory = receipt_factory
        self._receipt_lock = threading.Lock()
        self._receipts: dict[str, _ReceiptRecord] = {}
        self._available = True

    @property
    def available(self) -> bool:
        return self._available

    def prepare_heavy(
        self,
        command: object,
        *,
        acknowledgements: SelfCheckAcknowledgements,
        session_id: str,
    ) -> dict[str, object]:
        semantic = _command_semantic(command)
        if semantic.mode is not SelfCheckMode.FULL:
            raise SelfCheckPlanError("FULL_MODE_REQUIRED")
        if not isinstance(acknowledgements, SelfCheckAcknowledgements):
            raise SelfCheckPlanError("INVALID_CONFIRMATION")
        normalized_session = _session_id(session_id)
        plan = self._build_plan(
            semantic,
            acknowledgements=acknowledgements,
        )
        now = self._clock()
        if not math.isfinite(now):
            raise SelfCheckPlanError("SELF_CHECK_CONFIRMATION_UNAVAILABLE")
        with self._receipt_lock:
            self._prune_receipts(now)
            if len(self._receipts) >= self._max_receipts:
                raise SelfCheckPlanError("SELF_CHECK_CONFIRMATION_CAPACITY")
            receipt = self._new_receipt()
            self._receipts[receipt] = _ReceiptRecord(
                session_id=normalized_session,
                semantic=semantic,
                expires_at=now + self._receipt_ttl_s,
            )
        return {
            "confirmation_receipt": receipt,
            "expires_in_s": self._receipt_ttl_s,
            "semantic": {
                "mode": plan.mode.value,
                "checks": list(plan.checks),
                "llm": semantic.llm,
                "include_disabled": semantic.include_disabled,
                "allow_model_downloads": semantic.allow_model_downloads,
            },
        }

    def start(
        self,
        command: object,
        *,
        session_id: str | None = None,
        confirmation_receipt: str | None = None,
    ) -> dict[str, object]:
        if not self._available:
            raise SelfCheckJobError("SELF_CHECK_MANAGER_UNSAFE")
        semantic = _command_semantic(command)
        confirmed = False
        if semantic.mode is SelfCheckMode.FULL:
            if confirmation_receipt is None or session_id is None:
                raise SelfCheckPlanError("FULL_CONFIRMATION_REQUIRED")
            self._consume_receipt(
                confirmation_receipt,
                session_id=_session_id(session_id),
                semantic=semantic,
            )
            confirmed = True
        elif confirmation_receipt is not None or session_id is not None:
            raise SelfCheckPlanError("SELF_CHECK_CONFIRMATION_UNEXPECTED")
        plan = self._build_plan(semantic, receipt_confirmed=confirmed)
        try:
            inputs = self._environment_inputs()
            if not isinstance(inputs, SelfCheckEnvironmentInputs):
                raise TypeError
            child_environment = _child_environment(
                inputs,
                base_environment=self._base_child_environment,
            )
        except Exception:
            raise SelfCheckJobError("INVALID_CHILD_ENVIRONMENT") from None
        try:
            started = self._job_manager.start(
                plan,
                child_environment,
                secret_material_sanitizer=inputs.secret_material_sanitizer,
            )
        except SelfCheckJobError as exc:
            if exc.code == "SELF_CHECK_MANAGER_UNSAFE":
                self._available = False
            raise
        return self._to_wire(started)

    def list(self) -> list[dict[str, object]]:
        return [self._to_wire(item) for item in self._job_manager.list()]

    def get(self, job_id: str) -> dict[str, object]:
        return self._to_wire(self._job_manager.get(job_id))

    def cancel(self, job_id: str) -> dict[str, object]:
        return self._to_wire(self._job_manager.cancel(job_id))

    def shutdown(self) -> list[dict[str, object]]:
        with self._receipt_lock:
            self._receipts.clear()
        return [self._to_wire(item) for item in self._job_manager.shutdown()]

    def _build_plan(
        self,
        semantic: _CommandSemantic,
        *,
        acknowledgements: SelfCheckAcknowledgements | None = None,
        receipt_confirmed: bool = False,
    ) -> SelfCheckPlan:
        consents: frozenset[SelfCheckConsent] = frozenset()
        if receipt_confirmed:
            items = {SelfCheckConsent.FULL}
            if semantic.llm:
                items.add(SelfCheckConsent.LLM)
            if semantic.include_disabled:
                items.add(SelfCheckConsent.INCLUDE_DISABLED)
            if semantic.allow_model_downloads:
                items.add(SelfCheckConsent.MODEL_DOWNLOADS)
            consents = frozenset(items)
        elif acknowledgements is not None:
            if (
                acknowledgements.llm and not semantic.llm
            ) or (
                acknowledgements.include_disabled
                and not semantic.include_disabled
            ) or (
                acknowledgements.model_downloads
                and not semantic.allow_model_downloads
            ):
                raise SelfCheckPlanError("SELF_CHECK_CONFIRMATION_MISMATCH")
            items = set()
            if acknowledgements.full:
                items.add(SelfCheckConsent.FULL)
            if acknowledgements.llm:
                items.add(SelfCheckConsent.LLM)
            if acknowledgements.include_disabled:
                items.add(SelfCheckConsent.INCLUDE_DISABLED)
            if acknowledgements.model_downloads:
                items.add(SelfCheckConsent.MODEL_DOWNLOADS)
            consents = frozenset(items)
        return self._plan_builder.build(
            mode=semantic.mode,
            only=semantic.only,
            llm=semantic.llm,
            include_disabled=semantic.include_disabled,
            allow_model_downloads=semantic.allow_model_downloads,
            consents=consents,
        )

    def _consume_receipt(
        self,
        receipt: object,
        *,
        session_id: str,
        semantic: _CommandSemantic,
    ) -> None:
        if not isinstance(receipt, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,256}", receipt
        ):
            raise SelfCheckPlanError("SELF_CHECK_CONFIRMATION_INVALID")
        now = self._clock()
        if not math.isfinite(now):
            raise SelfCheckPlanError("SELF_CHECK_CONFIRMATION_UNAVAILABLE")
        with self._receipt_lock:
            matched = next(
                (
                    name
                    for name in self._receipts
                    if hmac.compare_digest(name, receipt)
                ),
                None,
            )
            if matched is None:
                raise SelfCheckPlanError("SELF_CHECK_CONFIRMATION_INVALID")
            record = self._receipts[matched]
            if record.expires_at <= now:
                self._receipts.pop(matched, None)
                raise SelfCheckPlanError("SELF_CHECK_CONFIRMATION_EXPIRED")
            if not hmac.compare_digest(record.session_id, session_id):
                raise SelfCheckPlanError("SELF_CHECK_CONFIRMATION_MISMATCH")
            if record.semantic != semantic:
                raise SelfCheckPlanError("SELF_CHECK_CONFIRMATION_MISMATCH")
            self._receipts.pop(matched, None)

    def _prune_receipts(self, now: float) -> None:
        for receipt, record in tuple(self._receipts.items()):
            if record.expires_at <= now:
                self._receipts.pop(receipt, None)

    def _new_receipt(self) -> str:
        for _ in range(8):
            try:
                candidate = self._receipt_factory()
            except Exception:
                break
            if (
                isinstance(candidate, str)
                and re.fullmatch(r"[A-Za-z0-9_-]{1,256}", candidate)
                and candidate not in self._receipts
            ):
                return candidate
        raise SelfCheckPlanError("SELF_CHECK_CONFIRMATION_UNAVAILABLE") from None

    def _to_wire(self, snapshot: SelfCheckJobSnapshot) -> dict[str, object]:
        details = json.loads(
            json.dumps(
                [result.detail for result in snapshot.results],
                ensure_ascii=False,
                allow_nan=False,
            )
        )
        dto: dict[str, object] = {
            "job_id": snapshot.job_id,
            "mode": snapshot.mode.value,
            "checks": list(snapshot.checks),
            "status": snapshot.status.value,
            "duration_s": snapshot.duration_s,
            "results": [
                {
                    "name": result.name,
                    "status": result.status,
                    "detail": detail,
                    "reason": result.reason,
                    "duration_s": result.duration_s,
                }
                for result, detail in zip(snapshot.results, details, strict=True)
            ],
            "progress": [
                {"name": item.name, "status": item.status}
                for item in snapshot.progress
            ],
            "error_code": snapshot.error_code,
            "stderr_line_count": snapshot.stderr_line_count,
            "stderr_total_line_count": snapshot.stderr_total_line_count,
            "stderr_truncated": snapshot.stderr_truncated,
        }
        if snapshot.error_code in {
            "PROCESS_CONTAINMENT_UNAVAILABLE",
            "CONTAINMENT_CLEANUP_UNCONFIRMED",
            "CONTAINMENT_CANCEL_UNCONFIRMED",
        }:
            self._available = False
        return dto


def _command_semantic(command: object) -> _CommandSemantic:
    try:
        mode = command.mode
        only = command.only
        llm = command.llm
        include_disabled = command.include_disabled
        allow_model_downloads = command.allow_model_downloads
    except AttributeError:
        raise SelfCheckJobError("SELF_CHECK_PLAN_INVALID") from None
    if (
        not isinstance(mode, SelfCheckMode)
        or not isinstance(only, tuple)
        or any(not isinstance(item, str) for item in only)
        or any(
            type(item) is not bool
            for item in (llm, include_disabled, allow_model_downloads)
        )
    ):
        raise SelfCheckJobError("SELF_CHECK_PLAN_INVALID")
    return _CommandSemantic(
        mode=mode,
        only=only,
        llm=llm,
        include_disabled=include_disabled,
        allow_model_downloads=allow_model_downloads,
    )


def _session_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,256}", value
    ):
        raise SelfCheckPlanError("SELF_CHECK_CONFIRMATION_INVALID")
    return value


def _validated_base_environment(values: Mapping[str, str]) -> dict[str, str]:
    try:
        environment = dict(values)
    except Exception:
        raise ValueError("invalid base child environment") from None
    if set(environment) & set(REQUIRED_SELF_CHECK_ENV_NAMES):
        raise ValueError("base child environment cannot define config variables")
    if any(
        not isinstance(name, str)
        or not name
        or "=" in name
        or "\x00" in name
        or not isinstance(value, str)
        or "\x00" in value
        for name, value in environment.items()
    ):
        raise ValueError("invalid base child environment")
    return environment


def _child_environment(
    inputs: SelfCheckEnvironmentInputs,
    *,
    base_environment: Mapping[str, str],
) -> dict[str, str]:
    environment = dict(base_environment)
    environment.update(
        {
            name: inputs.environment_snapshot.get(name) or ""
            for name in REQUIRED_SELF_CHECK_ENV_NAMES
        }
    )
    secret_values = {
        SECRETS_ENV_MAP["openai_api_key"]: inputs.secrets.openai_api_key or "",
        SECRETS_ENV_MAP["judge_api_key"]: inputs.secrets.judge_api_key or "",
        SECRETS_ENV_MAP["bilibili_cookie"]: inputs.secrets.bilibili_cookie or "",
        SECRETS_ENV_MAP["qbittorrent_password"]: (
            inputs.secrets.qbittorrent_password or ""
        ),
    }
    environment.update(secret_values)
    environment.update(dict(inputs.legacy_secret_canaries))
    return environment


def create_production_self_check_service(
    *,
    repo_root: Path,
    environment_inputs: Callable[[], SelfCheckEnvironmentInputs],
    platform_capabilities: PlatformCapabilities,
    runner: SelfCheckRunner | None = None,
    base_child_environment: Mapping[str, str] | None = None,
) -> SelfCheckService | None:
    """Compose an injected platform runner without starting any subprocess."""

    selected_platform = platform_capabilities
    if (
        not isinstance(selected_platform, PlatformCapabilities)
        or not selected_platform.self_check_containment
    ):
        return None
    try:
        root = Path(repo_root).resolve()
        SelfCheckPlanBuilder(
            script_path=root / "scripts" / "self_check.py",
            verify_script_file=True,
        ).build()
        if runner is None or base_child_environment is None:
            return None
        manager = SelfCheckJobManager(
            runner=runner,
            hard_timeout_s=900.0,
            max_terminal_jobs=20,
        )
        return SelfCheckService(
            script_path=root / "scripts" / "self_check.py",
            job_manager=manager,
            environment_inputs=environment_inputs,
            base_child_environment=base_child_environment,
        )
    except Exception:  # noqa: BLE001 -- optional capability fails closed
        return None

__all__ = [
    "SelfCheckAcknowledgements",
    "SelfCheckEnvironmentInputs",
    "SelfCheckService",
    "create_production_self_check_service",
]
