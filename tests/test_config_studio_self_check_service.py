from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import pytest

from spica.config.environment_snapshot import EnvironmentSnapshot
from spica.adapters.config_studio.platform import platform_capabilities_for
from spica.config.secrets import LoadedSecrets, Secrets, load_secrets
from spica.config_studio.self_check import (
    LIGHT_CHECKS,
    REQUIRED_SELF_CHECK_ENV_NAMES,
    SelfCheckJobManager,
    SelfCheckJobError,
    SelfCheckJobStatus,
    SelfCheckMode,
    SelfCheckPlanError,
    SelfCheckProcessOutcome,
    SelfCheckStderrSummary,
)
from spica.config_studio.self_check_service import (
    SelfCheckAcknowledgements,
    SelfCheckEnvironmentInputs,
    SelfCheckService,
    create_production_self_check_service,
)


@dataclass(frozen=True)
class _Command:
    mode: SelfCheckMode = SelfCheckMode.LIGHT
    only: tuple[str, ...] = ()
    llm: bool = False
    include_disabled: bool = False
    allow_model_downloads: bool = False


class _FinishedProcess:
    containment_established = True

    def __init__(self, outcome: SelfCheckProcessOutcome) -> None:
        self._outcome = outcome

    def wait(self, timeout_s: float) -> SelfCheckProcessOutcome:
        return self._outcome

    def cancel(self) -> bool:
        return True

    def stderr_snapshot(self) -> SelfCheckStderrSummary:
        return SelfCheckStderrSummary()


class _RecordingRunner:
    def __init__(self) -> None:
        self.environments: list[dict[str, str]] = []
        self.argv: list[tuple[str, ...]] = []

    def start(
        self, argv: tuple[str, ...], environment: Mapping[str, str]
    ) -> _FinishedProcess:
        self.argv.append(argv)
        self.environments.append(dict(environment))
        results = [
            {
                "name": name,
                "status": "PASS",
                "detail": {},
                "reason": "",
                "duration_s": 0.0,
            }
            for name in LIGHT_CHECKS
        ]
        return _FinishedProcess(
            SelfCheckProcessOutcome(
                returncode=0,
                stdout=json.dumps(
                    {"mode": "light", "results": results, "exit_code": 0}
                ),
                stderr="",
                cleanup_confirmed=True,
            )
        )


class _UnavailableProcess:
    containment_established = False

    def wait(self, timeout_s: float) -> SelfCheckProcessOutcome:
        raise AssertionError("an uncontained process must never be waited")

    def cancel(self) -> bool:
        return True

    def stderr_snapshot(self) -> SelfCheckStderrSummary:
        return SelfCheckStderrSummary()


class _UnavailableRunner:
    def __init__(self) -> None:
        self.environment: dict[str, str] | None = None

    def start(
        self, argv: tuple[str, ...], environment: Mapping[str, str]
    ) -> _UnavailableProcess:
        self.environment = dict(environment)
        return _UnavailableProcess()


def _wait_for_terminal(service: SelfCheckService, job_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        job = service.get(job_id)
        if job["status"] in {
            SelfCheckJobStatus.PASS.value,
            SelfCheckJobStatus.INTERNAL_ERROR.value,
        }:
            return job
        time.sleep(0.005)
    raise AssertionError("self-check service job did not finish")


def test_light_job_uses_latest_explicit_snapshot_and_separately_held_secrets(
    tmp_path: Path,
) -> None:
    script = tmp_path / "scripts" / "self_check.py"
    script.parent.mkdir()
    script.write_text("# synthetic owner script\n", encoding="utf-8")
    runner = _RecordingRunner()
    inputs = [
        SelfCheckEnvironmentInputs(
            environment_snapshot=EnvironmentSnapshot.from_mapping(
                {"MODEL": "first-model"}, layer="synthetic"
            ),
            secrets=Secrets(openai_api_key="first-secret"),
            legacy_secret_canaries=(("DEEPSEEK_API_KEY", "first-legacy"),),
        )
    ]
    service = SelfCheckService(
        script_path=script,
        job_manager=SelfCheckJobManager(runner=runner),
        environment_inputs=lambda: inputs[-1],
        base_child_environment={
            "HOME": "/synthetic/home",
            "LANG": "C.UTF-8",
        },
    )

    first = service.start(_Command())
    _wait_for_terminal(service, str(first["job_id"]))
    inputs.append(
        SelfCheckEnvironmentInputs(
            environment_snapshot=EnvironmentSnapshot.from_mapping(
                {"MODEL": "second-model"}, layer="synthetic"
            ),
            secrets=Secrets(openai_api_key="second-secret"),
        )
    )
    second = service.start(_Command())
    _wait_for_terminal(service, str(second["job_id"]))

    assert REQUIRED_SELF_CHECK_ENV_NAMES.issubset(runner.environments[0])
    assert runner.environments[0]["MODEL"] == "first-model"
    assert runner.environments[0]["OPENAI_API_KEY"] == "first-secret"
    assert runner.environments[0]["DEEPSEEK_API_KEY"] == "first-legacy"
    assert runner.environments[1]["MODEL"] == "second-model"
    assert runner.environments[1]["OPENAI_API_KEY"] == "second-secret"
    assert runner.environments[1]["DEEPSEEK_API_KEY"] == ""
    assert runner.environments[1]["HOME"] == "/synthetic/home"
    assert set(runner.environments[1]) == (
        set(REQUIRED_SELF_CHECK_ENV_NAMES) | {"HOME", "LANG"}
    )


def test_terminal_dto_redacts_shadowed_and_duplicate_owner_secret_material(
    tmp_path: Path,
) -> None:
    script = tmp_path / "scripts" / "self_check.py"
    script.parent.mkdir()
    script.write_text("# synthetic owner script; never executed\n", encoding="utf-8")
    repo_env = tmp_path / "repo" / "xiaosan.env"
    parent_env = tmp_path / "parent" / "xiaosan.env"
    repo_env.parent.mkdir()
    parent_env.parent.mkdir()
    shadowed = "shadowed-owner-secret-canary"
    duplicate_first = "duplicate-first-secret-canary"
    duplicate_winner = "duplicate-winner-secret-canary"
    repo_env.write_text(
        f"OPENAI_API_KEY={shadowed}\n"
        f"JUDGE_API_KEY={duplicate_first}\n"
        f"JUDGE_API_KEY={duplicate_winner}\n",
        encoding="utf-8",
    )
    parent_env.write_bytes(b"")
    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={"OPENAI_API_KEY": "winning-inherited-secret"},
        repo_env_path=repo_env,
        parent_env_path=parent_env,
        prime_process=False,
    )
    assert isinstance(loaded, LoadedSecrets)
    assert loaded.contains_secret_material(shadowed) is True
    assert loaded.contains_secret_material(duplicate_first) is True

    results = [
        {
            "name": name,
            "status": "PASS",
            "detail": (
                {
                    "ordinary_value": shadowed,
                    duplicate_first: "secret-as-json-key",
                }
                if name == "config"
                else {}
            ),
            "reason": duplicate_first if name == "config" else "",
            "duration_s": 0.0,
        }
        for name in LIGHT_CHECKS
    ]
    outcome = SelfCheckProcessOutcome(
        returncode=0,
        stdout=json.dumps(
            {"mode": "light", "results": results, "exit_code": 0}
        ),
        stderr="",
        cleanup_confirmed=True,
    )

    class PayloadRunner:
        def start(
            self,
            argv: tuple[str, ...],
            environment: Mapping[str, str],
        ) -> _FinishedProcess:
            del argv, environment
            return _FinishedProcess(outcome)

    service = SelfCheckService(
        script_path=script,
        job_manager=SelfCheckJobManager(runner=PayloadRunner()),
        environment_inputs=lambda: SelfCheckEnvironmentInputs(
            environment_snapshot=loaded.environment_snapshot,
            secrets=loaded.secrets,
            legacy_secret_canaries=loaded.legacy_secret_canaries,
            secret_material_sanitizer=loaded.sanitize_secret_material,
        ),
    )

    started = service.start(_Command())
    terminal = _wait_for_terminal(service, str(started["job_id"]))
    encoded = json.dumps(terminal, ensure_ascii=False)

    assert terminal["status"] == "PASS"
    assert shadowed not in encoded
    assert duplicate_first not in encoded
    assert "«REDACTED:OPENAI_API_KEY»" in encoded
    assert "«REDACTED:JUDGE_API_KEY»" in encoded

    from fastapi.testclient import TestClient

    from spica.config_studio.api import create_config_studio_app
    from spica.config_studio.security import SecurityContext
    from spica.config_studio.services import ReadOnlyConfigStudioServices

    app_path = tmp_path / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_bytes(b"{}\n")
    platform = platform_capabilities_for(
        os_family="posix",
        runtime_name="linux",
        user_id=script.stat().st_uid,
        temp_directory=tmp_path / "platform-tmp",
    )
    services = ReadOnlyConfigStudioServices(
        repo_root=tmp_path,
        environment_snapshot=loaded.environment_snapshot,
        background_health_code=None,
        platform_capabilities=platform,
        secrets=loaded.secrets,
        environment_owner=lambda: loaded,
        self_check_service=service,
    )
    bootstrap_token = "synthetic-bootstrap-token-000000000000"
    token_values = iter(
        (
            "synthetic-session-token-000000000000",
            "synthetic-csrf-token-000000000000",
        )
    )
    app = create_config_studio_app(
        services,
        SecurityContext(
            host="127.0.0.1",
            port=8765,
            bootstrap_token=bootstrap_token,
            token_factory=lambda: next(token_values),
        ),
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": bootstrap_token,
            },
        ).raise_for_status()
        response = client.get("/api/v1/self-check/jobs")

    response.raise_for_status()
    assert shadowed not in response.text
    assert duplicate_first not in response.text
    assert "«REDACTED:OPENAI_API_KEY»" in response.text
    assert "«REDACTED:JUDGE_API_KEY»" in response.text


def test_heavy_job_requires_a_server_receipt_bound_to_session_and_plan(
    tmp_path: Path,
) -> None:
    script = tmp_path / "scripts" / "self_check.py"
    script.parent.mkdir()
    script.write_text("# synthetic owner script\n", encoding="utf-8")
    runner = _RecordingRunner()
    service = SelfCheckService(
        script_path=script,
        job_manager=SelfCheckJobManager(runner=runner),
        environment_inputs=lambda: SelfCheckEnvironmentInputs(
            environment_snapshot=EnvironmentSnapshot.from_mapping(
                {}, layer="synthetic"
            ),
            secrets=Secrets(),
        ),
        receipt_factory=lambda: "opaque-confirmation-1",
    )
    command = _Command(mode=SelfCheckMode.FULL, only=("tts",))

    confirmation = service.prepare_heavy(
        command,
        acknowledgements=SelfCheckAcknowledgements(full=True),
        session_id="session-a",
    )

    assert confirmation == {
        "confirmation_receipt": "opaque-confirmation-1",
        "expires_in_s": 120.0,
        "semantic": {
            "mode": "full",
            "checks": ["tts"],
            "llm": False,
            "include_disabled": False,
            "allow_model_downloads": False,
        },
    }
    assert "argv" not in confirmation
    with pytest.raises(SelfCheckPlanError) as missing:
        service.start(command, session_id="session-a")
    assert missing.value.code == "FULL_CONFIRMATION_REQUIRED"
    with pytest.raises(SelfCheckPlanError) as wrong_session:
        service.start(
            command,
            session_id="session-b",
            confirmation_receipt="opaque-confirmation-1",
        )
    assert wrong_session.value.code == "SELF_CHECK_CONFIRMATION_MISMATCH"

    service.start(
        command,
        session_id="session-a",
        confirmation_receipt="opaque-confirmation-1",
    )

    assert runner.argv == [
        (
            sys.executable,
            str(script.absolute()),
            "--json",
            "--full",
            "--only",
            "tts",
        )
    ]
    with pytest.raises(SelfCheckPlanError) as replayed:
        service.start(
            command,
            session_id="session-a",
            confirmation_receipt="opaque-confirmation-1",
        )
    assert replayed.value.code == "SELF_CHECK_CONFIRMATION_INVALID"


def test_include_disabled_requires_its_own_explicit_acknowledgement(
    tmp_path: Path,
) -> None:
    script = tmp_path / "self_check.py"
    script.write_text("# synthetic owner script\n", encoding="utf-8")
    service = SelfCheckService(
        script_path=script,
        job_manager=SelfCheckJobManager(runner=_RecordingRunner()),
        environment_inputs=lambda: SelfCheckEnvironmentInputs(
            environment_snapshot=EnvironmentSnapshot.from_mapping(
                {}, layer="synthetic"
            ),
            secrets=Secrets(),
        ),
    )
    command = _Command(
        mode=SelfCheckMode.FULL,
        only=("ocr",),
        include_disabled=True,
    )

    with pytest.raises(SelfCheckPlanError) as missing:
        service.prepare_heavy(
            command,
            acknowledgements=SelfCheckAcknowledgements(full=True),
            session_id="session-a",
        )

    assert missing.value.code == "INCLUDE_DISABLED_CONFIRMATION_REQUIRED"
    confirmation = service.prepare_heavy(
        command,
        acknowledgements=SelfCheckAcknowledgements(
            full=True,
            include_disabled=True,
        ),
        session_id="session-a",
    )
    assert confirmation["semantic"]["include_disabled"] is True


def test_production_factory_is_posix_only_and_latches_containment_failure(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "synthetic-repo"
    script = repo_root / "scripts" / "self_check.py"
    script.parent.mkdir(parents=True)
    script.write_text("# synthetic owner script\n", encoding="utf-8")
    inputs = lambda: SelfCheckEnvironmentInputs(
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {}, layer="synthetic"
        ),
        secrets=Secrets(),
    )
    synthetic_runtime = {
        "PATH": "/synthetic/bin",
        "HOME": str(tmp_path / "synthetic-home"),
        "TMPDIR": str(tmp_path / "synthetic-tmp"),
        "LANG": "C.UTF-8",
    }

    windows_runner = _UnavailableRunner()
    assert (
        create_production_self_check_service(
            repo_root=repo_root,
            environment_inputs=inputs,
            platform_capabilities=platform_capabilities_for(
                os_family="nt",
                runtime_name="win32",
                user_id=None,
                temp_directory=tmp_path,
            ),
            runner=windows_runner,
            base_child_environment=synthetic_runtime,
        )
        is None
    )
    assert windows_runner.environment is None
    linux_runner = _UnavailableRunner()
    service = create_production_self_check_service(
        repo_root=repo_root,
        environment_inputs=inputs,
        platform_capabilities=platform_capabilities_for(
            os_family="posix",
            runtime_name="linux",
            user_id=1000,
            temp_directory=tmp_path,
        ),
        runner=linux_runner,
        base_child_environment=synthetic_runtime,
    )
    assert service is not None
    assert service.available is True

    job = service.start(_Command())

    assert job["status"] == "INTERNAL_ERROR"
    assert job["error_code"] == "PROCESS_CONTAINMENT_UNAVAILABLE"
    assert linux_runner.environment is not None
    assert linux_runner.environment["PATH"] == "/synthetic/bin"
    assert linux_runner.environment["TMPDIR"] == str(tmp_path / "synthetic-tmp")
    assert linux_runner.environment["LANG"] == "C.UTF-8"
    assert Path(linux_runner.environment["HOME"]).is_absolute()
    assert service.available is False
    with pytest.raises(SelfCheckJobError) as disabled:
        service.start(_Command())
    assert getattr(disabled.value, "code", None) == "SELF_CHECK_MANAGER_UNSAFE"


def test_heavy_confirmation_expires_without_starting_a_process(tmp_path: Path) -> None:
    script = tmp_path / "self_check.py"
    script.write_text("# synthetic owner script\n", encoding="utf-8")
    runner = _RecordingRunner()
    now = [10.0]
    service = SelfCheckService(
        script_path=script,
        job_manager=SelfCheckJobManager(runner=runner),
        environment_inputs=lambda: SelfCheckEnvironmentInputs(
            environment_snapshot=EnvironmentSnapshot.from_mapping(
                {}, layer="synthetic"
            ),
            secrets=Secrets(),
        ),
        clock=lambda: now[-1],
        receipt_factory=lambda: "expiring-confirmation",
    )
    command = _Command(
        mode=SelfCheckMode.FULL,
        only=("llm",),
        llm=True,
        include_disabled=True,
        allow_model_downloads=True,
    )
    confirmation = service.prepare_heavy(
        command,
        acknowledgements=SelfCheckAcknowledgements(
            full=True,
            llm=True,
            include_disabled=True,
            model_downloads=True,
        ),
        session_id="session-a",
    )
    now.append(130.0)

    with pytest.raises(SelfCheckPlanError) as expired:
        service.start(
            command,
            session_id="session-a",
            confirmation_receipt=str(confirmation["confirmation_receipt"]),
        )

    assert expired.value.code == "SELF_CHECK_CONFIRMATION_EXPIRED"
    assert runner.argv == []


def test_environment_provider_failure_is_bounded_before_process_start(
    tmp_path: Path,
) -> None:
    script = tmp_path / "self_check.py"
    script.write_text("# synthetic owner script\n", encoding="utf-8")
    runner = _RecordingRunner()
    secret = "provider-exception-secret-canary"

    def fail_provider() -> SelfCheckEnvironmentInputs:
        raise RuntimeError(secret)

    service = SelfCheckService(
        script_path=script,
        job_manager=SelfCheckJobManager(runner=runner),
        environment_inputs=fail_provider,
    )

    with pytest.raises(SelfCheckJobError) as failed:
        service.start(_Command())

    assert failed.value.code == "INVALID_CHILD_ENVIRONMENT"
    assert secret not in repr(failed.value)
    assert runner.argv == []


def test_read_only_owner_exposes_only_the_injected_self_check_capability(
    tmp_path: Path,
) -> None:
    from spica.config_studio.services import ReadOnlyConfigStudioServices

    repo_root = tmp_path / "synthetic-repo"
    config = repo_root / "data" / "config" / "app.yaml"
    script = repo_root / "scripts" / "self_check.py"
    config.parent.mkdir(parents=True)
    script.parent.mkdir(parents=True)
    config.write_text("tts:\n  enabled: false\n", encoding="utf-8")
    script.write_text("# synthetic owner script\n", encoding="utf-8")
    runner = _RecordingRunner()
    self_check = SelfCheckService(
        script_path=script,
        job_manager=SelfCheckJobManager(runner=runner),
        environment_inputs=lambda: SelfCheckEnvironmentInputs(
            environment_snapshot=EnvironmentSnapshot.from_mapping(
                {}, layer="synthetic"
            ),
            secrets=Secrets(),
        ),
    )
    services = ReadOnlyConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {}, layer="synthetic"
        ),
        secrets=Secrets(),
        background_health_code=None,
        self_check_service=self_check,
        platform_capabilities=platform_capabilities_for(
            os_family="posix",
            runtime_name="linux",
            user_id=1000,
            temp_directory=tmp_path / "platform-tmp",
        ),
    )

    assert services.meta()["capabilities"] == {
        "app_config_write": False,
        "overlay_write": False,
        "sensitive_write": False,
        "rollback": False,
        "self_check": True,
        "self_check_jobs": True,
    }
    queued = services.start_self_check(_Command())
    terminal = _wait_for_terminal(self_check, str(queued["job_id"]))

    assert terminal["status"] == "PASS"
    assert services.list_self_checks()[0]["job_id"] == queued["job_id"]
    services.shutdown()
    with pytest.raises(SelfCheckJobError) as closed:
        services.start_self_check(_Command())
    assert closed.value.code == "SELF_CHECK_MANAGER_SHUTDOWN"
