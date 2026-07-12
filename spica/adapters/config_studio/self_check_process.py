"""POSIX subprocess adapter for the Config Studio self-check runner.

The caller supplies the complete child environment.  This module never merges
it with the Studio process environment.
"""

from __future__ import annotations

import math
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Mapping, Protocol

from spica.ports.config_studio_platform import PlatformCapabilities
from spica.config_studio.self_check import (
    SELF_CHECK_PROGRESS_TIMEOUTS,
    SPICA_RUNNING_PRECONDITION_STDERR,
    SelfCheckProcessOutcome,
    SelfCheckStderrSummary,
)


_READ_SIZE = 64 * 1024
_MAX_STDERR_LINE_BYTES = 8 * 1024
_PROGRESS_TIMEOUTS = dict(SELF_CHECK_PROGRESS_TIMEOUTS)
_PROGRESS_RE = re.compile(
    r"^\[self-check\] running "
    r"(tts|stt|moondream|ocr|song_uvr|song_rvc|llm) "
    r"\(timeout ([0-9]+)s\)\.\.\.$"
)
_SPICA_RUNNING_BYTES = SPICA_RUNNING_PRECONDITION_STDERR.encode("utf-8")


class _BinaryPipe(Protocol):
    def read(self, size: int) -> bytes: ...


class _PopenProcess(Protocol):
    pid: int
    returncode: int | None
    stdout: _BinaryPipe
    stderr: _BinaryPipe

    def wait(self, timeout: float | None = None) -> int: ...

    def poll(self) -> int | None: ...


class _PosixProcessGroups(Protocol):
    def getpgid(self, pid: int) -> int: ...

    def getpgrp(self) -> int: ...

    def killpg(self, pgid: int, signal_number: int) -> None: ...


class _BoundedPipeReader:
    def __init__(self, pipe: _BinaryPipe, budget_bytes: int) -> None:
        self._pipe = pipe
        self._budget_bytes = budget_bytes
        self._parts: list[bytes] = []
        self._stored_bytes = 0
        self._truncated = False
        self._error: Exception | None = None
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._drain, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def wait(self, timeout: float) -> bool:
        return self._done.wait(max(0.0, timeout))

    @property
    def text(self) -> str:
        if self._error is not None:
            raise RuntimeError("SELF_CHECK_PIPE_READ_FAILED") from None
        return b"".join(self._parts).decode("utf-8", errors="replace")

    def strict_text(self) -> tuple[str, bool]:
        if self._error is not None:
            raise RuntimeError("SELF_CHECK_PIPE_READ_FAILED") from None
        try:
            return b"".join(self._parts).decode("utf-8", errors="strict"), True
        except UnicodeDecodeError:
            return "", False

    @property
    def truncated(self) -> bool:
        return self._truncated

    def _drain(self) -> None:
        try:
            while True:
                chunk = self._pipe.read(_READ_SIZE)
                if not chunk:
                    break
                remaining = self._budget_bytes - self._stored_bytes
                if remaining > 0:
                    kept = chunk[:remaining]
                    self._parts.append(kept)
                    self._stored_bytes += len(kept)
                if len(chunk) > max(remaining, 0):
                    self._truncated = True
        except Exception as exc:  # noqa: BLE001 -- normalized at boundary
            self._error = exc
        finally:
            self._done.set()


class _StructuredStderrReader:
    """Continuously reduce stderr to bounded metadata, never retained text."""

    def __init__(self, pipe: _BinaryPipe, budget_bytes: int) -> None:
        self._pipe = pipe
        self._budget_bytes = budget_bytes
        self._byte_count = 0
        self._truncated = False
        self._progress_names: list[str] = []
        self._progress_seen: set[str] = set()
        self._unclassified_line_count = 0
        self._total_line_count = 0
        self._line = bytearray()
        self._line_nonempty = False
        self._line_overflow = False
        self._exact_possible = True
        self._exact_offset = 0
        self._exact_match = False
        self._error: Exception | None = None
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._drain, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def wait(self, timeout: float) -> bool:
        return self._done.wait(max(0.0, timeout))

    def snapshot(self) -> SelfCheckStderrSummary:
        if self._error is not None:
            raise RuntimeError("SELF_CHECK_PIPE_READ_FAILED") from None
        with self._lock:
            return SelfCheckStderrSummary(
                progress_names=tuple(self._progress_names),
                unclassified_line_count=self._unclassified_line_count,
                total_line_count=self._total_line_count,
                truncated=self._truncated,
                exact_spica_running_precondition=self._exact_match,
            )

    def _drain(self) -> None:
        try:
            while True:
                chunk = self._pipe.read(_READ_SIZE)
                if not chunk:
                    break
                with self._lock:
                    self._consume(chunk)
            with self._lock:
                if self._line_nonempty or self._line_overflow:
                    self._finish_line()
                self._exact_match = (
                    self._exact_possible
                    and self._exact_offset == len(_SPICA_RUNNING_BYTES)
                )
        except Exception as exc:  # noqa: BLE001 -- normalized at boundary
            self._error = exc
        finally:
            self._done.set()

    def _consume(self, chunk: bytes) -> None:
        previous_count = self._byte_count
        self._byte_count += len(chunk)
        if self._byte_count > self._budget_bytes:
            self._truncated = True
        if self._exact_possible:
            expected = _SPICA_RUNNING_BYTES[
                self._exact_offset : self._exact_offset + len(chunk)
            ]
            if chunk != expected or self._exact_offset + len(chunk) > len(
                _SPICA_RUNNING_BYTES
            ):
                self._exact_possible = False
            self._exact_offset += len(chunk)
        if previous_count > self._budget_bytes:
            self._truncated = True
        for byte in chunk:
            if byte == 10:  # newline
                self._finish_line()
                continue
            if byte not in (13,):
                self._line_nonempty = True
            if len(self._line) < _MAX_STDERR_LINE_BYTES:
                self._line.append(byte)
            else:
                self._line_overflow = True

    def _finish_line(self) -> None:
        if not self._line_nonempty and not self._line_overflow:
            self._line.clear()
            return
        self._total_line_count += 1
        classified = False
        if not self._line_overflow:
            try:
                text = bytes(self._line).rstrip(b"\r").decode("utf-8", "strict")
            except UnicodeDecodeError:
                text = ""
            matched = _PROGRESS_RE.fullmatch(text)
            if matched is not None:
                name, timeout = matched.groups()
                if (
                    _PROGRESS_TIMEOUTS.get(name) == timeout
                    and name not in self._progress_seen
                ):
                    self._progress_seen.add(name)
                    self._progress_names.append(name)
                    classified = True
        if not classified:
            self._unclassified_line_count += 1
        self._line.clear()
        self._line_nonempty = False
        self._line_overflow = False


class _PosixSelfCheckProcess:
    def __init__(
        self,
        process: _PopenProcess,
        *,
        groups: _PosixProcessGroups,
        stdout_budget_bytes: int,
        stderr_budget_bytes: int,
        terminate_grace_s: float,
        kill_grace_s: float,
    ) -> None:
        self._process = process
        self._groups = groups
        self._pgid: int | None = None
        try:
            pgid = groups.getpgid(process.pid)
            if pgid == process.pid and pgid != groups.getpgrp():
                self._pgid = pgid
        except OSError:
            self._pgid = None
        self.containment_established = self._pgid is not None
        self._stdout = _BoundedPipeReader(process.stdout, stdout_budget_bytes)
        self._stderr = _StructuredStderrReader(
            process.stderr, stderr_budget_bytes
        )
        self._stdout.start()
        self._stderr.start()
        self._terminate_grace_s = terminate_grace_s
        self._kill_grace_s = kill_grace_s
        self._cancel_lock = threading.Lock()
        self._confirmed_cancel = False

    def wait(self, timeout_s: float) -> SelfCheckProcessOutcome:
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be a finite positive number")
        deadline = time.monotonic() + timeout_s
        try:
            returncode = self._process.wait(timeout=self._remaining(deadline))
        except subprocess.TimeoutExpired:
            raise TimeoutError("SELF_CHECK_PROCESS_TIMEOUT") from None
        if not self._stdout.wait(self._remaining(deadline)):
            raise TimeoutError("SELF_CHECK_PROCESS_TIMEOUT")
        if not self._stderr.wait(self._remaining(deadline)):
            raise TimeoutError("SELF_CHECK_PROCESS_TIMEOUT")
        stdout, stdout_utf8_valid = self._stdout.strict_text()
        stderr_summary = self._stderr.snapshot()
        return SelfCheckProcessOutcome(
            returncode=returncode,
            stdout=stdout,
            stderr="",
            cleanup_confirmed=self._group_is_gone(),
            stdout_truncated=self._stdout.truncated,
            stderr_truncated=stderr_summary.truncated,
            stdout_utf8_valid=stdout_utf8_valid,
            stderr_summary=stderr_summary,
        )

    def stderr_snapshot(self) -> SelfCheckStderrSummary:
        return self._stderr.snapshot()

    def cancel(self) -> bool:
        if self._pgid is None:
            return False
        with self._cancel_lock:
            if self._confirmed_cancel:
                return True
            if self._send_group_signal(signal.SIGTERM):
                self._confirmed_cancel = True
                return True
            if self._wait_group_gone(self._terminate_grace_s):
                self._confirmed_cancel = True
                return True
            if self._send_group_signal(signal.SIGKILL):
                self._confirmed_cancel = True
                return True
            self._confirmed_cancel = self._wait_group_gone(self._kill_grace_s)
            return self._confirmed_cancel

    def _send_group_signal(self, signal_number: int) -> bool:
        if self._pgid is None:
            return False
        try:
            self._groups.killpg(self._pgid, signal_number)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return False

    def _wait_group_gone(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                self._process.poll()
            except OSError:
                pass
            if self._group_is_gone():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))

    def _group_is_gone(self) -> bool:
        if self._pgid is None:
            return False
        try:
            self._groups.killpg(self._pgid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return False

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(0.0, deadline - time.monotonic())


class _UnavailableSelfCheckProcess:
    containment_established = False

    def wait(self, timeout_s: float) -> SelfCheckProcessOutcome:
        raise RuntimeError("SELF_CHECK_PROCESS_CONTAINMENT_UNAVAILABLE")

    def cancel(self) -> bool:
        return True

    def stderr_snapshot(self) -> SelfCheckStderrSummary:
        return SelfCheckStderrSummary()


class SubprocessSelfCheckRunner:
    """Launch the fixed self-check argv behind a platform containment boundary."""

    def __init__(
        self,
        *,
        repo_root: Path,
        stdout_budget_bytes: int = 256_000,
        stderr_budget_bytes: int = 64_000,
        terminate_grace_s: float = 2.0,
        kill_grace_s: float = 2.0,
        popen_factory: Callable[..., _PopenProcess] = subprocess.Popen,
        platform_capabilities: PlatformCapabilities,
        posix_process_groups: _PosixProcessGroups = os,
    ) -> None:
        if stdout_budget_bytes < 0 or stderr_budget_bytes < 0:
            raise ValueError("output budgets must not be negative")
        if any(
            not math.isfinite(value) or value < 0
            for value in (terminate_grace_s, kill_grace_s)
        ):
            raise ValueError(
                "cancellation grace periods must be finite and non-negative"
            )
        if not isinstance(platform_capabilities, PlatformCapabilities):
            raise TypeError("platform_capabilities must be PlatformCapabilities")
        self._repo_root = Path(repo_root).resolve()
        self._stdout_budget_bytes = stdout_budget_bytes
        self._stderr_budget_bytes = stderr_budget_bytes
        self._terminate_grace_s = terminate_grace_s
        self._kill_grace_s = kill_grace_s
        self._popen_factory = popen_factory
        self._platform = platform_capabilities
        self._posix_process_groups = posix_process_groups

    def start(
        self, argv: tuple[str, ...], environment: Mapping[str, str]
    ) -> _PosixSelfCheckProcess | _UnavailableSelfCheckProcess:
        if not self._platform.self_check_containment:
            return _UnavailableSelfCheckProcess()
        process = self._popen_factory(
            list(argv),
            cwd=str(self._repo_root),
            env=dict(environment),
            shell=False,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        return _PosixSelfCheckProcess(
            process,
            groups=self._posix_process_groups,
            stdout_budget_bytes=self._stdout_budget_bytes,
            stderr_budget_bytes=self._stderr_budget_bytes,
            terminate_grace_s=self._terminate_grace_s,
            kill_grace_s=self._kill_grace_s,
        )


__all__ = ["SubprocessSelfCheckRunner"]
