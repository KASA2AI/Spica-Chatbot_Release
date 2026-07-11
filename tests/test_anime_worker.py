"""Phase 4: AnimeDownloadWorker -- qbt polling / yt-dlp argv safety / lifecycle.

The worker's core loop is exercised SYNCHRONOUSLY (``execute()``; injectable
popen/clock/sleep) -- no thread is started, no network, no subprocess.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from spica.adapters.torrent.qbittorrent import QBittorrentClient  # noqa: E402
from spica.anime.models import (  # noqa: E402
    DownloadStatus,
    DownloadTerminalCause,
    DownloadTerminalResult,
)
from spica.ports.torrent_client import (  # noqa: E402
    TorrentCancelOutcome,
    TorrentCancelResult,
    TorrentClientError,
)
import ui.workers.anime_worker as anime_worker_module  # noqa: E402
from ui.workers.anime_worker import AnimeDownloadWorker  # noqa: E402

MAGNET = "magnet:?xt=urn:btih:" + "a" * 40
TORRENT_HASH = "14299d250e3e00abb954b9a6020f5546fce5ba8f"
TORRENT_PAYLOAD = (
    b"d8:announce32:https://tracker.example/announce"
    b"13:announce-listll32:https://tracker.example/announcee"
    b"l35:udp://tracker.example:6969/announceee"
    b"4:infod6:lengthi4e4:name7:ep1.mkvee"
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class ManualClock:
    """Deterministic clock: reads are pure; only sleep/advance moves time."""

    def __init__(self, initial: float = 0.0):
        self._value = float(initial)
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._value

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._value += float(seconds)


def _timed_lines(clock: ManualClock, lines, *, step: float = 10.0):
    """Advance explicit source time before each emitted process-output line."""
    for line in lines:
        clock.advance(step)
        yield line


class FakeTorrent:
    def __init__(self, statuses):
        self.statuses = list(statuses)   # DownloadStatus | TorrentClientError
        self.added: list[str] = []
        self.payloads: list[bytes] = []
        self.expected_hashes: list[str] = []
        self.subfolders: list[str | None] = []
        self.cancelled: list[str] = []

    def add_magnet(self, magnet: str, *, subfolder: str | None = None) -> str:
        self.added.append(magnet)
        self.subfolders.append(subfolder)
        return "a" * 40

    def add_torrent_bytes(
        self, payload: bytes, *, expected_infohash: str,
        subfolder: str | None = None,
    ) -> str:
        self.payloads.append(payload)
        self.expected_hashes.append(expected_infohash)
        self.subfolders.append(subfolder)
        return expected_infohash

    def status(self, task_id: str) -> DownloadStatus:
        item = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if isinstance(item, TorrentClientError):
            raise item
        return item

    def cancel(self, task_id: str) -> TorrentCancelOutcome:
        self.cancelled.append(task_id)
        return TorrentCancelOutcome(TorrentCancelResult.CANCELLED)


class FakeProc:
    def __init__(self, lines, rc=0):
        self.stdout = iter(f"{line}\n" for line in lines)
        self.rc = rc
        self.terminated = 0
        self.killed = 0
        self._done = False

    def wait(self, timeout=None):
        del timeout
        self._done = True
        return self.rc

    def poll(self):
        return self.rc if self._done else None

    def terminate(self):
        self.terminated += 1
        self._done = True

    def kill(self):
        self.killed += 1
        self._done = True


_PIPE_EOF = object()


class BlockingStdout:
    """Queue-backed pipe whose iterator records the sole reading thread."""

    def __init__(self, lines=(), *, finish=False, first_read_gate=None,
                 close_unblocks=True, before_line=None):
        self._items = queue.Queue()
        for line in lines:
            self._items.put(f"{line}\n")
        self._finished = False
        self._first_read_gate = first_read_gate
        self._first_read = True
        self._close_unblocks = close_unblocks
        self._before_line = before_line
        self._on_eof = None
        self.read_started = threading.Event()
        self.line_read = threading.Event()
        self.reader_thread_ids = []
        self.close_calls = 0
        if finish:
            self.finish()

    def __iter__(self):
        return self

    def __next__(self):
        self.reader_thread_ids.append(threading.get_ident())
        self.read_started.set()
        if self._first_read and self._first_read_gate is not None:
            self._first_read = False
            if not self._first_read_gate.wait(2):
                raise RuntimeError("test reader gate timed out")
        try:
            item = self._items.get(timeout=2)
        except queue.Empty as exc:
            raise RuntimeError("test pipe remained blocked") from exc
        if item is _PIPE_EOF:
            if self._on_eof is not None:
                self._on_eof()
            raise StopIteration
        if self._before_line is not None:
            self._before_line()
        self.line_read.set()
        return item

    def feed(self, line):
        self._items.put(f"{line}\n")

    def finish(self):
        if not self._finished:
            self._finished = True
            self._items.put(_PIPE_EOF)

    def close(self):
        self.close_calls += 1
        if self._close_unblocks:
            self.finish()


class ControlledProc:
    def __init__(self, stdout, *, rc=0, exited=False,
                 terminate_exits=True, kill_exits=True,
                 exit_after_kills: int | None = None,
                 terminate_unblocks_pipe=True,
                 kill_unblocks_pipe=True, natural_exit_on_eof=True):
        self.stdout = stdout
        self.rc = rc
        self.terminated = 0
        self.killed = 0
        self.wait_calls = 0
        self.wait_started = threading.Event()
        self.reaped = threading.Event()
        self._exited = threading.Event()
        self._terminate_exits = terminate_exits
        self._kill_exits = kill_exits
        self._exit_after_kills = exit_after_kills
        self._terminate_unblocks_pipe = terminate_unblocks_pipe
        self._kill_unblocks_pipe = kill_unblocks_pipe
        self._natural_exit_on_eof = natural_exit_on_eof
        self.stdout._on_eof = self._stdout_eof
        if exited:
            self._exited.set()

    def _stdout_eof(self):
        if self._natural_exit_on_eof:
            self._exited.set()

    def poll(self):
        return self.rc if self._exited.is_set() else None

    def wait(self, timeout=None):
        self.wait_calls += 1
        self.wait_started.set()
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired("fake-yt-dlp", timeout)
        self.reaped.set()
        return self.rc

    def terminate(self):
        self.terminated += 1
        if self._terminate_exits:
            self._exited.set()
            if self._terminate_unblocks_pipe:
                self.stdout.finish()

    def kill(self):
        self.killed += 1
        exits_now = (self._kill_exits
                     or (self._exit_after_kills is not None
                         and self.killed >= self._exit_after_kills))
        if exits_now:
            self._exited.set()
        if exits_now or self._kill_unblocks_pipe:
            self.stdout.finish()


def _worker(tmp_path, *, locator=MAGNET, torrent=None, popen=None,
            resume_task_id=None, cookies_file="", stall_timeout_minutes=10.0,
            resume_created_at=None, wall_clock=None, clock=None,
            sleep=None, poll_seconds=1.0,
            title="无职转生 第三季", series_title="无职转生", **kw):
    if clock is None:
        manual_clock = ManualClock()
        clock = manual_clock.now
        if sleep is None:
            sleep = manual_clock.sleep
    return AnimeDownloadWorker(
        request_id="REQ", episode_key="无职转生|s3|e1", title=title,
        series_title=series_title,
        locator=locator, torrent=torrent, download_dir=str(tmp_path),
        poll_seconds=poll_seconds, stall_timeout_minutes=stall_timeout_minutes,
        cookies_file=cookies_file, resume_task_id=resume_task_id,
        resume_created_at=resume_created_at,
        popen=popen, clock=clock,
        wall_clock=wall_clock, sleep=sleep or (lambda s: None), **kw)


def _worker_with_popen_only(tmp_path, popen):
    return AnimeDownloadWorker(
        request_id="REQ", episode_key="无职转生|s3|e1",
        title="无职转生 第三季", series_title="无职转生",
        locator="BV1234567890:1", torrent=None,
        download_dir=str(tmp_path), popen=popen)


def _downloading(p: float) -> DownloadStatus:
    return DownloadStatus(task_id="a" * 40, state="downloading", progress=p)


def _completed(path: str) -> DownloadStatus:
    return DownloadStatus(task_id="a" * 40, state="completed", progress=1.0,
                          save_path=path)


def _ytdlp_progress(*, downloaded: int, total: int = 20 * 1024 * 1024,
                    format_id: str = "100026", status: str = "downloading",
                    tmpfilename: str = "episode.f100026.mp4.part") -> str:
    return "SPICA:" + json.dumps({
        "format_id": format_id,
        "progress": {
            "status": status,
            "downloaded_bytes": downloaded,
            "total_bytes": total,
            "tmpfilename": tmpfilename,
        },
    })


# -- qbt magnet lane -----------------------------------------------------------

def test_magnet_add_poll_complete(qapp, tmp_path):
    ft = FakeTorrent([_downloading(0.3), _downloading(0.8),
                      _completed(str(tmp_path / "ep1.mkv"))])
    w = _worker(tmp_path, torrent=ft)
    started, prog = [], []
    w.task_started.connect(lambda rid, tid: started.append((rid, tid)))
    w.progress.connect(lambda rid, p, ph: prog.append(p))
    event = w.execute()
    assert ft.added == [MAGNET]                       # magnet handed verbatim
    assert ft.subfolders == ["无职转生"]               # grouped by anime NAME (series_title)
    assert started == [("REQ", "a" * 40)]             # btih reported (P1-9 seam)
    assert prog == [0.3, 0.8, 1.0]
    assert event.error is None
    assert event.save_path == str(tmp_path / "ep1.mkv")
    assert event.elapsed_seconds is not None and event.elapsed_seconds > 0


def test_magnet_reports_metadata_phase_before_download(qapp, tmp_path):
    ft = FakeTorrent([
        DownloadStatus(task_id="a" * 40, state="metadata", progress=0.0),
        _completed(str(tmp_path / "ep1.mkv")),
    ])
    worker = _worker(tmp_path, torrent=ft)
    phases = []
    worker.progress.connect(lambda rid, progress, phase: phases.append(phase))

    event = worker.execute()

    assert phases == ["metadata", "downloading"]
    assert event.error is None


def test_torrent_payload_add_poll_complete(qapp, tmp_path):
    ft = FakeTorrent([_downloading(0.3),
                      _completed(str(tmp_path / "ep1.mkv"))])
    locator = "magnet:?xt=urn:btih:" + TORRENT_HASH
    w = _worker(
        tmp_path, locator=locator, torrent=ft,
        torrent_payload_b64=base64.b64encode(TORRENT_PAYLOAD).decode("ascii"))

    event = w.execute()

    assert ft.payloads == [TORRENT_PAYLOAD]
    assert ft.expected_hashes == [TORRENT_HASH]
    assert ft.subfolders == ["无职转生"]
    assert event.error is None


def test_magnet_transient_error_reconnects_not_fails(qapp, tmp_path):
    # P1-10: a connection blip means keep polling, never a failure verdict
    ft = FakeTorrent([TorrentClientError("UNREACHABLE", "down"),
                      TorrentClientError("API_ERROR", "503"),
                      _completed(str(tmp_path / "ep1.mkv"))])
    event = _worker(tmp_path, torrent=ft).execute()
    assert event.error is None
    assert event.save_path == str(tmp_path / "ep1.mkv")


def test_magnet_task_not_found_is_terminal(qapp, tmp_path):
    ft = FakeTorrent([TorrentClientError("TASK_NOT_FOUND", "gone")])
    event = _worker(tmp_path, torrent=ft).execute()
    assert event.error is not None and "TASK_NOT_FOUND" in event.error


def test_magnet_error_state_reports(qapp, tmp_path):
    ft = FakeTorrent([DownloadStatus(task_id="a" * 40, state="error",
                                     progress=0.1, error="missingFiles")])
    event = _worker(tmp_path, torrent=ft).execute()
    assert event.error is not None and "missingFiles" in event.error


def test_magnet_stall_timeout_cancels_once_and_is_terminal(qapp, tmp_path):
    ft = FakeTorrent(
        [_downloading(0.5)] * 10 + [_completed(str(tmp_path / "e.mkv"))])
    w = _worker(
        tmp_path, torrent=ft, stall_timeout_minutes=1.0, poll_seconds=10.0)

    event = w.execute()

    assert event.error is not None
    assert event.terminal_result is DownloadTerminalResult.CANCELLED
    assert event.terminal_cause is DownloadTerminalCause.STALL
    assert "移除任务并请求删除未完成数据" in event.error
    assert ft.cancelled == ["a" * 40]


def test_stall_timeout_does_not_fire_at_599_point_9_seconds(qapp, tmp_path):
    clock = ManualClock()
    torrent = FakeTorrent([
        _downloading(0.5),
        _downloading(0.5),
        _completed(str(tmp_path / "ep.mkv")),
    ])
    worker = _worker(
        tmp_path, torrent=torrent, stall_timeout_minutes=10.0,
        poll_seconds=599.9, clock=clock.now, sleep=clock.sleep)

    event = worker.execute()

    assert event.terminal_result is DownloadTerminalResult.COMPLETED
    assert torrent.cancelled == []


def test_stall_timeout_fires_at_exactly_600_seconds(qapp, tmp_path):
    clock = ManualClock()
    torrent = FakeTorrent([_downloading(0.5)])
    worker = _worker(
        tmp_path, torrent=torrent, stall_timeout_minutes=10.0,
        poll_seconds=600.0, clock=clock.now, sleep=clock.sleep)

    event = worker.execute()

    assert event.terminal_result is DownloadTerminalResult.CANCELLED
    assert event.terminal_cause is DownloadTerminalCause.STALL
    assert torrent.cancelled == ["a" * 40]


def test_stall_cutoff_error_state_enters_scoped_cancel(qapp, tmp_path):
    clock = ManualClock()
    error = DownloadStatus(
        task_id="a" * 40,
        state="error",
        progress=0.5,
        error="missingFiles",
    )
    torrent = FakeTorrent([_downloading(0.5), error, error])
    worker = _worker(
        tmp_path, torrent=torrent, stall_timeout_minutes=10.0,
        poll_seconds=600.0, clock=clock.now, sleep=clock.sleep)

    event = worker.execute()

    assert event.terminal_result is DownloadTerminalResult.CANCELLED
    assert event.terminal_cause is DownloadTerminalCause.STALL
    assert torrent.cancelled == ["a" * 40]


def test_first_observed_progress_is_baseline_at_hard_cutoff(qapp, tmp_path):
    clock = ManualClock()
    half = _downloading(0.5)
    torrent = FakeTorrent([
        TorrentClientError("UNREACHABLE", "down"),
        half,
        half,
        _completed(str(tmp_path / "ep.mkv")),
    ])
    worker = _worker(
        tmp_path, torrent=torrent, stall_timeout_minutes=10.0,
        poll_seconds=600.0, clock=clock.now, sleep=clock.sleep)

    event = worker.execute()

    assert event.terminal_result is DownloadTerminalResult.CANCELLED
    assert event.terminal_cause is DownloadTerminalCause.STALL
    assert torrent.cancelled == ["a" * 40]


def test_real_progress_on_cutoff_final_read_resets_timer(qapp, tmp_path):
    clock = ManualClock()
    torrent = FakeTorrent([
        _downloading(0.5),
        _downloading(0.5),
        _downloading(0.6),
        _completed(str(tmp_path / "ep.mkv")),
    ])
    worker = _worker(
        tmp_path, torrent=torrent, stall_timeout_minutes=10.0,
        poll_seconds=600.0, clock=clock.now, sleep=clock.sleep)

    event = worker.execute()

    assert event.terminal_result is DownloadTerminalResult.COMPLETED
    assert torrent.cancelled == []


def test_magnet_stall_timeout_cancel_failure_still_returns_terminal(
        qapp, tmp_path):
    class CancelFailsTorrent(FakeTorrent):
        def cancel(self, task_id: str) -> TorrentCancelOutcome:
            self.cancelled.append(task_id)
            raise TorrentClientError("UNREACHABLE", "qbt down")

    ft = CancelFailsTorrent([_downloading(0.5)] * 20)
    w = _worker(
        tmp_path, torrent=ft, stall_timeout_minutes=1.0, poll_seconds=10.0)

    event = w.execute()

    assert event is not None
    assert event.error is not None
    assert event.terminal_result is DownloadTerminalResult.UNCONFIRMED
    assert event.terminal_cause is DownloadTerminalCause.STALL
    assert "后台任务可能仍在" in event.error
    assert ft.cancelled == ["a" * 40]


def test_stall_cancel_none_outcome_is_unconfirmed(qapp, tmp_path):
    class InvalidLegacyTorrent(FakeTorrent):
        def cancel(self, task_id: str) -> None:
            self.cancelled.append(task_id)
            return None

    torrent = InvalidLegacyTorrent([_downloading(0.5)])
    worker = _worker(
        tmp_path, torrent=torrent, stall_timeout_minutes=1.0,
        poll_seconds=60.0)

    event = worker.execute()

    assert event.terminal_result is DownloadTerminalResult.UNCONFIRMED
    assert event.terminal_cause is DownloadTerminalCause.STALL
    assert "后台任务可能仍在" in event.error


def test_stall_final_task_not_found_is_disambiguated_by_cancel_port(
        qapp, tmp_path):
    class OwnerLostTorrent(FakeTorrent):
        def cancel(self, task_id: str) -> TorrentCancelOutcome:
            self.cancelled.append(task_id)
            raise TorrentClientError("CANCEL_OWNER_LOST", "recategorized")

    clock = ManualClock()
    torrent = OwnerLostTorrent([
        _downloading(0.5),
        _downloading(0.5),
        TorrentClientError("TASK_NOT_FOUND", "category miss"),
    ])
    worker = _worker(
        tmp_path, torrent=torrent, stall_timeout_minutes=10.0,
        poll_seconds=600.0, clock=clock.now, sleep=clock.sleep)

    event = worker.execute()

    assert event.terminal_result is DownloadTerminalResult.UNCONFIRMED
    assert event.terminal_cause is DownloadTerminalCause.STALL
    assert torrent.cancelled == ["a" * 40]


def test_magnet_stall_final_recheck_completed_wins(qapp, tmp_path):
    ft = FakeTorrent(
        [_downloading(0.5)] * 6 + [_completed(str(tmp_path / "e.mkv"))])
    w = _worker(tmp_path, torrent=ft, stall_timeout_minutes=1.0)

    event = w.execute()

    assert event.error is None
    assert event.save_path == str(tmp_path / "e.mkv")
    assert ft.cancelled == []


class _BlockingCutoffTorrent(FakeTorrent):
    """Block only the cutoff's final status read, not normal polling."""

    def __init__(self, clock, final_status):
        super().__init__([_downloading(0.5)])
        self._clock = clock
        self._final_status = final_status
        self._cutoff_poll_seen = False
        self.final_read_started = threading.Event()
        self.release_final_read = threading.Event()

    def status(self, task_id: str) -> DownloadStatus:
        del task_id
        if self._clock.now() < 60.0:
            return _downloading(0.5)
        if not self._cutoff_poll_seen:
            self._cutoff_poll_seen = True
            return _downloading(0.5)
        self.final_read_started.set()
        assert self.release_final_read.wait(2), "test did not release final qBT read"
        return self._final_status


def _execute_in_thread(worker):
    box = {}

    def execute():
        box["event"] = worker.execute()

    thread = threading.Thread(target=execute)
    thread.start()
    return thread, box


def test_shutdown_claimed_during_stall_final_read_preserves_qbt(qapp, tmp_path):
    clock = ManualClock()
    torrent = _BlockingCutoffTorrent(clock, _downloading(0.5))
    worker = _worker(
        tmp_path, torrent=torrent, stall_timeout_minutes=1.0,
        clock=clock.now, sleep=clock.sleep)
    thread, box = _execute_in_thread(worker)
    assert torrent.final_read_started.wait(2)

    worker.cancel()
    torrent.release_final_read.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert torrent.cancelled == []
    assert box["event"].terminal_result is DownloadTerminalResult.PRESERVED
    assert box["event"].terminal_cause is DownloadTerminalCause.SHUTDOWN


def test_manual_claimed_during_stall_final_read_wins_over_timeout(qapp, tmp_path):
    clock = ManualClock()
    torrent = _BlockingCutoffTorrent(clock, _downloading(0.5))
    worker = _worker(
        tmp_path, torrent=torrent, stall_timeout_minutes=1.0,
        clock=clock.now, sleep=clock.sleep)
    thread, box = _execute_in_thread(worker)
    assert torrent.final_read_started.wait(2)

    assert worker.request_download_cancel() is True
    torrent.release_final_read.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert torrent.cancelled == ["a" * 40]
    assert box["event"].terminal_result is DownloadTerminalResult.CANCELLED
    assert box["event"].terminal_cause is DownloadTerminalCause.MANUAL


def test_manual_claim_during_status_error_is_not_lost(qapp, tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingErrorTorrent(FakeTorrent):
        def status(self, task_id: str) -> DownloadStatus:
            del task_id
            entered.set()
            assert release.wait(2)
            return DownloadStatus(
                task_id="a" * 40, state="error", progress=0.2,
                error="missingFiles")

    torrent = BlockingErrorTorrent([_downloading(0.2)])
    worker = _worker(tmp_path, torrent=torrent)
    thread, box = _execute_in_thread(worker)
    assert entered.wait(2)

    assert worker.request_download_cancel() is True
    release.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert torrent.cancelled == ["a" * 40]
    assert box["event"].terminal_result is DownloadTerminalResult.CANCELLED
    assert box["event"].terminal_cause is DownloadTerminalCause.MANUAL


def test_manual_owner_is_not_overwritten_by_later_shutdown(qapp, tmp_path):
    torrent = FakeTorrent([_downloading(0.2), _downloading(0.2)])
    worker = _worker(
        tmp_path, locator="", torrent=torrent, resume_task_id="a" * 40)

    assert worker.request_download_cancel() is True
    worker.cancel()
    event = worker.execute()

    assert torrent.cancelled == ["a" * 40]
    assert event.terminal_result is DownloadTerminalResult.CANCELLED
    assert event.terminal_cause is DownloadTerminalCause.MANUAL


def test_shutdown_owner_rejects_later_manual_and_never_mutates_qbt(
        qapp, tmp_path):
    torrent = FakeTorrent([_downloading(0.2)])
    worker = _worker(
        tmp_path, locator="", torrent=torrent, resume_task_id="a" * 40)

    worker.cancel()
    assert worker.request_download_cancel() is False
    event = worker.execute()

    assert torrent.cancelled == []
    assert event.terminal_result is DownloadTerminalResult.PRESERVED
    assert event.terminal_cause is DownloadTerminalCause.SHUTDOWN


def test_magnet_progress_resets_stall_timeout(qapp, tmp_path):
    ft = FakeTorrent(
        [_downloading(0.1)] * 5
        + [_downloading(0.2)] * 5
        + [_completed(str(tmp_path / "e.mkv"))])
    w = _worker(tmp_path, torrent=ft, stall_timeout_minutes=1.0)

    event = w.execute()

    assert event.error is None
    assert ft.cancelled == []


def test_magnet_progress_after_rollback_resets_stall_timeout(qapp, tmp_path):
    ft = FakeTorrent(
        [_downloading(0.5)] * 4
        + [_downloading(0.1), _downloading(0.2), _downloading(0.2)]
        + [_completed(str(tmp_path / "e.mkv"))])
    w = _worker(tmp_path, torrent=ft, stall_timeout_minutes=1.0)

    event = w.execute()

    assert event.error is None
    assert event.save_path == str(tmp_path / "e.mkv")
    assert ft.cancelled == []


def test_magnet_cancel_stops_polling_only(qapp, tmp_path):
    # P1-9: qbt is an external resident service -- cancel never touches the task
    ft = FakeTorrent([_downloading(0.2)])
    w = _worker(tmp_path, torrent=ft)
    w.cancel()
    event = w.execute()
    assert event.terminal_result is DownloadTerminalResult.PRESERVED
    assert event.terminal_cause is DownloadTerminalCause.SHUTDOWN
    assert ft.cancelled == []                          # task untouched


def test_manual_cancel_qbt_is_terminal_and_deletes_partial_data(
        qapp, tmp_path):
    ft = FakeTorrent([_downloading(0.2), _downloading(0.2)])
    w = _worker(tmp_path, torrent=ft)
    calls = []

    def request_during_poll(_seconds):
        calls.append(w.request_download_cancel())

    w._sleep = request_during_poll

    event = w.execute()

    assert calls == [True]
    assert event is not None and event.error is not None
    assert event.terminal_result is DownloadTerminalResult.CANCELLED
    assert event.terminal_cause is DownloadTerminalCause.MANUAL
    assert "删除未完成数据" in event.error
    assert ft.cancelled == ["a" * 40]
    assert w.request_download_cancel() is False        # idempotent


def test_manual_cancel_qbt_final_completion_wins(qapp, tmp_path):
    ft = FakeTorrent([
        _downloading(0.2),
        _completed(str(tmp_path / "ep1.mkv")),
    ])
    w = _worker(tmp_path, torrent=ft)
    w._sleep = lambda _seconds: w.request_download_cancel()

    event = w.execute()

    assert event.error is None
    assert event.save_path == str(tmp_path / "ep1.mkv")
    assert ft.cancelled == []


def test_manual_cancel_uses_adapter_latest_completed_outcome(qapp, tmp_path):
    task_hash = "a" * 40
    incomplete = [{
        "hash": task_hash, "state": "downloading", "progress": 0.99,
    }]
    completed = [{
        "hash": task_hash, "state": "stoppedUP", "progress": 1.0,
        "content_path": str(tmp_path / "ep1.mkv"),
    }]

    class CompletionRaceSession:
        def __init__(self):
            # worker status, worker manual final status, adapter initial read,
            # then adapter's first post-stop read observes completion.
            self.snapshots = [incomplete, incomplete, incomplete, completed]
            self.posts = []

        def get(self, url, params=None, timeout=None, **kwargs):
            del timeout, kwargs
            if "app/version" in url:
                return SimpleNamespace(status_code=200, text="v5.2.3")
            if "torrents/info" in url:
                snapshot = self.snapshots.pop(0)
                return SimpleNamespace(
                    status_code=200, text="", json=lambda: snapshot)
            raise AssertionError((url, params))

        def post(self, url, data=None, timeout=None, **kwargs):
            del timeout, kwargs
            self.posts.append((url, data))
            return SimpleNamespace(status_code=204, text="")

    session = CompletionRaceSession()
    client = QBittorrentClient(
        "http://127.0.0.1:8080", str(tmp_path), session=session,
        sleep=lambda _seconds: None)
    worker = _worker(
        tmp_path, locator="", torrent=client, resume_task_id=task_hash)
    worker._sleep = lambda _seconds: worker.request_download_cancel()

    event = worker.execute()

    assert event.terminal_result is DownloadTerminalResult.COMPLETED
    assert event.terminal_cause is DownloadTerminalCause.MANUAL
    assert event.save_path == str(tmp_path / "ep1.mkv")
    assert not any("torrents/delete" in url for url, _ in session.posts)


def test_manual_cancel_qbt_failure_is_terminal_but_unconfirmed(
        qapp, tmp_path):
    class CancelFailsTorrent(FakeTorrent):
        def cancel(self, task_id: str) -> TorrentCancelOutcome:
            self.cancelled.append(task_id)
            raise TorrentClientError("UNREACHABLE", "qbt down")

    ft = CancelFailsTorrent([_downloading(0.2), _downloading(0.2)])
    w = _worker(tmp_path, torrent=ft)
    w._sleep = lambda _seconds: w.request_download_cancel()

    event = w.execute()

    assert event is not None and event.error is not None
    assert event.terminal_result is DownloadTerminalResult.UNCONFIRMED
    assert event.terminal_cause is DownloadTerminalCause.MANUAL
    assert "后台任务可能仍在" in event.error
    assert ft.cancelled == ["a" * 40]


def test_manual_final_task_not_found_is_disambiguated_by_cancel_port(
        qapp, tmp_path):
    class OwnerLostTorrent(FakeTorrent):
        def cancel(self, task_id: str) -> TorrentCancelOutcome:
            self.cancelled.append(task_id)
            raise TorrentClientError("CANCEL_OWNER_LOST", "recategorized")

    torrent = OwnerLostTorrent([
        _downloading(0.2),
        TorrentClientError("TASK_NOT_FOUND", "category miss"),
    ])
    worker = _worker(tmp_path, torrent=torrent)
    worker._sleep = lambda _seconds: worker.request_download_cancel()

    event = worker.execute()

    assert event.terminal_result is DownloadTerminalResult.UNCONFIRMED
    assert event.terminal_cause is DownloadTerminalCause.MANUAL
    assert torrent.cancelled == ["a" * 40]


def test_manual_cancel_qthread_still_emits_terminal_ready(qapp, tmp_path):
    ft = FakeTorrent([_downloading(0.2)])
    worker = _worker(
        tmp_path, locator="", torrent=ft, resume_task_id="a" * 40)
    events = []
    worker.ready.connect(events.append)

    assert worker.request_download_cancel() is True
    worker.start()
    assert worker.wait(1000)
    qapp.processEvents()

    assert len(events) == 1
    assert events[0].error is not None
    assert events[0].terminal_result is DownloadTerminalResult.CANCELLED
    assert events[0].terminal_cause is DownloadTerminalCause.MANUAL
    assert ft.cancelled == ["a" * 40]


def test_resume_mode_polls_existing_task_elapsed_none(qapp, tmp_path):
    # restart reconcile (P1-9): unknown age -> elapsed None (=> confirmation only)
    ft = FakeTorrent([_completed(str(tmp_path / "ep1.mkv"))])
    w = _worker(tmp_path, locator="", torrent=ft, resume_task_id="b" * 40)
    event = w.execute()
    assert ft.added == []                              # never re-adds
    assert event.error is None
    assert event.elapsed_seconds is None


def test_resume_mode_stall_timeout_cancels_existing_task(qapp, tmp_path):
    ft = FakeTorrent([_downloading(0.5)] * 10)
    w = _worker(
        tmp_path,
        locator="",
        torrent=ft,
        resume_task_id="b" * 40,
        stall_timeout_minutes=1.0,
    )

    event = w.execute()

    assert event.error is not None
    assert event.terminal_result is DownloadTerminalResult.CANCELLED
    assert event.terminal_cause is DownloadTerminalCause.STALL
    assert ft.cancelled == ["b" * 40]


def test_resume_mode_inherits_qbt_last_activity_and_cancels_immediately(
        qapp, tmp_path):
    stale = DownloadStatus(
        task_id="b" * 40, state="downloading", progress=0.5,
        last_activity_at=100.0)
    ft = FakeTorrent([stale, stale])
    w = _worker(
        tmp_path, locator="", torrent=ft, resume_task_id="b" * 40,
        resume_created_at="1970-01-01T00:01:00+00:00",
        stall_timeout_minutes=1.0, clock=lambda: 500.0,
        wall_clock=lambda: 1000.0)

    event = w.execute()

    assert event.error is not None
    assert event.terminal_result is DownloadTerminalResult.CANCELLED
    assert event.terminal_cause is DownloadTerminalCause.STALL
    assert ft.cancelled == ["b" * 40]


def test_resume_mode_completed_first_sample_wins_over_old_activity(
        qapp, tmp_path):
    completed = DownloadStatus(
        task_id="b" * 40, state="completed", progress=1.0,
        save_path=str(tmp_path / "ep1.mkv"), last_activity_at=100.0)
    ft = FakeTorrent([completed])
    w = _worker(
        tmp_path, locator="", torrent=ft, resume_task_id="b" * 40,
        resume_created_at="1970-01-01T00:01:00+00:00",
        stall_timeout_minutes=1.0, clock=lambda: 500.0,
        wall_clock=lambda: 1000.0)

    event = w.execute()

    assert event.error is None
    assert event.save_path == str(tmp_path / "ep1.mkv")
    assert ft.cancelled == []


def test_resume_mode_prefers_newer_qbt_activity_over_old_pending_time(
        qapp, tmp_path):
    active = DownloadStatus(
        task_id="b" * 40, state="downloading", progress=0.5,
        last_activity_at=990.0)
    ft = FakeTorrent([active, _completed(str(tmp_path / "ep1.mkv"))])
    w = _worker(
        tmp_path, locator="", torrent=ft, resume_task_id="b" * 40,
        resume_created_at="1970-01-01T00:01:00+00:00",
        stall_timeout_minutes=1.0, clock=lambda: 500.0,
        wall_clock=lambda: 1000.0)

    event = w.execute()

    assert event.error is None
    assert ft.cancelled == []


def test_resume_valid_old_qbt_activity_overrides_newer_pending_time(
        qapp, tmp_path):
    stale = DownloadStatus(
        task_id="b" * 40, state="downloading", progress=0.5,
        last_activity_at=100.0)
    torrent = FakeTorrent([
        stale,
        stale,
        _completed(str(tmp_path / "ep1.mkv")),
    ])
    worker = _worker(
        tmp_path, locator="", torrent=torrent, resume_task_id="b" * 40,
        # Pending is newer, but a valid qBT activity timestamp is authoritative.
        resume_created_at="1970-01-01T00:15:00+00:00",
        stall_timeout_minutes=10.0, clock=lambda: 500.0,
        wall_clock=lambda: 1000.0)

    event = worker.execute()

    assert event.terminal_result is DownloadTerminalResult.CANCELLED
    assert event.terminal_cause is DownloadTerminalCause.STALL
    assert torrent.cancelled == ["b" * 40]


def test_resume_transient_final_recheck_keeps_nine_minute_inactivity(
        qapp, tmp_path):
    clock = ManualClock()
    recovered = DownloadStatus(
        task_id="b" * 40, state="downloading", progress=0.5,
        last_activity_at=460.0)

    class RecordsCancelTime(FakeTorrent):
        def __init__(self):
            super().__init__([
                TorrentClientError("UNREACHABLE", "down"),
                TorrentClientError("UNREACHABLE", "down"),
                recovered,
                recovered,
                recovered,
            ])
            self.cancel_times = []

        def cancel(self, task_id: str) -> TorrentCancelOutcome:
            self.cancel_times.append(clock.now())
            return super().cancel(task_id)

    torrent = RecordsCancelTime()
    worker = _worker(
        tmp_path, locator="", torrent=torrent, resume_task_id="b" * 40,
        resume_created_at="1970-01-01T00:07:40+00:00",
        stall_timeout_minutes=10.0, poll_seconds=60.0,
        clock=clock.now, sleep=clock.sleep, wall_clock=lambda: 1000.0)

    event = worker.execute()

    assert event.terminal_result is DownloadTerminalResult.CANCELLED
    assert torrent.cancel_times == [pytest.approx(120.0)]


@pytest.mark.parametrize(
    ("bad_activity", "pending_created_at"),
    [
        (2000.0, "1970-01-01T00:01:40+00:00"),
        (1_721_234_567_000, "1970-01-01T00:01:40+00:00"),
        (float("nan"), "1970-01-01T00:16:30+00:00"),
        (float("inf"), "1970-01-01T00:16:30+00:00"),
        (10 ** 400, "1970-01-01T00:16:30+00:00"),
    ],
)
def test_resume_bad_or_future_qbt_activity_never_deletes_immediately(
        qapp, tmp_path, bad_activity, pending_created_at):
    clock = ManualClock()
    uncertain = DownloadStatus(
        task_id="b" * 40, state="downloading", progress=0.5,
        last_activity_at=bad_activity)
    torrent = FakeTorrent([uncertain])
    worker = _worker(
        tmp_path, locator="", torrent=torrent, resume_task_id="b" * 40,
        # Future/millisecond evidence is fail-safe even with an ancient pending
        # timestamp; malformed evidence uses the recent pending fallback.
        resume_created_at=pending_created_at,
        stall_timeout_minutes=10.0, poll_seconds=1.0,
        clock=clock.now, wall_clock=lambda: 1000.0,
        sleep=lambda _seconds: worker.cancel(),
    )

    event = worker.execute()

    assert event.terminal_result is DownloadTerminalResult.PRESERVED
    assert event.terminal_cause is DownloadTerminalCause.SHUTDOWN
    assert torrent.cancelled == []


def test_resume_mode_falls_back_to_pending_created_at_when_qbt_unreachable(
        qapp, tmp_path):
    stale = _downloading(0.0)
    ft = FakeTorrent([TorrentClientError("UNREACHABLE", "down"), stale])
    w = _worker(
        tmp_path, locator="", torrent=ft, resume_task_id="b" * 40,
        resume_created_at="1970-01-01T00:01:00+00:00",
        stall_timeout_minutes=1.0, clock=lambda: 500.0,
        wall_clock=lambda: 1000.0)

    event = w.execute()

    assert event.error is not None
    assert event.terminal_result is DownloadTerminalResult.CANCELLED
    assert event.terminal_cause is DownloadTerminalCause.STALL
    assert ft.cancelled == ["b" * 40]


# -- F2: auth/API failures must terminate, only UNREACHABLE stays transient -------

def _guarded(worker, limit=500):
    """Replace the worker's sleep with a runaway guard: a poll loop that never
    terminates gets cancelled after ``limit`` sleeps, so a broken (pre-F2)
    implementation FAILS the test instead of hanging it."""
    calls = {"n": 0}

    def s(sec):
        calls["n"] += 1
        if calls["n"] >= limit:
            worker.cancel()

    worker._sleep = s
    return calls


def test_magnet_auth_failed_is_terminal(qapp, tmp_path):
    ft = FakeTorrent([TorrentClientError("AUTH_FAILED", "login rejected")])
    w = _worker(tmp_path, torrent=ft)
    _guarded(w)
    event = w.execute()
    assert event is not None, "AUTH_FAILED looped forever instead of failing"
    assert event.error is not None and "AUTH_FAILED" in event.error


def test_magnet_api_error_bounded_retry(qapp, tmp_path):
    ft = FakeTorrent([TorrentClientError("API_ERROR", "HTTP 500")])  # persists
    w = _worker(tmp_path, torrent=ft)
    _guarded(w)
    event = w.execute()
    assert event is not None, "API_ERROR looped forever instead of failing"
    assert event.error is not None and "API_ERROR" in event.error


def test_magnet_api_error_recovery_below_limit_still_succeeds(qapp, tmp_path):
    # P1-10 not regressed: a short API_ERROR streak that recovers is transient
    ft = FakeTorrent([TorrentClientError("API_ERROR", "500"),
                      TorrentClientError("API_ERROR", "500"),
                      _completed(str(tmp_path / "ep1.mkv"))])
    event = _worker(tmp_path, torrent=ft).execute()
    assert event.error is None
    assert event.save_path == str(tmp_path / "ep1.mkv")


def test_magnet_unreachable_below_timeout_still_recovers(qapp, tmp_path):
    # A short qBT outage remains transient and may recover before the cutoff.
    ft = FakeTorrent([TorrentClientError("UNREACHABLE", "down")] * 3
                     + [_completed(str(tmp_path / "ep1.mkv"))])
    event = _worker(tmp_path, torrent=ft).execute()
    assert event.error is None


def test_magnet_unreachable_beyond_stall_timeout_ends_locally(
        qapp, tmp_path):
    ft = FakeTorrent([TorrentClientError("UNREACHABLE", "down")] * 10
                     + [_completed(str(tmp_path / "ep1.mkv"))])
    w = _worker(
        tmp_path, torrent=ft, stall_timeout_minutes=1.0, poll_seconds=10.0)

    event = w.execute()

    assert event.error is not None
    assert event.terminal_result is DownloadTerminalResult.CANCELLED
    assert event.terminal_cause is DownloadTerminalCause.STALL
    assert ft.cancelled == ["a" * 40]


# -- F3: the qbt poll sleep must be interruptible ----------------------------------

def test_qbt_poll_sleep_is_sliced_and_interruptible(qapp, tmp_path):
    ft = FakeTorrent([_downloading(0.1)])          # downloads forever
    w = _worker(tmp_path, torrent=ft)
    slices: list[float] = []

    def s(sec):
        slices.append(sec)
        if len(slices) == 3:
            w.cancel()                             # cancel lands MID-sleep

    w._sleep = s
    event = w.execute()
    assert event.terminal_result is DownloadTerminalResult.PRESERVED
    assert event.terminal_cause is DownloadTerminalCause.SHUTDOWN
    assert all(sec <= 0.1 for sec in slices), f"unsliced sleep: {slices}"
    assert len(slices) <= 10                       # exits within the same period
    assert ft.cancelled == []                      # still never cancels the task


# -- locator whitelist -----------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "http://evil/x.torrent", "file:///etc/passwd", "BV12345:1",  # bvid too short
    "BV1234567890", "BV1234567890:x", "; rm -rf /", "",
])
def test_bad_locator_never_executes(qapp, tmp_path, bad):
    spawned = []

    def popen(argv, **kw):
        spawned.append(argv)
        return FakeProc([])

    ft = FakeTorrent([_downloading(0.1)])
    event = _worker(tmp_path, locator=bad, torrent=ft, popen=popen).execute()
    assert event.error is not None and "BAD_LOCATOR" in event.error
    assert spawned == []
    assert ft.added == []


# -- yt-dlp argv safety -----------------------------------------------------------

def test_subfolder_uses_anime_name_not_source_release_title(qapp, tmp_path):
    # The download-organizing dir must be the anime NAME (series_title), NOT the
    # full source release title (fansub + episode + quality / [Pxx]) -- else every
    # episode of one anime lands in a different folder (the reviewer's High).
    ft = FakeTorrent([_completed(str(tmp_path / "ep1.mkv"))])
    w = _worker(
        tmp_path, torrent=ft,
        title="[LoliHouse] 无职转生 第三季 - 01 [WebRip 1080p HEVC-10bit AAC][简繁内封]",
        series_title="无职转生")
    w.execute()
    assert ft.subfolders == ["无职转生"]               # anime name, not the release title
    argv = w._ytdlp_argv("BV1234567890", 1)
    assert argv[argv.index("-P") + 1] == str(tmp_path.resolve() / "无职转生")


def test_subfolder_falls_back_to_title_when_series_name_absent(qapp, tmp_path):
    # Defensive: a worker built without a series_title (e.g. an old serialized
    # event) still groups under SOMETHING, not the download-dir root.
    w = _worker(tmp_path, title="某番", series_title="")
    assert w._ytdlp_argv("BV1", 1)[w._ytdlp_argv("BV1", 1).index("-P") + 1] == \
        str(tmp_path.resolve() / "某番")


def test_ytdlp_cli_contract_is_explicit_and_resumable(qapp, tmp_path):
    out = tmp_path / "episode.mp4"
    out.write_bytes(b"media")
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return FakeProc([str(out)])

    event = _worker(
        tmp_path, locator="BV1234567890:3", popen=popen,
        source_timeout_seconds=23.0,
    ).execute()

    assert event.error is None
    [(argv, popen_kw)] = spawned
    assert argv[1:4] == ["-m", "yt_dlp", "--ignore-config"]
    assert argv[-1] == "https://www.bilibili.com/video/BV1234567890"
    i = argv.index("-P")
    # output pinned AND grouped by anime NAME (series_title), not the release title
    assert argv[i + 1] == str(tmp_path.resolve() / "无职转生")
    j = argv.index("-I")
    assert argv[j + 1] == "3"                          # the requested part only
    assert "--progress" in argv                         # --print otherwise hides it
    template = argv[argv.index("--progress-template") + 1]
    assert template == (
        'download:SPICA:{"format_id":%(info.format_id|null)j,'
        '"progress":%(progress)j}')
    assert argv[argv.index("--progress-delta") + 1] == "1"
    assert float(argv[argv.index("--socket-timeout") + 1]) == 23.0
    assert argv[argv.index("--retries") + 1] == "1"
    assert argv[argv.index("--retry-sleep") + 1] == "http:1"
    assert "--part" in argv and "--continue" in argv
    assert "--no-part" not in argv and "--no-continue" not in argv
    assert "--throttled-rate" not in argv
    assert popen_kw["shell"] is False
    assert "--cookies" not in argv                     # no cookies file -> absent


def test_ytdlp_cli_contract_forces_utf8_output(qapp, tmp_path):
    out = tmp_path / "episode.mp4"
    out.write_bytes(b"media")
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return FakeProc([str(out)])

    event = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
    ).execute()

    assert event.error is None
    [(argv, _popen_kw)] = spawned
    assert argv[1:6] == [
        "-m", "yt_dlp", "--ignore-config", "--encoding", "utf-8",
    ]


def test_ytdlp_utf8_output_survives_non_utf8_child_stdio(qapp, tmp_path):
    """Exercise the real yt-dlp writer and the worker's text-pipe decoder.

    The injected process replaces only the network extraction command with an
    offline info-json run.  It preserves the encoding option from the worker's
    real argv and forces the child stdio default to CP936, matching a non-UTF-8
    Windows codepage.  The separate CLI-contract test above proves that the
    production command actually supplies that option.
    """
    out = tmp_path / "无职转生 第三季 [P01].mkv"
    out.write_bytes(b"finished-media")
    info_file = tmp_path / "offline-info.json"
    info_file.write_text(json.dumps({
        "id": "encoding-check",
        "title": str(out),
        "ext": "mkv",
        "webpage_url": "https://example.invalid/encoding-check",
        "url": "https://example.invalid/episode.mkv",
        "extractor": "generic",
    }, ensure_ascii=False), encoding="utf-8")
    worker_commands = []

    def popen(worker_argv, **kwargs):
        worker_commands.append(list(worker_argv))
        encoding_args = []
        if "--encoding" in worker_argv:
            index = worker_argv.index("--encoding")
            encoding_args = list(worker_argv[index:index + 2])
        offline_argv = [
            sys.executable, "-m", "yt_dlp", "--ignore-config",
            *encoding_args,
            "--simulate", "--load-info-json", str(info_file),
            "--print", "video:%(title)s",
        ]
        child_env = dict(os.environ)
        child_env["PYTHONIOENCODING"] = "cp936"
        return subprocess.Popen(offline_argv, env=child_env, **kwargs)

    event = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
    ).execute()

    assert len(worker_commands) == 1
    assert event.error is None
    assert event.save_path == str(out.resolve())


def test_ytdlp_low_speed_reconnects_then_second_attempt_succeeds(qapp, tmp_path):
    clock = ManualClock()
    out = tmp_path / "episode.mp4"
    out.write_bytes(b"finished-media")
    part = tmp_path / "episode.f100026.mp4.part"
    part.write_bytes(b"partial-media")
    first = FakeProc(_timed_lines(clock, [
        _ytdlp_progress(downloaded=0),
        _ytdlp_progress(downloaded=256 * 1024 * 10),
        _ytdlp_progress(downloaded=256 * 1024 * 20),
    ]), rc=1)
    second = FakeProc([
        _ytdlp_progress(downloaded=10 * 1024 * 1024),
        str(out),
    ])
    procs = [first, second]
    spawned = []

    def popen(argv, **kw):
        spawned.append((list(argv), kw))
        return procs[len(spawned) - 1]

    event = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
        clock=clock.now, sleep=clock.sleep,
    ).execute()

    assert event.error is None
    assert event.save_path == str(out.resolve())
    assert event.elapsed_seconds is not None
    assert event.elapsed_seconds >= 20  # starts before attempt 1, not attempt 2
    assert len(spawned) == 2
    assert spawned[0][0] == spawned[1][0]
    assert first.terminated == 1
    assert first.killed == 0
    assert part.exists()


def test_ytdlp_socket_timeout_restarts_the_whole_extractor(qapp, tmp_path):
    out = tmp_path / "episode.mp4"
    out.write_bytes(b"finished-media")
    procs = [
        FakeProc(["ERROR: [download] Read timed out."], rc=1),
        FakeProc([str(out)]),
    ]
    spawned = []

    def popen(argv, **kw):
        spawned.append((list(argv), kw))
        return procs[len(spawned) - 1]

    worker = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
    )
    reconnects = []
    worker.reconnecting.connect(
        lambda rid, used, maximum, reason:
        reconnects.append((rid, used, maximum, reason)))

    event = worker.execute()

    assert event.error is None
    assert event.save_path == str(out.resolve())
    assert len(spawned) == 2
    assert spawned[0][0] == spawned[1][0]
    assert reconnects == [("REQ", 1, 2, "network")]


@pytest.mark.parametrize("first_error", [
    "ERROR: [download] Got error: [WinError 10053] connection aborted",
    "ERROR: [download] Got error: [WinError 10054] connection closed",
    "ERROR: [download] Got error: [WinError 10060] connection attempt failed",
    "ERROR: [download] Got error: [WinError 10061] target refused it",
    "ERROR: [download] Got error: [WinError 11001] no such host",
    "ERROR: [download] Got error: [Errno 11001] getaddrinfo failed",
    ("ERROR: [BiliBiliDynamic] 123456789: Unable to download JSON "
     "metadata: NameResolutionError('temporary DNS failure') "
     "(caused by ProxyError('proxy lookup failed'))"),
    ("ERROR: [youtube] BaW_jenozKc: Unable to download API page: "
     "NameResolutionError('temporary DNS failure') "
     "(caused by ProxyError('proxy lookup failed'))"),
    ("ERROR: A network error has occurred. "
     "(caused by <IncompleteRead: 18 bytes read, 982 more expected>)"),
    "ERROR: [download] Got error: HTTP Error 403: Forbidden",
    "ERROR: [download] Got error: HTTP Error 404: Not Found",
    ("ERROR: [download] Got error: HTTP Error 416: "
     "Requested Range Not Satisfiable"),
    ("ERROR: [SSL: UNEXPECTED_EOF_WHILE_READING] "
     "EOF occurred in violation of protocol"),
    "ERROR: HTTP Error 503: Service Unavailable",
])
def test_ytdlp_network_failure_restarts_extractor_and_second_attempt_succeeds(
        qapp, tmp_path, first_error):
    out = tmp_path / "episode.mp4"
    out.write_bytes(b"finished-media")
    procs = [FakeProc([first_error], rc=1), FakeProc([str(out)])]
    spawned = []

    def popen(argv, **kw):
        spawned.append((list(argv), kw))
        return procs[len(spawned) - 1]

    worker = _worker_with_popen_only(tmp_path, popen)
    reconnects = []
    worker.reconnecting.connect(
        lambda rid, used, maximum, reason:
        reconnects.append((used, maximum, reason)))

    event = worker.execute()

    assert event.error is None
    assert event.save_path == str(out.resolve())
    assert len(spawned) == 2
    assert reconnects == [(1, 2, "network")]


@pytest.mark.parametrize(("first_error", "expected_error"), [
    ("ERROR: 该视频为充电专属视频", "充电"),
    ("ERROR: This video is for premium members only; login required; "
     "[WinError 10054] connection closed", "大会员专属"),
    ("ERROR: Login required; getaddrinfo failed", "需要登录"),
    ("ERROR: cookies expired; connection reset", "需要登录"),
    ("ERROR: This video is unavailable; Unable to download API page", "不可用"),
    ("ERROR: This video may be deleted or geo-restricted", "不可用"),
    ("ERROR: No space left on device after connection reset", "本地保存或合并失败"),
    ("ERROR: ffmpeg postprocessing failed after IncompleteRead", "本地保存或合并失败"),
    ("ERROR: [Errno 13] Permission denied: '/home/account/anime.part'; "
     "getaddrinfo failed",
     "本地保存或合并失败"),
])
def test_ytdlp_terminal_failure_does_not_restart_extractor(
        qapp, tmp_path, first_error, expected_error):
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return FakeProc([first_error], rc=1)

    worker = _worker_with_popen_only(tmp_path, popen)
    reconnects = []
    worker.reconnecting.connect(
        lambda rid, used, maximum, reason:
        reconnects.append((used, maximum, reason)))

    event = worker.execute()

    assert event.error is not None and expected_error in event.error
    assert len(spawned) == 1
    assert reconnects == []


def test_ytdlp_unknown_failure_returns_generic_error_without_retry(
        qapp, tmp_path):
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return FakeProc(["ERROR: some random failure"], rc=1)

    worker = _worker_with_popen_only(tmp_path, popen)
    reconnects = []
    worker.reconnecting.connect(
        lambda rid, used, maximum, reason:
        reconnects.append((used, maximum, reason)))

    event = worker.execute()

    assert event.error is not None
    assert event.error.startswith("yt-dlp 下载失败：")
    assert "some random failure" in event.error
    assert len(spawned) == 1
    assert reconnects == []


def test_ytdlp_low_speed_and_network_share_two_reconnects(qapp, tmp_path):
    clock = ManualClock()
    procs = [
        FakeProc(["ERROR: [download] Read timed out."], rc=1),
        FakeProc(_timed_lines(clock, [
            _ytdlp_progress(downloaded=0),
            _ytdlp_progress(downloaded=256 * 1024 * 10),
            _ytdlp_progress(downloaded=256 * 1024 * 20),
        ]), rc=1),
        FakeProc(["ERROR: HTTP Error 503: Service Unavailable"], rc=1),
    ]
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return procs[len(spawned) - 1]

    worker = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
        clock=clock.now, sleep=clock.sleep,
    )
    reconnects = []
    worker.reconnecting.connect(
        lambda rid, used, maximum, reason:
        reconnects.append((used, maximum, reason)))

    event = worker.execute()

    assert event.error is not None
    assert len(spawned) == 3
    assert reconnects == [(1, 2, "network"), (2, 2, "low_speed")]


def test_ytdlp_third_low_speed_attempt_continues_degraded(qapp, tmp_path):
    clock = ManualClock()
    out = tmp_path / "episode.mp4"
    out.write_bytes(b"finished-media")

    def slow_lines(*, final_path=None):
        lines = [
            _ytdlp_progress(downloaded=0),
            _ytdlp_progress(downloaded=256 * 1024 * 10),
            _ytdlp_progress(downloaded=256 * 1024 * 20),
        ]
        if final_path is not None:
            lines.append(str(final_path))
        return lines

    procs = [
        FakeProc(_timed_lines(clock, slow_lines()), rc=1),
        FakeProc(_timed_lines(clock, slow_lines()), rc=1),
        FakeProc(_timed_lines(clock, slow_lines(final_path=out))),
    ]
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return procs[len(spawned) - 1]

    worker = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
        clock=clock.now, sleep=clock.sleep,
    )
    degraded = []
    worker.degraded.connect(degraded.append)

    event = worker.execute()

    assert event.error is None
    assert event.save_path == str(out.resolve())
    assert len(spawned) == 3
    assert degraded == ["REQ"]
    assert procs[2].terminated == 0


def test_ytdlp_bad_progress_json_does_not_restart(qapp, tmp_path):
    out = tmp_path / "episode.mp4"
    out.write_bytes(b"finished-media")
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return FakeProc(["SPICA:{not-json", str(out)])

    event = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
    ).execute()

    assert event.error is None
    assert len(spawned) == 1


@pytest.mark.parametrize("bad_line", [
    "SPICA:{not-json",
    "SPICA:" + json.dumps({"progress": []}),
    _ytdlp_progress(downloaded=str(256 * 1024 * 15)),
    _ytdlp_progress(downloaded=float("inf")),
    _ytdlp_progress(downloaded=-1),
    _ytdlp_progress(
        downloaded=256 * 1024 * 15, total=str(20 * 1024 * 1024)),
    _ytdlp_progress(
        downloaded=256 * 1024 * 15, total=float(20 * 1024 * 1024)),
    _ytdlp_progress(downloaded=256 * 1024 * 15, total=float("inf")),
    _ytdlp_progress(downloaded=256 * 1024 * 15, total=-1),
    _ytdlp_progress(downloaded=256 * 1024 * 15, total=True),
    _ytdlp_progress(downloaded=256 * 1024 * 15, format_id=100026),
    _ytdlp_progress(downloaded=256 * 1024 * 15, tmpfilename=["bad"]),
], ids=[
    "bad-json", "bad-progress-type", "downloaded-string",
    "downloaded-non-finite", "downloaded-negative", "total-string",
    "total-float", "total-non-finite", "total-negative", "total-bool",
    "format-id-type", "tmpfilename-type",
])
def test_ytdlp_corrupt_progress_resets_the_low_speed_window(
        qapp, tmp_path, bad_line):
    clock = ManualClock()
    out = tmp_path / "episode.mp4"
    out.write_bytes(b"finished-media")
    first = FakeProc(_timed_lines(clock, [
        _ytdlp_progress(downloaded=0),
        _ytdlp_progress(downloaded=256 * 1024 * 10),
        bad_line,
        _ytdlp_progress(downloaded=256 * 1024 * 20),
        str(out),
    ]))
    procs = [first, FakeProc([str(out)])]
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return procs[len(spawned) - 1]

    worker = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
        clock=clock.now, sleep=clock.sleep,
    )
    reconnects = []
    worker.reconnecting.connect(
        lambda rid, used, maximum, reason: reconnects.append(reason))

    event = worker.execute()

    assert event.error is None
    assert event.save_path == str(out.resolve())
    assert len(spawned) == 1
    assert reconnects == []
    assert first.terminated == 0


def test_ytdlp_regular_log_keeps_the_low_speed_window(qapp, tmp_path):
    clock = ManualClock()
    out = tmp_path / "episode.mp4"
    out.write_bytes(b"finished-media")
    first = FakeProc(_timed_lines(clock, [
        _ytdlp_progress(downloaded=0),
        _ytdlp_progress(downloaded=256 * 1024 * 10),
        "[download] Destination: episode.f100026.mp4.part",
        _ytdlp_progress(downloaded=256 * 1024 * 20),
        str(out),
    ]), rc=1)
    procs = [first, FakeProc([str(out)])]
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return procs[len(spawned) - 1]

    worker = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
        clock=clock.now, sleep=clock.sleep,
    )
    reconnects = []
    worker.reconnecting.connect(
        lambda rid, used, maximum, reason: reconnects.append(reason))

    event = worker.execute()

    assert event.error is None
    assert len(spawned) == 2
    assert reconnects == ["low_speed"]
    assert first.terminated == 1


@pytest.mark.parametrize("unknown_or_zero_total", [None, 0])
def test_ytdlp_unknown_or_zero_total_keeps_the_low_speed_window(
        qapp, tmp_path, unknown_or_zero_total):
    clock = ManualClock()
    out = tmp_path / "episode.mp4"
    out.write_bytes(b"finished-media")
    first = FakeProc(_timed_lines(clock, [
        _ytdlp_progress(downloaded=0, total=unknown_or_zero_total),
        _ytdlp_progress(
            downloaded=256 * 1024 * 10, total=unknown_or_zero_total),
        _ytdlp_progress(
            downloaded=256 * 1024 * 20, total=unknown_or_zero_total),
    ]), rc=1)
    procs = [first, FakeProc([str(out)])]
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return procs[len(spawned) - 1]

    event = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
        clock=clock.now, sleep=clock.sleep,
    ).execute()

    assert event.error is None
    assert len(spawned) == 2
    assert first.terminated == 1


@pytest.mark.parametrize("bad_payload", [
    {"progress": None},
    {"progress": []},
    {"progress": "downloading"},
    {"progress": {"status": "downloading", "downloaded_bytes": "wat"}},
    {"progress": {"status": "downloading", "downloaded_bytes": float("inf")}},
])
def test_ytdlp_bad_progress_shape_is_ignored(qapp, tmp_path, bad_payload):
    out = tmp_path / "episode.mp4"
    out.write_bytes(b"finished-media")
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return FakeProc(["SPICA:" + json.dumps(bad_payload), str(out)])

    event = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
    ).execute()

    assert event.error is None
    assert len(spawned) == 1


def test_ytdlp_does_not_restart_completed_attempt_for_queued_slow_samples(
        qapp, tmp_path):
    clock = ManualClock()
    out = tmp_path / "episode.mp4"
    out.write_bytes(b"finished-media")
    proc = FakeProc(_timed_lines(clock, [
        _ytdlp_progress(downloaded=0),
        _ytdlp_progress(downloaded=256 * 1024 * 10),
        _ytdlp_progress(downloaded=256 * 1024 * 20),
        str(out),
    ]))
    proc._done = True
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return proc

    event = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
        clock=clock.now, sleep=clock.sleep,
    ).execute()

    assert event.error is None
    assert event.save_path == str(out.resolve())
    assert len(spawned) == 1
    assert proc.terminated == 0


def test_ytdlp_stdout_has_exactly_one_reader_thread(qapp, tmp_path):
    out = tmp_path / "episode.mp4"
    out.write_bytes(b"finished-media")
    stdout = BlockingStdout([str(out)], finish=True)
    proc = ControlledProc(stdout)
    caller_thread = threading.get_ident()

    event = _worker(
        tmp_path,
        locator="BV1234567890:1",
        popen=lambda argv, **kw: proc,
    ).execute()

    assert event.error is None
    assert len(set(stdout.reader_thread_ids)) == 1
    assert caller_thread not in stdout.reader_thread_ids


def test_ytdlp_waits_for_eof_tail_after_process_exit(
        qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(anime_worker_module, "_READER_JOIN_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(anime_worker_module, "_YTDLP_READER_POLL_SECONDS", 0.005)
    stdout = BlockingStdout(close_unblocks=False)
    proc = ControlledProc(stdout, rc=1, exited=True)
    worker = _worker(
        tmp_path,
        locator="BV1234567890:1",
        popen=lambda argv, **kw: proc,
    )
    result = {}

    thread = threading.Thread(
        target=lambda: result.setdefault("event", worker.execute()))
    thread.start()
    assert stdout.read_started.wait(1)
    time.sleep(0.05)
    assert thread.is_alive(), "worker decided before the reader delivered EOF"
    assert stdout.close_calls == 0, "natural exit must not close a draining pipe"
    stdout.feed("ERROR: This video requires login cookies")
    assert stdout.line_read.wait(1)
    assert thread.is_alive(), "worker decided from the tail before the EOF sentinel"
    stdout.finish()
    thread.join(2)

    assert not thread.is_alive()
    assert "登录" in result["event"].error


def test_ytdlp_terminate_timeout_escalates_to_kill(
        qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(
        anime_worker_module, "_PROCESS_TERMINATE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        anime_worker_module, "_PROCESS_KILL_TIMEOUT_SECONDS", 0.05)
    clock = ManualClock()
    stdout = BlockingStdout([
        _ytdlp_progress(downloaded=0),
        _ytdlp_progress(downloaded=256 * 1024 * 10),
        _ytdlp_progress(downloaded=256 * 1024 * 20),
    ], before_line=lambda: clock.advance(10.0))
    proc = ControlledProc(stdout, rc=1, terminate_exits=False)
    worker = _worker(
        tmp_path,
        locator="BV1234567890:1",
        popen=lambda argv, **kw: proc,
        clock=clock.now,
        sleep=clock.sleep,
    )

    result = worker._run_ytdlp_attempt(
        worker._ytdlp_argv("BV1234567890", 1),
        stop_on_low_speed=True,
    )

    assert result.low_speed is True
    assert proc.terminated == 1
    assert proc.killed == 1
    assert worker._proc is None


def test_ytdlp_reader_cleanup_is_bounded_when_close_does_not_unblock(
        qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(anime_worker_module, "_READER_JOIN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(anime_worker_module, "_YTDLP_READER_POLL_SECONDS", 0.01)
    clock = ManualClock()
    stdout = BlockingStdout([
        _ytdlp_progress(downloaded=0),
        _ytdlp_progress(downloaded=256 * 1024 * 10),
        _ytdlp_progress(downloaded=256 * 1024 * 20),
    ], close_unblocks=False, before_line=lambda: clock.advance(10.0))
    proc = ControlledProc(
        stdout,
        rc=1,
        terminate_unblocks_pipe=False,
        natural_exit_on_eof=False,
    )
    worker = _worker(
        tmp_path,
        locator="BV1234567890:1",
        popen=lambda argv, **kw: proc,
        clock=clock.now,
        sleep=clock.sleep,
    )
    result = {}
    thread = threading.Thread(
        target=lambda: result.setdefault(
            "attempt",
            worker._run_ytdlp_attempt(
                worker._ytdlp_argv("BV1234567890", 1),
                stop_on_low_speed=True,
            ),
        ))

    thread.start()
    thread.join(0.15)
    bounded = not thread.is_alive()
    stdout.finish()
    thread.join(2)

    assert bounded, "worker waited forever for a missing EOF sentinel"
    assert result["attempt"].lifecycle_error is not None
    assert worker._proc is None


def test_ytdlp_kill_timeout_returns_lifecycle_error(
        qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(
        anime_worker_module, "_PROCESS_TERMINATE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        anime_worker_module, "_PROCESS_KILL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(anime_worker_module, "_READER_JOIN_TIMEOUT_SECONDS", 0.02)
    clock = ManualClock()
    stdout = BlockingStdout([
        _ytdlp_progress(downloaded=0),
        _ytdlp_progress(downloaded=256 * 1024 * 10),
        _ytdlp_progress(downloaded=256 * 1024 * 20),
    ], before_line=lambda: clock.advance(10.0))
    proc = ControlledProc(
        stdout,
        rc=1,
        terminate_exits=False,
        kill_exits=False,
        exit_after_kills=2,
        kill_unblocks_pipe=True,
        natural_exit_on_eof=False,
    )
    worker = _worker(
        tmp_path,
        locator="BV1234567890:1",
        popen=lambda argv, **kw: proc,
        clock=clock.now,
        sleep=clock.sleep,
    )
    event = worker.execute()

    assert event.error is not None and "强制终止后仍未退出" in event.error
    assert proc.terminated >= 1
    assert proc.killed >= 2
    assert proc.reaped.wait(1)
    assert proc.wait_calls >= 3
    assert proc.poll() is not None
    assert worker._proc is None


def test_ytdlp_cancel_from_reconnect_signal_prevents_next_spawn(qapp, tmp_path):
    clock = ManualClock()
    first = FakeProc(_timed_lines(clock, [
        _ytdlp_progress(downloaded=0),
        _ytdlp_progress(downloaded=256 * 1024 * 10),
        _ytdlp_progress(downloaded=256 * 1024 * 20),
    ], step=10.0), rc=1)
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return first

    worker = _worker(
        tmp_path,
        locator="BV1234567890:1",
        popen=popen,
        clock=clock.now,
        sleep=clock.sleep,
    )
    worker.reconnecting.connect(
        lambda rid, used, maximum, reason: worker.cancel())

    event = worker.execute()
    assert event.terminal_result is DownloadTerminalResult.PRESERVED
    assert event.terminal_cause is DownloadTerminalCause.SHUTDOWN
    assert len(spawned) == 1


def test_ytdlp_attempt_cancelled_before_start_does_not_spawn(qapp, tmp_path):
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return FakeProc([])

    worker = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
    )
    worker.cancel()

    result = worker._run_ytdlp_attempt(
        worker._ytdlp_argv("BV1234567890", 1),
        stop_on_low_speed=True,
    )

    assert result.cancelled is True
    assert spawned == []


def test_ytdlp_cancel_during_popen_is_adopted_stopped_and_reaped(
        qapp, tmp_path):
    stdout = BlockingStdout()
    proc = ControlledProc(stdout)
    popen_entered = threading.Event()
    release_popen = threading.Event()
    cancel_finished = threading.Event()
    result = {}

    def popen(argv, **kw):
        del argv, kw
        popen_entered.set()
        if not release_popen.wait(2):
            raise RuntimeError("test did not release Popen")
        return proc

    worker = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
    )
    attempt_thread = threading.Thread(
        target=lambda: result.setdefault(
            "attempt",
            worker._run_ytdlp_attempt(
                worker._ytdlp_argv("BV1234567890", 1),
                stop_on_low_speed=True,
            ),
        ))
    attempt_thread.start()
    assert popen_entered.wait(1)

    cancel_thread = threading.Thread(
        target=lambda: (worker.cancel(), cancel_finished.set()))
    cancel_thread.start()
    cancel_returned_before_popen = cancel_finished.wait(0.5)
    release_popen.set()
    cancel_thread.join(1)
    attempt_thread.join(2)
    if attempt_thread.is_alive():
        proc.kill()
        stdout.finish()
        attempt_thread.join(2)

    assert cancel_returned_before_popen, "cancel blocked on the Popen handoff"
    assert not attempt_thread.is_alive()
    assert result["attempt"].cancelled is True
    assert proc.terminated >= 1
    assert proc.wait_calls >= 1
    assert proc.poll() is not None
    assert worker._proc is None


def test_ytdlp_process_wait_does_not_hold_the_ownership_lock(
        qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(
        anime_worker_module, "_PROCESS_TERMINATE_TIMEOUT_SECONDS", 0.5)
    stdout = BlockingStdout()
    proc = ControlledProc(stdout, terminate_exits=False, kill_exits=True)
    worker = _worker(
        tmp_path, locator="BV1234567890:1",
        popen=lambda argv, **kw: proc,
    )
    result = {}
    attempt_thread = threading.Thread(
        target=lambda: result.setdefault(
            "attempt",
            worker._run_ytdlp_attempt(
                worker._ytdlp_argv("BV1234567890", 1),
                stop_on_low_speed=True,
            ),
        ))
    attempt_thread.start()
    assert stdout.read_started.wait(1)
    worker.cancel()
    assert proc.wait_started.wait(1)

    force_kill_finished = threading.Event()
    force_kill_thread = threading.Thread(
        target=lambda: (worker.force_kill(), force_kill_finished.set()))
    force_kill_thread.start()
    force_kill_returned_while_waiting = force_kill_finished.wait(0.2)
    force_kill_thread.join(1)
    attempt_thread.join(2)
    if attempt_thread.is_alive():
        proc.kill()
        stdout.finish()
        attempt_thread.join(2)

    assert force_kill_returned_while_waiting
    assert not attempt_thread.is_alive()
    assert result["attempt"].cancelled is True
    assert proc.killed >= 1
    assert proc.wait_calls >= 1
    assert worker._proc is None


def test_ytdlp_cookie_value_never_in_argv(qapp, tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape\nSESSDATA\tSECRETTOKEN123\n", encoding="utf-8")
    w = _worker(tmp_path, locator="BV1234567890:1",
                cookies_file=str(cookie_file))
    argv = w._ytdlp_argv("BV1234567890", 1)
    k = argv.index("--cookies")
    assert argv[k + 1] == str(cookie_file)             # path only
    assert all("SECRETTOKEN123" not in a for a in argv)


def test_ytdlp_missing_cookie_file_omits_flag(qapp, tmp_path):
    w = _worker(tmp_path, locator="BV1234567890:1",
                cookies_file=str(tmp_path / "nope.txt"))
    assert "--cookies" not in w._ytdlp_argv("BV1234567890", 1)


# -- yt-dlp lane -------------------------------------------------------------------

def test_ytdlp_success_parses_progress_and_validates_path(qapp, tmp_path):
    out = tmp_path / "无职转生 第三季 [P01].mkv"
    out.write_bytes(b"x" * 16)
    spawned = {}

    clock = ManualClock()

    def popen(argv, **kw):
        spawned["argv"] = argv
        spawned["kw"] = kw
        return FakeProc(_timed_lines(clock, [
            _ytdlp_progress(downloaded=423, total=1000),
            str(out),
        ], step=1.0))

    w = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
        clock=clock.now, sleep=clock.sleep)
    prog = []
    w.progress.connect(lambda rid, p, ph: prog.append(round(p, 3)))
    event = w.execute()
    assert spawned["kw"]["shell"] is False
    assert event.error is None
    assert event.save_path == str(out.resolve())
    assert event.elapsed_seconds is not None and event.elapsed_seconds > 0
    assert prog[0] == 0.423 and prog[-1] == 1.0


def test_ytdlp_shutdown_claimed_at_completion_preserves_terminal_owner(
        qapp, tmp_path):
    out = tmp_path / "episode.mkv"
    out.write_bytes(b"x")
    worker = _worker(
        tmp_path,
        locator="BV1234567890:1",
        popen=lambda argv, **kw: FakeProc([str(out)]),
    )
    worker.progress.connect(
        lambda _rid, progress, _phase: (
            worker.cancel() if progress == 1.0 else None))

    event = worker.execute()

    assert event.terminal_result is DownloadTerminalResult.PRESERVED
    assert event.terminal_cause is DownloadTerminalCause.SHUTDOWN


def test_ytdlp_manual_claimed_at_completion_keeps_file_without_normal_cause(
        qapp, tmp_path):
    out = tmp_path / "episode.mkv"
    out.write_bytes(b"x")
    worker = _worker(
        tmp_path,
        locator="BV1234567890:1",
        popen=lambda argv, **kw: FakeProc([str(out)]),
    )
    worker.progress.connect(
        lambda _rid, progress, _phase: (
            worker.request_download_cancel() if progress == 1.0 else None))

    event = worker.execute()

    assert event.terminal_result is DownloadTerminalResult.COMPLETED
    assert event.terminal_cause is DownloadTerminalCause.MANUAL
    assert event.save_path == str(out.resolve())


def test_ytdlp_output_outside_download_dir_rejected(qapp, tmp_path):
    dl = tmp_path / "dl"
    dl.mkdir()
    outside = tmp_path / "evil.mkv"
    outside.write_bytes(b"x")
    w = _worker(dl, locator="BV1234567890:1",
                popen=lambda argv, **kw: FakeProc([str(outside)]))
    event = w.execute()
    assert event.error is not None and "可信" in event.error


def test_ytdlp_output_bad_extension_rejected(qapp, tmp_path):
    bad = tmp_path / "evil.desktop"
    bad.write_bytes(b"x")
    w = _worker(tmp_path, locator="BV1234567890:1",
                popen=lambda argv, **kw: FakeProc([str(bad)]))
    event = w.execute()
    assert event.error is not None and "可信" in event.error


def test_ytdlp_nonzero_exit_classified(qapp, tmp_path):
    w = _worker(tmp_path, locator="BV1234567890:1",
                popen=lambda argv, **kw: FakeProc(
                    ["ERROR: This video requires login cookies"], rc=1))
    event = w.execute()
    assert event.error is not None and "登录" in event.error


def test_ytdlp_cancel_terminates_and_keeps_part(qapp, tmp_path):
    part = tmp_path / "ep.mkv.part"
    part.write_bytes(b"partial")
    procs = []
    w = _worker(tmp_path, locator="BV1234567890:1")

    def lines():
        yield "[download]  10.0% of 300MB\n"
        w.cancel()                          # user quits mid-download
        yield "[download]  20.0% of 300MB\n"

    def popen(argv, **kw):
        p = FakeProc([])
        p.stdout = lines()
        procs.append(p)
        return p

    w._popen = popen
    event = w.execute()
    assert event.terminal_result is DownloadTerminalResult.PRESERVED
    assert event.terminal_cause is DownloadTerminalCause.SHUTDOWN
    assert procs[0].terminated == 1         # subprocess terminated...
    assert part.exists()                    # ...and the .part survives


def test_ytdlp_manual_cancel_emits_terminal_result_and_keeps_part(
        qapp, tmp_path):
    target = tmp_path / "无职转生"
    target.mkdir()
    part = target / "episode.mp4.part"
    part.write_bytes(b"partial")
    stdout = BlockingStdout()
    proc = ControlledProc(stdout, rc=1)
    worker = _worker(
        tmp_path, locator="BV1234567890:1",
        popen=lambda argv, **kw: proc)
    result = {}
    thread = threading.Thread(
        target=lambda: result.setdefault("event", worker.execute()))

    thread.start()
    assert stdout.read_started.wait(1)
    assert worker.request_download_cancel() is True
    thread.join(2)

    assert not thread.is_alive()
    event = result["event"]
    assert event is not None and event.error is not None
    assert event.terminal_result is DownloadTerminalResult.CANCELLED
    assert event.terminal_cause is DownloadTerminalCause.MANUAL
    assert "临时文件已保留" in event.error
    assert proc.terminated >= 1
    assert proc.poll() is not None
    assert part.read_bytes() == b"partial"


# -- boundary: the worker holds no write authority ---------------------------------

def test_worker_has_no_host_or_library_hooks(qapp, tmp_path):
    w = _worker(tmp_path, torrent=FakeTorrent([_downloading(0.1)]))
    for forbidden in ("_library", "_register_download", "_mark_played",
                      "_host", "_play_file"):
        assert not hasattr(w, forbidden)
