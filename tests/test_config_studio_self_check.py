from __future__ import annotations

import ast
import sys
import time
import json
from pathlib import Path
from threading import Event, Thread
from typing import Mapping

import pytest

from spica.config.env_roster import (
    APP_ENV_MAP,
    LEGACY_ENV_VARS,
    LEGACY_SECRET_ENV_VARS,
    RESPEAKER_ENV_MAP,
    RUNTIME_CACHE_ENV_MAP,
    SCREEN_ENV_MAP,
    SECRETS_ENV_MAP,
    consumed_env_names,
)
from spica.config_studio.self_check import (
    SELF_CHECK_PROGRESS_TIMEOUTS,
    SPICA_RUNNING_PRECONDITION_STDERR,
    SelfCheckConsent,
    SelfCheckMode,
    SelfCheckJobManager,
    SelfCheckJobError,
    SelfCheckJobSnapshot,
    SelfCheckJobStatus,
    SelfCheckPlanError,
    SelfCheckPlan,
    SelfCheckPlanBuilder,
    SelfCheckProcessOutcome,
    SelfCheckStderrSummary,
)


@pytest.fixture(autouse=True)
def clear_real_config_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No inherited real config or secret reaches Config Studio tests."""
    for name in consumed_env_names() | frozenset(LEGACY_ENV_VARS):
        monkeypatch.delenv(name, raising=False)


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


def synthetic_child_environment(**overrides: str) -> dict[str, str]:
    environment = {name: "" for name in REQUIRED_SELF_CHECK_ENV_NAMES}
    environment.update(
        {
            "HOME": "/synthetic/home",
            "TMPDIR": "/synthetic/tmp",
            "PATH": "/synthetic/bin",
            "LANG": "C.UTF-8",
        }
    )
    environment.update(overrides)
    return environment


class FakeProcess:
    def __init__(
        self,
        outcome: SelfCheckProcessOutcome,
        *,
        containment_established: bool = True,
        release: Event | None = None,
        cancel_confirmed: bool = True,
        stderr_summary: SelfCheckStderrSummary = SelfCheckStderrSummary(),
    ) -> None:
        self.containment_established = containment_established
        self._outcome = outcome
        self._release = release
        self._cancel_confirmed = cancel_confirmed
        self._stderr_summary = stderr_summary
        self.cancel_calls = 0
        self.wait_calls = 0

    def wait(self, timeout_s: float) -> SelfCheckProcessOutcome:
        self.wait_calls += 1
        if self._release is not None and not self._release.wait(timeout_s):
            raise TimeoutError
        return self._outcome

    def cancel(self) -> bool:
        self.cancel_calls += 1
        if self._release is not None:
            self._release.set()
        return self._cancel_confirmed

    def stderr_snapshot(self) -> SelfCheckStderrSummary:
        return self._stderr_summary


class FakeRunner:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.argv: tuple[str, ...] | None = None
        self.environment: dict[str, str] | None = None

    def start(
        self, argv: tuple[str, ...], environment: Mapping[str, str]
    ) -> FakeProcess:
        self.argv = argv
        self.environment = dict(environment)
        return self.process


class BlockingRunner(FakeRunner):
    def __init__(self, process: FakeProcess) -> None:
        super().__init__(process)
        self.start_entered = Event()
        self.release_start = Event()

    def start(
        self, argv: tuple[str, ...], environment: Mapping[str, str]
    ) -> FakeProcess:
        self.start_entered.set()
        if not self.release_start.wait(1.0):
            raise TimeoutError
        return super().start(argv, environment)


def wait_for_terminal(
    manager: SelfCheckJobManager, job_id: str, timeout_s: float = 1.0
):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snapshot = manager.get(job_id)
        if snapshot.status in {
            SelfCheckJobStatus.PASS,
            SelfCheckJobStatus.UNVERIFIED,
            SelfCheckJobStatus.DEGRADED,
            SelfCheckJobStatus.FAIL,
            SelfCheckJobStatus.CANCELLED,
            SelfCheckJobStatus.INTERNAL_ERROR,
        }:
            return snapshot
        time.sleep(0.005)
    raise AssertionError("self-check job did not finish")


def test_light_plan_uses_only_the_fixed_json_entrypoint(tmp_path: Path) -> None:
    script = tmp_path / "scripts" / "self_check.py"

    plan = SelfCheckPlanBuilder(script_path=script).build()

    assert plan.mode is SelfCheckMode.LIGHT
    assert plan.argv == (sys.executable, str(script.absolute()), "--json")
    assert plan.checks == (
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


def test_full_plan_emits_only_reviewed_flags_in_stable_order(tmp_path: Path) -> None:
    script = tmp_path / "scripts" / "self_check.py"
    builder = SelfCheckPlanBuilder(script_path=script)

    plan = builder.build(
        mode=SelfCheckMode.FULL,
        only=("ocr", "llm"),
        llm=True,
        include_disabled=True,
        allow_model_downloads=True,
        consents=frozenset(SelfCheckConsent),
    )

    assert plan.argv == (
        sys.executable,
        str(script.absolute()),
        "--json",
        "--full",
        "--only",
        "ocr,llm",
        "--llm",
        "--all",
        "--allow-model-downloads",
    )
    assert plan.checks == ("ocr", "llm")


def test_plan_can_only_be_created_by_the_allowlisting_builder(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        SelfCheckPlan()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        SelfCheckPlan(  # type: ignore[call-arg]
            mode=SelfCheckMode.LIGHT,
            argv=(sys.executable, str(tmp_path / "evil.py"), "--json"),
            checks=("config",),
        )


def test_manager_revalidates_the_complete_fixed_argv_grammar(tmp_path: Path) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build()
    object.__setattr__(plan, "argv", (*plan.argv, "--force"))
    runner = FakeRunner(
        FakeProcess(SelfCheckProcessOutcome(0, "{}", "", cleanup_confirmed=True))
    )
    manager = SelfCheckJobManager(runner=runner)

    with pytest.raises(SelfCheckJobError) as caught:
        manager.start(plan, synthetic_child_environment())

    assert caught.value.code == "SELF_CHECK_PLAN_INVALID"
    assert runner.argv is None


def test_production_plan_builder_can_require_a_regular_non_symlink_script(
    tmp_path: Path,
) -> None:
    script = tmp_path / "scripts" / "self_check.py"
    script.parent.mkdir()
    script.write_text("# synthetic, never executed\n", encoding="utf-8")

    plan = SelfCheckPlanBuilder(
        script_path=script,
        verify_script_file=True,
    ).build()

    assert plan.argv[1] == str(script.absolute())

    replacement = tmp_path / "replacement.py"
    replacement.write_text("# replacement\n", encoding="utf-8")
    script.unlink()
    script.symlink_to(replacement)
    manager = SelfCheckJobManager(
        runner=FakeRunner(
            FakeProcess(SelfCheckProcessOutcome(0, "{}", "", True))
        )
    )
    with pytest.raises(SelfCheckJobError) as tampered:
        manager.start(plan, synthetic_child_environment())
    assert tampered.value.code == "SELF_CHECK_PLAN_INVALID"

    link = tmp_path / "self_check-link.py"
    link.symlink_to(script)
    with pytest.raises(SelfCheckPlanError) as caught:
        SelfCheckPlanBuilder(
            script_path=link,
            verify_script_file=True,
        ).build()
    assert caught.value.code == "SELF_CHECK_SCRIPT_UNSAFE"


def test_self_check_cli_contract_drift_is_reviewed_statically() -> None:
    """Pin the existing script without importing it or starting a process."""
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "self_check.py"
    tree = ast.parse(script_path.read_text(encoding="utf-8"), filename=str(script_path))
    assignments: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                try:
                    assignments[target.id] = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    pass
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            try:
                assignments[node.target.id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                pass

    expected_checks = (
        "tts",
        "stt",
        "moondream",
        "ocr",
        "song_uvr",
        "song_rvc",
        "llm",
    )
    expected_timeouts = {
        "tts": 300.0,
        "stt": 240.0,
        "moondream": 300.0,
        "ocr": 240.0,
        "song_uvr": 300.0,
        "song_rvc": 480.0,
        "llm": 60.0,
    }
    string_constants = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert assignments["HEAVY_CHECKS"] == expected_checks
    assert assignments["DEFAULT_TIMEOUTS_S"] == expected_timeouts
    assert dict(SELF_CHECK_PROGRESS_TIMEOUTS) == {
        name: f"{timeout:.0f}" for name, timeout in expected_timeouts.items()
    }
    assert SPICA_RUNNING_PRECONDITION_STDERR.rstrip("\n") in string_constants


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"mode": SelfCheckMode.FULL}, "FULL_CONFIRMATION_REQUIRED"),
        (
            {
                "mode": SelfCheckMode.FULL,
                "only": ("not_reviewed",),
                "consents": frozenset({SelfCheckConsent.FULL}),
            },
            "CHECK_NOT_ALLOWLISTED",
        ),
        (
            {
                "mode": SelfCheckMode.FULL,
                "only": ("ocr", "ocr"),
                "consents": frozenset({SelfCheckConsent.FULL}),
            },
            "DUPLICATE_CHECK",
        ),
        (
            {
                "mode": SelfCheckMode.FULL,
                "only": ("ocr",),
                "llm": True,
                "consents": frozenset({SelfCheckConsent.FULL, SelfCheckConsent.LLM}),
            },
            "LLM_NOT_SELECTED",
        ),
        (
            {
                "mode": SelfCheckMode.FULL,
                "llm": True,
                "consents": frozenset({SelfCheckConsent.FULL}),
            },
            "LLM_CONFIRMATION_REQUIRED",
        ),
        (
            {
                "mode": SelfCheckMode.FULL,
                "include_disabled": True,
                "consents": frozenset({SelfCheckConsent.FULL}),
            },
            "INCLUDE_DISABLED_CONFIRMATION_REQUIRED",
        ),
        (
            {
                "mode": SelfCheckMode.FULL,
                "allow_model_downloads": True,
                "consents": frozenset({SelfCheckConsent.FULL}),
            },
            "MODEL_DOWNLOAD_CONFIRMATION_REQUIRED",
        ),
        ({"only": ("ocr",)}, "FULL_MODE_REQUIRED"),
        ({"mode": "full"}, "INVALID_MODE"),
    ],
)
def test_plan_builder_rejects_unreviewed_or_unconfirmed_work(
    tmp_path: Path, kwargs: dict[str, object], code: str
) -> None:
    builder = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py")

    with pytest.raises(SelfCheckPlanError) as caught:
        builder.build(**kwargs)  # type: ignore[arg-type]

    assert caught.value.code == code


def test_job_runs_with_explicit_environment_and_returns_only_protocol_dtos(
    tmp_path: Path,
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build(
        mode=SelfCheckMode.FULL,
        only=("ocr",),
        consents=frozenset({SelfCheckConsent.FULL}),
    )
    process = FakeProcess(
        SelfCheckProcessOutcome(
            returncode=0,
            stdout='{"mode":"full","results":[{"name":"ocr","status":"PASS",'
            '"detail":{"device":"cuda"}}],"exit_code":0}',
            stderr="[self-check] running ocr (timeout 240s)...\nignored diagnostic\n",
            cleanup_confirmed=True,
        )
    )
    runner = FakeRunner(process)
    manager = SelfCheckJobManager(runner=runner, id_factory=lambda: "opaque_job_1")
    environment = synthetic_child_environment(OPENAI_API_KEY="synthetic-only")
    expected_environment = environment.copy()

    started = manager.start(plan, environment)
    environment["MODEL"] = "mutated-after-start"
    finished = wait_for_terminal(manager, started.job_id)

    assert started.status is SelfCheckJobStatus.QUEUED
    assert runner.argv == plan.argv
    assert runner.environment == expected_environment
    assert finished.status is SelfCheckJobStatus.PASS
    assert finished.results[0].name == "ocr"
    assert finished.results[0].detail == {"device": "cuda"}
    assert finished.progress[0].name == "ocr"
    assert finished.progress[0].status == "RUNNING"
    assert finished.stderr_line_count == 1
    assert not hasattr(finished, "stderr")


def test_running_job_exposes_only_streamed_stderr_metadata(tmp_path: Path) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build(
        mode=SelfCheckMode.FULL,
        only=("ocr",),
        consents=frozenset({SelfCheckConsent.FULL}),
    )
    release = Event()
    process = FakeProcess(
        SelfCheckProcessOutcome(0, full_ocr_payload(), "", True),
        release=release,
        stderr_summary=SelfCheckStderrSummary(
            progress_names=("ocr",),
            unclassified_line_count=3,
            total_line_count=4,
            truncated=True,
        ),
    )
    manager = SelfCheckJobManager(
        runner=FakeRunner(process), id_factory=lambda: "streamed_progress"
    )

    started = manager.start(plan, synthetic_child_environment())
    running = manager.get(started.job_id)

    assert running.status is SelfCheckJobStatus.RUNNING
    assert tuple(item.name for item in running.progress) == ("ocr",)
    assert running.stderr_line_count == 3
    assert running.stderr_total_line_count == 4
    assert running.stderr_truncated is True
    assert not hasattr(running, "stderr")
    manager.cancel(started.job_id)


def test_only_one_job_runs_and_confirmed_cancellation_wins_over_process_rc(
    tmp_path: Path,
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build()
    release = Event()
    process = FakeProcess(
        SelfCheckProcessOutcome(
            returncode=0,
            stdout="{}",
            stderr="",
            cleanup_confirmed=True,
        ),
        release=release,
    )
    manager = SelfCheckJobManager(
        runner=FakeRunner(process),
        id_factory=iter(("job_a", "job_b")).__next__,
    )

    active = manager.start(plan, synthetic_child_environment())
    with pytest.raises(SelfCheckJobError) as caught:
        manager.start(plan, synthetic_child_environment())
    cancelled = manager.cancel(active.job_id)
    final = wait_for_terminal(manager, active.job_id)

    assert caught.value.code == "SELF_CHECK_BUSY"
    assert cancelled.status is SelfCheckJobStatus.CANCELLED
    assert final.status is SelfCheckJobStatus.CANCELLED
    assert process.cancel_calls == 1


def test_queued_job_cancels_before_its_process_handle_is_available(
    tmp_path: Path,
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build()
    process = FakeProcess(
        SelfCheckProcessOutcome(0, "{}", "", cleanup_confirmed=True)
    )
    runner = BlockingRunner(process)
    manager = SelfCheckJobManager(
        runner=runner,
        id_factory=lambda: "queued_cancel",
    )
    start_result: list[object] = []
    start_thread = Thread(
        target=lambda: start_result.append(
            manager.start(plan, synthetic_child_environment())
        )
    )

    start_thread.start()
    assert runner.start_entered.wait(1.0)
    try:
        cancelled = manager.cancel("queued_cancel")
    finally:
        runner.release_start.set()
        start_thread.join(1.0)

    assert not start_thread.is_alive()
    assert len(start_result) == 1
    assert cancelled.status is SelfCheckJobStatus.CANCELLED
    assert manager.get("queued_cancel").status is SelfCheckJobStatus.CANCELLED
    assert process.cancel_calls == 1
    assert process.wait_calls == 0


@pytest.mark.parametrize("established", [False, "false"])
def test_job_is_refused_when_top_level_containment_cannot_be_established(
    tmp_path: Path, established: object
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build()
    process = FakeProcess(
        SelfCheckProcessOutcome(0, "{}", "", cleanup_confirmed=True),
        containment_established=established,  # type: ignore[arg-type]
    )
    manager = SelfCheckJobManager(
        runner=FakeRunner(process), id_factory=lambda: "uncontained_job"
    )

    refused = manager.start(plan, synthetic_child_environment())

    assert refused.status is SelfCheckJobStatus.INTERNAL_ERROR
    assert refused.error_code == "PROCESS_CONTAINMENT_UNAVAILABLE"
    assert process.cancel_calls == 1
    assert process.wait_calls == 0


def test_string_false_cancel_result_never_claims_cancelled(tmp_path: Path) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build()
    process = FakeProcess(
        SelfCheckProcessOutcome(0, "", "", cleanup_confirmed=True),
        release=Event(),
        cancel_confirmed="false",  # type: ignore[arg-type]
    )
    manager = SelfCheckJobManager(
        runner=FakeRunner(process), id_factory=lambda: "strict_cancel_bool"
    )

    active = manager.start(plan, synthetic_child_environment())
    cancelled = manager.cancel(active.job_id)

    assert cancelled.status is SelfCheckJobStatus.INTERNAL_ERROR
    assert cancelled.error_code == "CONTAINMENT_CANCEL_UNCONFIRMED"


def full_ocr_payload(
    *,
    mode: str = "full",
    status: str = "PASS",
    exit_code: int = 0,
    results: list[dict[str, object]] | None = None,
    extra: dict[str, object] | None = None,
) -> str:
    payload: dict[str, object] = {
        "mode": mode,
        "results": results
        if results is not None
        else [{"name": "ocr", "status": status, "detail": {}}],
        "exit_code": exit_code,
    }
    payload.update(extra or {})
    return json.dumps(payload)


@pytest.mark.parametrize(
    ("stdout", "returncode"),
    [
        ("noise\n" + full_ocr_payload(), 0),
        (full_ocr_payload() + "\n" + full_ocr_payload(), 0),
        (full_ocr_payload(mode="light"), 0),
        (
            full_ocr_payload(
                results=[
                    {"name": "ocr", "status": "PASS", "detail": {}},
                    {"name": "ocr", "status": "PASS", "detail": {}},
                ]
            ),
            0,
        ),
        (
            full_ocr_payload(
                results=[{"name": "tts", "status": "PASS", "detail": {}}]
            ),
            0,
        ),
        (full_ocr_payload(status="UNKNOWN"), 0),
        (full_ocr_payload(status="DEGRADED", exit_code=0), 0),
        (full_ocr_payload(exit_code=1), 0),
        (full_ocr_payload(), 1),
        (full_ocr_payload(extra={"unreviewed": True}), 0),
    ],
)
def test_job_rejects_json_that_does_not_exactly_match_the_plan_and_exit_contract(
    tmp_path: Path, stdout: str, returncode: int
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build(
        mode=SelfCheckMode.FULL,
        only=("ocr",),
        consents=frozenset({SelfCheckConsent.FULL}),
    )
    process = FakeProcess(
        SelfCheckProcessOutcome(returncode, stdout, "", cleanup_confirmed=True)
    )
    manager = SelfCheckJobManager(
        runner=FakeRunner(process), id_factory=lambda: "invalid_protocol"
    )

    started = manager.start(plan, synthetic_child_environment())
    finished = wait_for_terminal(manager, started.job_id)

    assert finished.status is SelfCheckJobStatus.INTERNAL_ERROR
    assert finished.error_code == "INVALID_SELF_CHECK_OUTPUT"
    assert finished.results == ()


def test_job_rejects_stdout_that_was_not_strict_utf8(tmp_path: Path) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build(
        mode=SelfCheckMode.FULL,
        only=("ocr",),
        consents=frozenset({SelfCheckConsent.FULL}),
    )
    process = FakeProcess(
        SelfCheckProcessOutcome(
            0,
            "",
            "",
            cleanup_confirmed=True,
            stdout_utf8_valid=False,
        )
    )
    manager = SelfCheckJobManager(
        runner=FakeRunner(process), id_factory=lambda: "invalid_utf8"
    )

    started = manager.start(plan, synthetic_child_environment())
    finished = wait_for_terminal(manager, started.job_id)

    assert finished.status is SelfCheckJobStatus.INTERNAL_ERROR
    assert finished.error_code == "INVALID_SELF_CHECK_OUTPUT"


def test_job_redacts_secret_canaries_in_values_keys_and_escaped_forms(
    tmp_path: Path,
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build(
        mode=SelfCheckMode.FULL,
        only=("ocr",),
        consents=frozenset({SelfCheckConsent.FULL}),
    )
    multiline = 'line one\n"quoted"\\slash雪'
    common_word = "PASS"
    json_key = "secret-as-json-key"
    payload = full_ocr_payload(
        results=[
            {
                "name": "ocr",
                "status": "PASS",
                "reason": f"failure included {multiline}",
                "detail": {
                    json_key: multiline,
                    "escaped": multiline.encode("unicode_escape").decode("ascii"),
                    "common": common_word,
                },
            }
        ]
    )
    process = FakeProcess(SelfCheckProcessOutcome(0, payload, "", True))
    manager = SelfCheckJobManager(
        runner=FakeRunner(process), id_factory=lambda: "redacted_job"
    )

    started = manager.start(
        plan,
        synthetic_child_environment(
            OPENAI_API_KEY=multiline,
            JUDGE_API_KEY=common_word,
            BILIBILI_COOKIE="short",
            QBITTORRENT_PASSWORD=json_key,
        ),
    )
    finished = wait_for_terminal(manager, started.job_id)
    rendered = repr(finished)

    assert finished.status is SelfCheckJobStatus.PASS
    assert finished.results[0].status == "PASS"
    assert multiline not in rendered
    assert multiline.encode("unicode_escape").decode("ascii") not in rendered
    assert json_key not in rendered
    assert "«REDACTED:OPENAI_API_KEY»" in rendered
    assert "«REDACTED:QBITTORRENT_PASSWORD»" in rendered
    assert finished.results[0].detail["common"] == "«REDACTED:JUDGE_API_KEY»"


def test_job_redacts_canonical_numeric_and_boolean_secret_data(
    tmp_path: Path,
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build(
        mode=SelfCheckMode.FULL,
        only=("ocr",),
        consents=frozenset({SelfCheckConsent.FULL}),
    )
    payload = full_ocr_payload(
        results=[
            {
                "name": "ocr",
                "status": "PASS",
                "detail": {"numeric": 123, "boolean": False},
            }
        ]
    )
    manager = SelfCheckJobManager(
        runner=FakeRunner(
            FakeProcess(SelfCheckProcessOutcome(0, payload, "", True))
        ),
        id_factory=lambda: "scalar_redaction",
    )

    started = manager.start(
        plan,
        synthetic_child_environment(
            OPENAI_API_KEY="123",
            JUDGE_API_KEY="false",
        ),
    )
    finished = wait_for_terminal(manager, started.job_id)

    assert finished.status is SelfCheckJobStatus.PASS
    assert finished.results[0].detail == {
        "numeric": "«REDACTED:OPENAI_API_KEY»",
        "boolean": "«REDACTED:JUDGE_API_KEY»",
    }


def test_job_redacts_retired_legacy_secret_canaries(tmp_path: Path) -> None:
    assert LEGACY_SECRET_ENV_VARS == ("DEEPSEEK_API_KEY",)
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build(
        mode=SelfCheckMode.FULL,
        only=("ocr",),
        consents=frozenset({SelfCheckConsent.FULL}),
    )
    canary = "retired-secret-canary"
    payload = full_ocr_payload(
        results=[
            {
                "name": "ocr",
                "status": "PASS",
                "detail": {"legacy": canary},
            }
        ]
    )
    manager = SelfCheckJobManager(
        runner=FakeRunner(
            FakeProcess(SelfCheckProcessOutcome(0, payload, "", True))
        ),
        id_factory=lambda: "legacy_redaction",
    )

    started = manager.start(
        plan,
        synthetic_child_environment(DEEPSEEK_API_KEY=canary),
    )
    finished = wait_for_terminal(manager, started.job_id)

    encoded = repr(finished.results)
    assert canary not in encoded
    assert "REDACTED:DEEPSEEK_API_KEY" in encoded


@pytest.mark.parametrize(
    ("result", "manager_kwargs", "outcome_kwargs", "code"),
    [
        (
            {"name": "ocr", "status": "PASS", "detail": {"text": "x" * 80}},
            {"stdout_budget_bytes": 64},
            {},
            "SELF_CHECK_OUTPUT_LIMIT_EXCEEDED",
        ),
        (
            {"name": "ocr", "status": "PASS", "detail": {"text": "x" * 33}},
            {"max_string_chars": 32},
            {},
            "INVALID_SELF_CHECK_OUTPUT",
        ),
        (
            {"name": "ocr", "status": "PASS", "detail": {"items": [1, 2, 3]}},
            {"max_collection_items": 2},
            {},
            "INVALID_SELF_CHECK_OUTPUT",
        ),
        (
            {
                "name": "ocr",
                "status": "PASS",
                "detail": {"a": {"b": {"c": "too deep"}}},
            },
            {"max_value_depth": 2},
            {},
            "INVALID_SELF_CHECK_OUTPUT",
        ),
        (
            {"name": "ocr", "status": "PASS", "detail": {}},
            {},
            {"stdout_truncated": True},
            "SELF_CHECK_OUTPUT_TRUNCATED",
        ),
    ],
)
def test_job_enforces_output_and_nested_value_budgets(
    tmp_path: Path,
    result: dict[str, object],
    manager_kwargs: dict[str, int],
    outcome_kwargs: dict[str, bool],
    code: str,
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build(
        mode=SelfCheckMode.FULL,
        only=("ocr",),
        consents=frozenset({SelfCheckConsent.FULL}),
    )
    process = FakeProcess(
        SelfCheckProcessOutcome(
            0,
            full_ocr_payload(results=[result]),
            "",
            cleanup_confirmed=True,
            **outcome_kwargs,
        )
    )
    manager = SelfCheckJobManager(
        runner=FakeRunner(process),
        id_factory=lambda: "bounded_output",
        **manager_kwargs,
    )

    started = manager.start(plan, synthetic_child_environment())
    finished = wait_for_terminal(manager, started.job_id)

    assert finished.status is SelfCheckJobStatus.INTERNAL_ERROR
    assert finished.error_code == code


def test_stderr_exposes_only_strict_planned_progress_and_bounded_metadata(
    tmp_path: Path,
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build(
        mode=SelfCheckMode.FULL,
        only=("ocr",),
        consents=frozenset({SelfCheckConsent.FULL}),
    )
    stderr = "\n".join(
        (
            "[self-check] running ocr (timeout 240s)...",
            " [self-check] running ocr (timeout 240s)...",
            "[self-check] running ocr (timeout 240s)...suffix",
            "[self-check] running ocr (timeout 999s)...",
            "[self-check] running tts (timeout 300s)...",
            "[self-check] running arbitrary (timeout 1s)...",
            "diagnostic contained a synthetic secret",
        )
    )
    process = FakeProcess(
        SelfCheckProcessOutcome(
            0,
            full_ocr_payload(),
            stderr,
            cleanup_confirmed=True,
            stderr_truncated=True,
        )
    )
    manager = SelfCheckJobManager(
        runner=FakeRunner(process), id_factory=lambda: "strict_stderr"
    )

    started = manager.start(
        plan, synthetic_child_environment(OPENAI_API_KEY="synthetic secret")
    )
    finished = wait_for_terminal(manager, started.job_id)

    assert [(item.name, item.status) for item in finished.progress] == [
        ("ocr", "RUNNING")
    ]
    assert finished.stderr_line_count == 6
    assert finished.stderr_truncated is True
    assert "synthetic secret" not in repr(finished)


def test_exact_current_rc3_guard_maps_to_one_job_precondition_without_raw_text(
    tmp_path: Path,
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build(
        mode=SelfCheckMode.FULL,
        only=("ocr",),
        consents=frozenset({SelfCheckConsent.FULL}),
    )
    process = FakeProcess(
        SelfCheckProcessOutcome(
            3,
            "{}",
            "",
            cleanup_confirmed=True,
            stderr_summary=SelfCheckStderrSummary(
                unclassified_line_count=1,
                total_line_count=1,
                exact_spica_running_precondition=True,
            ),
        )
    )
    manager = SelfCheckJobManager(
        runner=FakeRunner(process), id_factory=lambda: "spica_precondition"
    )

    started = manager.start(plan, synthetic_child_environment())
    finished = wait_for_terminal(manager, started.job_id)

    assert finished.status is SelfCheckJobStatus.INTERNAL_ERROR
    assert finished.error_code == "PRECONDITION_SPICA_RUNNING"
    assert finished.stderr_line_count == 1
    assert "--force" not in repr(finished)


def test_rc3_exact_guard_does_not_override_a_valid_final_document(
    tmp_path: Path,
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build(
        mode=SelfCheckMode.FULL,
        only=("ocr",),
        consents=frozenset({SelfCheckConsent.FULL}),
    )
    exact_stderr = (
        "[self-check] FATAL: 检测到 Spica(qt_overlay) 正在运行。--full 会真加载模型并与"
        "应用争 GPU/显存——请先关闭应用，或用 --force 强行继续。\n"
    )
    process = FakeProcess(
        SelfCheckProcessOutcome(
            3,
            full_ocr_payload(),
            exact_stderr,
            cleanup_confirmed=True,
        )
    )
    manager = SelfCheckJobManager(
        runner=FakeRunner(process), id_factory=lambda: "rc3_valid_document"
    )

    started = manager.start(plan, synthetic_child_environment())
    finished = wait_for_terminal(manager, started.job_id)

    assert finished.error_code == "INVALID_SELF_CHECK_OUTPUT"


def test_rc3_guard_text_drift_falls_back_to_internal_error(tmp_path: Path) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build(
        mode=SelfCheckMode.FULL,
        only=("ocr",),
        consents=frozenset({SelfCheckConsent.FULL}),
    )
    process = FakeProcess(
        SelfCheckProcessOutcome(
            3,
            "",
            "[self-check] FATAL: Spica seems to be running.\n",
            cleanup_confirmed=True,
        )
    )
    manager = SelfCheckJobManager(
        runner=FakeRunner(process), id_factory=lambda: "drifted_precondition"
    )

    started = manager.start(plan, synthetic_child_environment())
    finished = wait_for_terminal(manager, started.job_id)

    assert finished.status is SelfCheckJobStatus.INTERNAL_ERROR
    assert finished.error_code == "INVALID_SELF_CHECK_OUTPUT"


@pytest.mark.parametrize(
    ("cancel_confirmed", "expected_code"),
    [
        (True, "SELF_CHECK_TIMEOUT"),
        (False, "CONTAINMENT_CANCEL_UNCONFIRMED"),
    ],
)
def test_server_timeout_cancels_the_contained_tree_before_finishing(
    tmp_path: Path, cancel_confirmed: bool, expected_code: str
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build()
    process = FakeProcess(
        SelfCheckProcessOutcome(0, "", "", cleanup_confirmed=True),
        release=Event(),
        cancel_confirmed=cancel_confirmed,
    )
    manager = SelfCheckJobManager(
        runner=FakeRunner(process),
        hard_timeout_s=0.001,
        id_factory=lambda: "timed_out_job",
    )

    started = manager.start(plan, synthetic_child_environment())
    finished = wait_for_terminal(manager, started.job_id)

    assert finished.status is SelfCheckJobStatus.INTERNAL_ERROR
    assert finished.error_code == expected_code
    assert process.cancel_calls == 1


def test_manager_retains_only_the_twenty_most_recent_terminal_jobs(
    tmp_path: Path,
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build(
        mode=SelfCheckMode.FULL,
        only=("ocr",),
        consents=frozenset({SelfCheckConsent.FULL}),
    )
    process = FakeProcess(
        SelfCheckProcessOutcome(0, full_ocr_payload(), "", cleanup_confirmed=True)
    )
    job_ids = iter(f"job_{index}" for index in range(21))
    manager = SelfCheckJobManager(
        runner=FakeRunner(process), id_factory=job_ids.__next__
    )

    for _ in range(21):
        started = manager.start(plan, synthetic_child_environment())
        wait_for_terminal(manager, started.job_id)

    with pytest.raises(SelfCheckJobError) as caught:
        manager.get("job_0")

    assert caught.value.code == "SELF_CHECK_JOB_NOT_FOUND"
    assert manager.get("job_1").status is SelfCheckJobStatus.PASS
    assert manager.get("job_20").status is SelfCheckJobStatus.PASS


def test_job_duration_uses_the_injected_monotonic_clock(tmp_path: Path) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build()
    release = Event()
    process = FakeProcess(
        SelfCheckProcessOutcome(0, "", "", cleanup_confirmed=True), release=release
    )
    now = [100.0]
    manager = SelfCheckJobManager(
        runner=FakeRunner(process),
        clock=lambda: now[0],
        id_factory=lambda: "monotonic_job",
    )

    started = manager.start(plan, synthetic_child_environment())
    now[0] = 112.5
    cancelled = manager.cancel(started.job_id)

    assert cancelled.status is SelfCheckJobStatus.CANCELLED
    assert cancelled.duration_s == 12.5


@pytest.mark.parametrize("field", ["llm", "include_disabled", "allow_model_downloads"])
def test_plan_flags_require_native_booleans(tmp_path: Path, field: str) -> None:
    builder = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py")
    kwargs: dict[str, object] = {
        "mode": SelfCheckMode.FULL,
        "consents": frozenset(SelfCheckConsent),
        field: "false",
    }

    with pytest.raises(SelfCheckPlanError) as caught:
        builder.build(**kwargs)  # type: ignore[arg-type]

    assert caught.value.code == "NATIVE_BOOLEAN_REQUIRED"


def test_job_requires_an_explicit_string_environment_mapping(tmp_path: Path) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build()
    process = FakeProcess(SelfCheckProcessOutcome(0, "", "", True))
    runner = FakeRunner(process)
    manager = SelfCheckJobManager(
        runner=runner, id_factory=lambda: "invalid_environment"
    )

    missing_roster_name = synthetic_child_environment()
    missing_roster_name.pop("MODEL")
    invalid_value = synthetic_child_environment()
    invalid_value["OPENAI_API_KEY"] = None  # type: ignore[assignment]

    for environment in (missing_roster_name, invalid_value):
        with pytest.raises(SelfCheckJobError) as caught:
            manager.start(plan, environment)
        assert caught.value.code == "INVALID_CHILD_ENVIRONMENT"
    assert runner.argv is None


@pytest.mark.parametrize(
    "required_name",
    [
        next(iter(RESPEAKER_ENV_MAP.values())),
        next(iter(RUNTIME_CACHE_ENV_MAP.values())),
    ],
)
def test_job_environment_must_mask_non_app_roster_names_too(
    tmp_path: Path,
    required_name: str,
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build()
    runner = FakeRunner(FakeProcess(SelfCheckProcessOutcome(0, "", "", True)))
    manager = SelfCheckJobManager(runner=runner)
    environment = synthetic_child_environment()
    environment.pop(required_name)

    with pytest.raises(SelfCheckJobError) as caught:
        manager.start(plan, environment)

    assert caught.value.code == "INVALID_CHILD_ENVIRONMENT"
    assert runner.environment is None


def test_unknown_job_ids_return_the_same_stable_error_for_get_and_cancel(
    tmp_path: Path,
) -> None:
    process = FakeProcess(SelfCheckProcessOutcome(0, "", "", True))
    manager = SelfCheckJobManager(
        runner=FakeRunner(process), id_factory=lambda: "unused_job"
    )

    for operation in (manager.get, manager.cancel):
        with pytest.raises(SelfCheckJobError) as caught:
            operation("missing_or_sensitive_input")
        assert caught.value.code == "SELF_CHECK_JOB_NOT_FOUND"
        assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    ("result_status", "exit_code", "job_status"),
    [
        ("UNVERIFIED", 0, SelfCheckJobStatus.UNVERIFIED),
        ("SKIPPED_DISABLED", 0, SelfCheckJobStatus.UNVERIFIED),
        ("DEGRADED", 1, SelfCheckJobStatus.DEGRADED),
        ("FAIL", 2, SelfCheckJobStatus.FAIL),
    ],
)
def test_result_statuses_map_to_stable_job_terminal_states(
    tmp_path: Path,
    result_status: str,
    exit_code: int,
    job_status: SelfCheckJobStatus,
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build(
        mode=SelfCheckMode.FULL,
        only=("ocr",),
        consents=frozenset({SelfCheckConsent.FULL}),
    )
    process = FakeProcess(
        SelfCheckProcessOutcome(
            exit_code,
            full_ocr_payload(status=result_status, exit_code=exit_code),
            "",
            cleanup_confirmed=True,
        )
    )
    manager = SelfCheckJobManager(
        runner=FakeRunner(process), id_factory=lambda: f"status_{result_status}"
    )

    started = manager.start(plan, synthetic_child_environment())
    finished = wait_for_terminal(manager, started.job_id)

    assert finished.status is job_status


@pytest.mark.parametrize(
    "outcome",
    [
        SelfCheckProcessOutcome(
            False, full_ocr_payload(), "", cleanup_confirmed=True  # type: ignore[arg-type]
        ),
        SelfCheckProcessOutcome(
            0,
            full_ocr_payload(),
            None,  # type: ignore[arg-type]
            cleanup_confirmed=True,
        ),
        SelfCheckProcessOutcome(
            0,
            full_ocr_payload(),
            "",
            cleanup_confirmed=True,
            stderr_truncated="false",  # type: ignore[arg-type]
        ),
    ],
)
def test_malformed_runner_outcomes_fail_closed_instead_of_leaving_running_jobs(
    tmp_path: Path, outcome: SelfCheckProcessOutcome
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build(
        mode=SelfCheckMode.FULL,
        only=("ocr",),
        consents=frozenset({SelfCheckConsent.FULL}),
    )
    manager = SelfCheckJobManager(
        runner=FakeRunner(FakeProcess(outcome)),
        id_factory=lambda: "malformed_outcome",
    )

    started = manager.start(plan, synthetic_child_environment())
    finished = wait_for_terminal(manager, started.job_id)

    assert finished.status is SelfCheckJobStatus.INTERNAL_ERROR
    assert finished.error_code == "INVALID_PROCESS_OUTCOME"


def test_malformed_process_outcome_is_cancelled_before_it_is_classified(
    tmp_path: Path,
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build(
        mode=SelfCheckMode.FULL,
        only=("ocr",),
        consents=frozenset({SelfCheckConsent.FULL}),
    )
    process = FakeProcess(
        SelfCheckProcessOutcome(
            False,  # type: ignore[arg-type]
            full_ocr_payload(),
            "",
            cleanup_confirmed=True,
        )
    )
    manager = SelfCheckJobManager(
        runner=FakeRunner(process), id_factory=lambda: "cancel_invalid_outcome"
    )

    started = manager.start(plan, synthetic_child_environment())
    finished = wait_for_terminal(manager, started.job_id)

    assert finished.error_code == "INVALID_PROCESS_OUTCOME"
    assert process.cancel_calls == 1


def test_unconfirmed_containment_cleanup_latches_the_manager_unsafe(
    tmp_path: Path,
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build()
    process = FakeProcess(
        SelfCheckProcessOutcome(0, "{}", "", cleanup_confirmed=True),
        containment_established=False,
        cancel_confirmed=False,
    )
    runner = FakeRunner(process)
    manager = SelfCheckJobManager(
        runner=runner, id_factory=lambda: "unconfirmed_containment"
    )

    refused = manager.start(plan, synthetic_child_environment())

    assert refused.error_code == "CONTAINMENT_CLEANUP_UNCONFIRMED"
    with pytest.raises(SelfCheckJobError) as caught:
        manager.start(plan, synthetic_child_environment())
    assert caught.value.code == "SELF_CHECK_MANAGER_UNSAFE"
    assert process.cancel_calls == 1


@pytest.mark.parametrize(
    ("cancel_confirmed", "expected_status", "expected_error"),
    [
        (True, SelfCheckJobStatus.PASS, None),
        (
            False,
            SelfCheckJobStatus.INTERNAL_ERROR,
            "CONTAINMENT_CLEANUP_UNCONFIRMED",
        ),
    ],
)
def test_normal_exit_uses_the_same_confirmed_containment_cleanup_path(
    tmp_path: Path,
    cancel_confirmed: bool,
    expected_status: SelfCheckJobStatus,
    expected_error: str | None,
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build(
        mode=SelfCheckMode.FULL,
        only=("ocr",),
        consents=frozenset({SelfCheckConsent.FULL}),
    )
    process = FakeProcess(
        SelfCheckProcessOutcome(
            0,
            full_ocr_payload(),
            "",
            cleanup_confirmed=False,
        ),
        cancel_confirmed=cancel_confirmed,
    )
    manager = SelfCheckJobManager(
        runner=FakeRunner(process), id_factory=lambda: "normal_cleanup"
    )

    started = manager.start(plan, synthetic_child_environment())
    finished = wait_for_terminal(manager, started.job_id)

    assert finished.status is expected_status
    assert finished.error_code == expected_error
    assert process.cancel_calls == 1
    if not cancel_confirmed:
        with pytest.raises(SelfCheckJobError) as caught:
            manager.start(plan, synthetic_child_environment())
        assert caught.value.code == "SELF_CHECK_MANAGER_UNSAFE"


def test_list_returns_the_active_job_then_recent_terminal_jobs(
    tmp_path: Path,
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build()
    process = FakeProcess(
        SelfCheckProcessOutcome(0, "{}", "", cleanup_confirmed=True),
        release=Event(),
    )
    manager = SelfCheckJobManager(
        runner=FakeRunner(process), id_factory=lambda: "listed_active"
    )

    active = manager.start(plan, synthetic_child_environment())

    listed_active = manager.list()
    assert tuple(item.job_id for item in listed_active) == (active.job_id,)
    assert listed_active[0].status is SelfCheckJobStatus.RUNNING
    cancelled = manager.cancel(active.job_id)
    assert manager.list() == (cancelled,)


def test_shutdown_is_idempotent_and_uses_the_confirmed_cancel_path(
    tmp_path: Path,
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build()
    process = FakeProcess(
        SelfCheckProcessOutcome(0, "{}", "", cleanup_confirmed=True),
        release=Event(),
    )
    manager = SelfCheckJobManager(
        runner=FakeRunner(process), id_factory=lambda: "shutdown_active"
    )
    active = manager.start(plan, synthetic_child_environment())

    first = manager.shutdown()
    second = manager.shutdown()

    assert first == second
    assert first[0].job_id == active.job_id
    assert first[0].status is SelfCheckJobStatus.CANCELLED
    assert process.cancel_calls == 1
    with pytest.raises(SelfCheckJobError) as caught:
        manager.start(plan, synthetic_child_environment())
    assert caught.value.code == "SELF_CHECK_MANAGER_SHUTDOWN"


def test_shutdown_waits_for_a_queued_launch_and_cleans_its_late_process(
    tmp_path: Path,
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build()
    process = FakeProcess(
        SelfCheckProcessOutcome(0, "{}", "", cleanup_confirmed=True)
    )
    runner = BlockingRunner(process)
    manager = SelfCheckJobManager(
        runner=runner,
        shutdown_launch_timeout_s=0.5,
        id_factory=lambda: "shutdown_queued_launch",
    )
    start_thread = Thread(
        target=lambda: manager.start(plan, synthetic_child_environment())
    )
    shutdown_result: list[SelfCheckJobSnapshot] = []
    shutdown_thread = Thread(
        target=lambda: shutdown_result.extend(manager.shutdown())
    )

    start_thread.start()
    assert runner.start_entered.wait(1.0)
    shutdown_thread.start()
    assert not runner.release_start.is_set()
    assert shutdown_thread.is_alive()

    runner.release_start.set()
    start_thread.join(1.0)
    shutdown_thread.join(1.0)

    assert not start_thread.is_alive()
    assert not shutdown_thread.is_alive()
    assert len(shutdown_result) == 1
    finished = shutdown_result[0]
    assert finished.status is SelfCheckJobStatus.CANCELLED
    assert process.cancel_calls == 1
    assert process.wait_calls == 0


def test_shutdown_reports_internal_error_when_queued_launch_does_not_resolve(
    tmp_path: Path,
) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build()
    process = FakeProcess(
        SelfCheckProcessOutcome(0, "{}", "", cleanup_confirmed=True)
    )
    runner = BlockingRunner(process)
    manager = SelfCheckJobManager(
        runner=runner,
        shutdown_launch_timeout_s=0.01,
        id_factory=lambda: "shutdown_stuck_launch",
    )
    start_thread = Thread(
        target=lambda: manager.start(plan, synthetic_child_environment())
    )

    start_thread.start()
    assert runner.start_entered.wait(1.0)
    started_at = time.monotonic()
    shutdown_result = manager.shutdown()
    shutdown_duration = time.monotonic() - started_at

    assert shutdown_duration < 0.5
    assert len(shutdown_result) == 1
    assert shutdown_result[0].status is SelfCheckJobStatus.INTERNAL_ERROR
    assert shutdown_result[0].error_code == "PROCESS_START_SHUTDOWN_TIMEOUT"

    runner.release_start.set()
    start_thread.join(1.0)

    assert not start_thread.is_alive()
    assert process.cancel_calls == 1
    assert manager.get("shutdown_stuck_launch").status is (
        SelfCheckJobStatus.INTERNAL_ERROR
    )


def test_redaction_expansion_cannot_exceed_the_api_output_budget(tmp_path: Path) -> None:
    plan = SelfCheckPlanBuilder(script_path=tmp_path / "self_check.py").build(
        mode=SelfCheckMode.FULL,
        only=("ocr",),
        consents=frozenset({SelfCheckConsent.FULL}),
    )
    payload = full_ocr_payload(
        results=[
            {
                "name": "ocr",
                "status": "PASS",
                "detail": {"repeated": "x" * 32},
            }
        ]
    )
    manager = SelfCheckJobManager(
        runner=FakeRunner(
            FakeProcess(SelfCheckProcessOutcome(0, payload, "", True))
        ),
        max_string_chars=64,
        id_factory=lambda: "redaction_expansion",
    )

    started = manager.start(
        plan, synthetic_child_environment(OPENAI_API_KEY="x")
    )
    finished = wait_for_terminal(manager, started.job_id)

    assert finished.status is SelfCheckJobStatus.INTERNAL_ERROR
    assert finished.error_code == "SELF_CHECK_REDACTED_OUTPUT_LIMIT_EXCEEDED"
