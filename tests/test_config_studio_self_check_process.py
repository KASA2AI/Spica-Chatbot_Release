from __future__ import annotations

import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from spica.adapters.config_studio.platform import platform_capabilities_for
from spica.adapters.config_studio.self_check_process import SubprocessSelfCheckRunner
from spica.config_studio.self_check import SelfCheckProcessOutcome


_LINUX_CAPABILITIES = platform_capabilities_for(
    os_family="posix",
    runtime_name="linux",
    user_id=1000,
    temp_directory="/synthetic-tmp",
)


class _BinaryStream:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = iter(chunks)
        self.drained = threading.Event()

    def read(self, _size: int) -> bytes:
        try:
            return next(self._chunks)
        except StopIteration:
            self.drained.set()
            return b""


class _FakePopen:
    def __init__(
        self,
        *,
        stdout_chunks: tuple[bytes, ...] = (b'{"ok": true}',),
        stderr_chunks: tuple[bytes, ...] = (b"progress\n",),
    ) -> None:
        self.pid = 4102
        self.returncode: int | None = None
        self.stdout = _BinaryStream(stdout_chunks)
        self.stderr = _BinaryStream(stderr_chunks)

    def wait(self, timeout: float | None = None) -> int:
        if not self.stdout.drained.wait(timeout):
            raise subprocess.TimeoutExpired(("fake",), timeout)
        if not self.stderr.drained.wait(timeout):
            raise subprocess.TimeoutExpired(("fake",), timeout)
        self.returncode = 0
        return 0

    def poll(self) -> int | None:
        return self.returncode


class _RecordingPopenFactory:
    def __init__(self, process: _FakePopen) -> None:
        self.process = process
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> _FakePopen:
        self.calls.append((argv, kwargs))
        return self.process


class _TimedOutPopen(_FakePopen):
    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired(("fake",), timeout)


class _GonePosixProcessGroups:
    def __init__(self) -> None:
        self.probes: list[tuple[int, int]] = []

    def getpgid(self, pid: int) -> int:
        return pid

    def getpgrp(self) -> int:
        return 9001

    def killpg(self, pgid: int, signal_number: int) -> None:
        self.probes.append((pgid, signal_number))
        if signal_number == 0:
            raise ProcessLookupError


class _EscalatingPosixProcessGroups(_GonePosixProcessGroups):
    def __init__(self) -> None:
        super().__init__()
        self.killed = False

    def killpg(self, pgid: int, signal_number: int) -> None:
        self.probes.append((pgid, signal_number))
        if signal_number == signal.SIGKILL:
            self.killed = True
        if signal_number == 0 and self.killed:
            raise ProcessLookupError


class _AlivePosixProcessGroups(_GonePosixProcessGroups):
    def killpg(self, pgid: int, signal_number: int) -> None:
        self.probes.append((pgid, signal_number))


class _MismatchedPosixProcessGroups(_AlivePosixProcessGroups):
    def getpgid(self, pid: int) -> int:
        return pid + 1


def test_runner_launches_in_an_isolated_group_with_only_the_explicit_environment(
    tmp_path: Path,
) -> None:
    process = _FakePopen()
    popen = _RecordingPopenFactory(process)
    groups = _GonePosixProcessGroups()
    runner = SubprocessSelfCheckRunner(
        repo_root=tmp_path,
        popen_factory=popen,
        platform_capabilities=_LINUX_CAPABILITIES,
        posix_process_groups=groups,
    )
    argv = ("/synthetic/python", "/synthetic/self_check.py", "--json")

    handle = runner.start(argv, {"SPICA_SYNTHETIC": "only-this-value"})
    outcome = handle.wait(1.0)

    assert handle.containment_established is True
    assert outcome.returncode == 0
    assert outcome.stdout == '{"ok": true}'
    assert outcome.stderr == ""
    assert outcome.stderr_summary is not None
    assert outcome.stderr_summary.total_line_count == 1
    assert outcome.stderr_summary.unclassified_line_count == 1
    assert outcome.cleanup_confirmed is True
    assert popen.calls == [
        (
            list(argv),
            {
                "cwd": str(tmp_path.resolve()),
                "env": {"SPICA_SYNTHETIC": "only-this-value"},
                "shell": False,
                "start_new_session": True,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": False,
            },
        )
    ]


def test_cancel_escalates_from_term_to_kill_and_confirms_the_group_is_gone(
    tmp_path: Path,
) -> None:
    process = _FakePopen(stdout_chunks=(), stderr_chunks=())
    popen = _RecordingPopenFactory(process)
    groups = _EscalatingPosixProcessGroups()
    runner = SubprocessSelfCheckRunner(
        repo_root=tmp_path,
        popen_factory=popen,
        platform_capabilities=_LINUX_CAPABILITIES,
        posix_process_groups=groups,
        terminate_grace_s=0.0,
        kill_grace_s=0.0,
    )
    handle = runner.start(("python", "self_check.py", "--json"), {"SAFE": "1"})

    assert handle.cancel() is True
    assert groups.probes == [
        (process.pid, signal.SIGTERM),
        (process.pid, 0),
        (process.pid, signal.SIGKILL),
        (process.pid, 0),
    ]


def test_output_budgets_are_independent_and_excess_bytes_are_still_drained(
    tmp_path: Path,
) -> None:
    process = _FakePopen(
        stdout_chunks=(b"abc", b"def", b"stdout-tail"),
        stderr_chunks=(b"12", b"345", b"stderr-tail"),
    )
    runner = SubprocessSelfCheckRunner(
        repo_root=tmp_path,
        popen_factory=_RecordingPopenFactory(process),
        platform_capabilities=_LINUX_CAPABILITIES,
        posix_process_groups=_GonePosixProcessGroups(),
        stdout_budget_bytes=5,
        stderr_budget_bytes=3,
    )

    outcome = runner.start(("python", "self_check.py", "--json"), {}).wait(1.0)

    assert outcome.stdout == "abcde"
    assert outcome.stderr == ""
    assert outcome.stdout_truncated is True
    assert outcome.stderr_truncated is True
    assert outcome.stderr_summary is not None
    assert outcome.stderr_summary.truncated is True
    assert process.stdout.drained.is_set()
    assert process.stderr.drained.is_set()


def test_stderr_is_streamed_into_bounded_metadata_without_retaining_raw_text(
    tmp_path: Path,
) -> None:
    process = _FakePopen(
        stdout_chunks=(b"{}",),
        stderr_chunks=(
            b"[self-check] running ocr (timeout 240s)...\n",
            b"synthetic diagnostic that must not be retained\n",
        ),
    )
    runner = SubprocessSelfCheckRunner(
        repo_root=tmp_path,
        popen_factory=_RecordingPopenFactory(process),
        platform_capabilities=_LINUX_CAPABILITIES,
        posix_process_groups=_GonePosixProcessGroups(),
    )

    handle = runner.start(("python", "self_check.py", "--json"), {})
    deadline = time.monotonic() + 1.0
    while not handle.stderr_snapshot().progress_names:
        if time.monotonic() >= deadline:
            raise AssertionError("stderr progress was not parsed while the job ran")
        time.sleep(0.001)
    outcome = handle.wait(1.0)

    assert outcome.stderr == ""
    assert "synthetic diagnostic" not in repr(outcome)
    assert outcome.stderr_summary is not None
    assert outcome.stderr_summary.progress_names == ("ocr",)
    assert outcome.stderr_summary.unclassified_line_count == 1
    assert outcome.stderr_summary.total_line_count == 2


def test_process_outcome_repr_never_contains_raw_stdout_or_stderr() -> None:
    stdout_canary = "synthetic-raw-stdout-secret"
    stderr_canary = "synthetic-raw-stderr-secret"
    outcome = SelfCheckProcessOutcome(
        returncode=0,
        stdout=stdout_canary,
        stderr=stderr_canary,
        cleanup_confirmed=True,
    )

    rendered = repr(outcome)

    assert stdout_canary not in rendered
    assert stderr_canary not in rendered


def test_exact_rc3_precondition_is_reduced_to_a_boolean_in_the_adapter(
    tmp_path: Path,
) -> None:
    exact = (
        "[self-check] FATAL: 检测到 Spica(qt_overlay) 正在运行。--full 会真加载模型并与"
        "应用争 GPU/显存——请先关闭应用，或用 --force 强行继续。\n"
    ).encode("utf-8")
    process = _FakePopen(stdout_chunks=(), stderr_chunks=(exact[:37], exact[37:]))
    runner = SubprocessSelfCheckRunner(
        repo_root=tmp_path,
        popen_factory=_RecordingPopenFactory(process),
        platform_capabilities=_LINUX_CAPABILITIES,
        posix_process_groups=_GonePosixProcessGroups(),
    )

    outcome = runner.start(("python", "self_check.py", "--json"), {}).wait(1.0)

    assert outcome.stderr == ""
    assert "--force" not in repr(outcome)
    assert outcome.stderr_summary is not None
    assert outcome.stderr_summary.exact_spica_running_precondition is True


def test_stdout_is_decoded_as_strict_utf8_without_replacement_characters(
    tmp_path: Path,
) -> None:
    process = _FakePopen(
        stdout_chunks=(b'{"detail":"\xff"}',),
        stderr_chunks=(),
    )
    runner = SubprocessSelfCheckRunner(
        repo_root=tmp_path,
        popen_factory=_RecordingPopenFactory(process),
        platform_capabilities=_LINUX_CAPABILITIES,
        posix_process_groups=_GonePosixProcessGroups(),
    )

    outcome = runner.start(("python", "self_check.py", "--json"), {}).wait(1.0)

    assert outcome.stdout == ""
    assert outcome.stdout_utf8_valid is False
    assert "�" not in outcome.stdout


def test_wait_raises_builtin_timeout_without_claiming_a_terminal_outcome(
    tmp_path: Path,
) -> None:
    runner = SubprocessSelfCheckRunner(
        repo_root=tmp_path,
        popen_factory=_RecordingPopenFactory(_TimedOutPopen()),
        platform_capabilities=_LINUX_CAPABILITIES,
        posix_process_groups=_GonePosixProcessGroups(),
    )
    handle = runner.start(("python", "self_check.py", "--json"), {})

    with pytest.raises(TimeoutError) as caught:
        handle.wait(0.01)

    assert str(caught.value) == "SELF_CHECK_PROCESS_TIMEOUT"


def test_normal_exit_reports_unconfirmed_cleanup_while_the_group_is_alive(
    tmp_path: Path,
) -> None:
    runner = SubprocessSelfCheckRunner(
        repo_root=tmp_path,
        popen_factory=_RecordingPopenFactory(_FakePopen()),
        platform_capabilities=_LINUX_CAPABILITIES,
        posix_process_groups=_AlivePosixProcessGroups(),
    )

    outcome = runner.start(("python", "self_check.py", "--json"), {}).wait(1.0)

    assert outcome.cleanup_confirmed is False


def test_cancel_never_claims_success_when_the_group_cannot_be_proven_gone(
    tmp_path: Path,
) -> None:
    process = _FakePopen(stdout_chunks=(), stderr_chunks=())
    groups = _AlivePosixProcessGroups()
    runner = SubprocessSelfCheckRunner(
        repo_root=tmp_path,
        popen_factory=_RecordingPopenFactory(process),
        platform_capabilities=_LINUX_CAPABILITIES,
        posix_process_groups=groups,
        terminate_grace_s=0.0,
        kill_grace_s=0.0,
    )

    handle = runner.start(("python", "self_check.py", "--json"), {})

    assert handle.cancel() is False
    assert groups.probes == [
        (process.pid, signal.SIGTERM),
        (process.pid, 0),
        (process.pid, signal.SIGKILL),
        (process.pid, 0),
    ]


def test_containment_requires_proof_that_the_child_is_its_group_leader(
    tmp_path: Path,
) -> None:
    runner = SubprocessSelfCheckRunner(
        repo_root=tmp_path,
        popen_factory=_RecordingPopenFactory(
            _FakePopen(stdout_chunks=(), stderr_chunks=())
        ),
        platform_capabilities=_LINUX_CAPABILITIES,
        posix_process_groups=_MismatchedPosixProcessGroups(),
    )

    handle = runner.start(("python", "self_check.py", "--json"), {})

    assert handle.containment_established is False
    assert handle.cancel() is False


def test_windows_fails_closed_without_starting_an_uncontained_process(
    tmp_path: Path,
) -> None:
    process = _FakePopen()
    popen = _RecordingPopenFactory(process)
    runner = SubprocessSelfCheckRunner(
        repo_root=tmp_path,
        popen_factory=popen,
        platform_capabilities=platform_capabilities_for(
            os_family="nt",
            runtime_name="win32",
            user_id=None,
            temp_directory=tmp_path,
        ),
    )

    handle = runner.start(("python", "self_check.py", "--json"), {})

    assert handle.containment_established is False
    assert handle.cancel() is True
    assert popen.calls == []
