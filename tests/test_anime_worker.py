"""Phase 4: AnimeDownloadWorker -- qbt polling / yt-dlp argv safety / lifecycle.

The worker's core loop is exercised SYNCHRONOUSLY (``execute()``; injectable
popen/clock/sleep) -- no thread is started, no network, no subprocess.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from spica.anime.models import DownloadStatus  # noqa: E402
from spica.ports.torrent_client import TorrentClientError  # noqa: E402
import ui.workers.anime_worker as anime_worker_module  # noqa: E402
from ui.workers.anime_worker import (  # noqa: E402
    AnimeDownloadWorker,
    _YtDlpFailureKind,
    _analyze_ytdlp_failure,
    _classify_ytdlp_failure,
)

MAGNET = "magnet:?xt=urn:btih:" + "a" * 40


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _clock(step: float = 10.0):
    state = {"t": 0.0}

    def tick() -> float:
        state["t"] += step
        return state["t"]

    return tick


class FakeTorrent:
    def __init__(self, statuses):
        self.statuses = list(statuses)   # DownloadStatus | TorrentClientError
        self.added: list[str] = []
        self.subfolders: list[str | None] = []
        self.cancelled: list[str] = []

    def add_magnet(self, magnet: str, *, subfolder: str | None = None) -> str:
        self.added.append(magnet)
        self.subfolders.append(subfolder)
        return "a" * 40

    def status(self, task_id: str) -> DownloadStatus:
        item = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if isinstance(item, TorrentClientError):
            raise item
        return item

    def cancel(self, task_id: str) -> None:
        self.cancelled.append(task_id)


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
                 close_unblocks=True):
        self._items = queue.Queue()
        for line in lines:
            self._items.put(f"{line}\n")
        self._finished = False
        self._first_read_gate = first_read_gate
        self._first_read = True
        self._close_unblocks = close_unblocks
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
            resume_task_id=None, cookies_file="", stall_timeout_minutes=30.0,
            title="无职转生 第三季", series_title="无职转生", **kw):
    return AnimeDownloadWorker(
        request_id="REQ", episode_key="无职转生|s3|e1", title=title,
        series_title=series_title,
        locator=locator, torrent=torrent, download_dir=str(tmp_path),
        poll_seconds=1.0, stall_timeout_minutes=stall_timeout_minutes,
        cookies_file=cookies_file, resume_task_id=resume_task_id,
        popen=popen, clock=_clock(), sleep=lambda s: None, **kw)


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


def test_magnet_stall_signal_fires_once(qapp, tmp_path):
    same = [_downloading(0.5)] * 10 + [_completed(str(tmp_path / "e.mkv"))]
    w = _worker(tmp_path, torrent=FakeTorrent(same), stall_timeout_minutes=1.0)
    stalls = []
    w.stalled.connect(lambda rid, m: stalls.append(m))
    event = w.execute()
    assert event.error is None
    assert len(stalls) == 1                            # once per dry spell


def test_magnet_cancel_stops_polling_only(qapp, tmp_path):
    # P1-9: qbt is an external resident service -- cancel never touches the task
    ft = FakeTorrent([_downloading(0.2)])
    w = _worker(tmp_path, torrent=ft)
    w.cancel()
    assert w.execute() is None                         # nothing emitted
    assert ft.cancelled == []                          # task untouched


def test_resume_mode_polls_existing_task_elapsed_none(qapp, tmp_path):
    # restart reconcile (P1-9): unknown age -> elapsed None (=> ANNOUNCE only)
    ft = FakeTorrent([_completed(str(tmp_path / "ep1.mkv"))])
    w = _worker(tmp_path, locator="", torrent=ft, resume_task_id="b" * 40)
    event = w.execute()
    assert ft.added == []                              # never re-adds
    assert event.error is None
    assert event.elapsed_seconds is None


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


def test_magnet_unreachable_stays_long_transient(qapp, tmp_path):
    # a LONG unreachable spell (service restarting) must never fail the task
    ft = FakeTorrent([TorrentClientError("UNREACHABLE", "down")] * 40
                     + [_completed(str(tmp_path / "ep1.mkv"))])
    event = _worker(tmp_path, torrent=ft).execute()
    assert event.error is None


def test_magnet_unreachable_beyond_stall_emits_stall_once_then_recovers(
        qapp, tmp_path):
    # P2-2: a qbt outage longer than stall_timeout must surface a SINGLE stall
    # (no permanent silent 0% busy), keep reconnecting, and still complete on
    # recovery -- never a failure verdict.
    ft = FakeTorrent([TorrentClientError("UNREACHABLE", "down")] * 10
                     + [_completed(str(tmp_path / "ep1.mkv"))])
    w = _worker(tmp_path, torrent=ft, stall_timeout_minutes=1.0)
    stalls = []
    w.stalled.connect(lambda rid, m: stalls.append(m))
    event = w.execute()
    assert len(stalls) == 1                         # once per dry spell, not spam
    assert event.error is None                      # reconnect, never fail (P1-10)
    assert event.save_path == str(tmp_path / "ep1.mkv")


def test_magnet_unreachable_stall_resets_after_progress(qapp, tmp_path):
    # once progress moves again the stall latch resets, so a SECOND dry spell can
    # re-notify -- an outage, recovery, then a fresh stall fires a second stall.
    ft = FakeTorrent(
        [TorrentClientError("UNREACHABLE", "down")] * 10   # spell 1 -> stall
        + [_downloading(0.5)]                              # progress -> reset
        + [TorrentClientError("UNREACHABLE", "down")] * 10  # spell 2 -> stall
        + [_completed(str(tmp_path / "ep1.mkv"))])
    w = _worker(tmp_path, torrent=ft, stall_timeout_minutes=1.0)
    stalls = []
    w.stalled.connect(lambda rid, m: stalls.append(m))
    event = w.execute()
    assert len(stalls) == 2                          # reset let the 2nd fire
    assert event.error is None


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
    assert w.execute() is None                     # prompt exit, nothing emitted
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
    out = tmp_path / "episode.mp4"
    out.write_bytes(b"finished-media")
    part = tmp_path / "episode.f100026.mp4.part"
    part.write_bytes(b"partial-media")
    first = FakeProc([
        _ytdlp_progress(downloaded=0),
        _ytdlp_progress(downloaded=256 * 1024 * 10),
        _ytdlp_progress(downloaded=256 * 1024 * 20),
    ], rc=1)
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
    ).execute()

    assert event.error is None
    assert event.save_path == str(out.resolve())
    assert event.elapsed_seconds is not None
    assert event.elapsed_seconds >= 50  # starts before attempt 1, not attempt 2
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

    worker = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
    )
    reconnects = []
    worker.reconnecting.connect(
        lambda rid, used, maximum, reason:
        reconnects.append((used, maximum, reason)))

    event = worker.execute()

    assert event.error is None
    assert event.save_path == str(out.resolve())
    assert len(spawned) == 2
    assert reconnects == [(1, 2, "network")]


def test_ytdlp_terminal_auth_wins_over_network_words(qapp, tmp_path):
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return FakeProc([
            "ERROR: Login required; account request timed out while checking cookies",
        ], rc=1)

    event = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
    ).execute()

    assert event.error is not None and "登录" in event.error
    assert len(spawned) == 1


def test_ytdlp_low_speed_and_network_share_two_reconnects(qapp, tmp_path):
    procs = [
        FakeProc(["ERROR: [download] Read timed out."], rc=1),
        FakeProc([
            _ytdlp_progress(downloaded=0),
            _ytdlp_progress(downloaded=256 * 1024 * 10),
            _ytdlp_progress(downloaded=256 * 1024 * 20),
        ], rc=1),
        FakeProc(["ERROR: HTTP Error 503: Service Unavailable"], rc=1),
    ]
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return procs[len(spawned) - 1]

    worker = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
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
        FakeProc(slow_lines(), rc=1),
        FakeProc(slow_lines(), rc=1),
        FakeProc(slow_lines(final_path=out)),
    ]
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return procs[len(spawned) - 1]

    worker = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
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
    out = tmp_path / "episode.mp4"
    out.write_bytes(b"finished-media")
    first = FakeProc([
        _ytdlp_progress(downloaded=0),
        _ytdlp_progress(downloaded=256 * 1024 * 10),
        bad_line,
        _ytdlp_progress(downloaded=256 * 1024 * 20),
        str(out),
    ])
    procs = [first, FakeProc([str(out)])]
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return procs[len(spawned) - 1]

    worker = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
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
    out = tmp_path / "episode.mp4"
    out.write_bytes(b"finished-media")
    first = FakeProc([
        _ytdlp_progress(downloaded=0),
        _ytdlp_progress(downloaded=256 * 1024 * 10),
        "[download] Destination: episode.f100026.mp4.part",
        _ytdlp_progress(downloaded=256 * 1024 * 20),
        str(out),
    ], rc=1)
    procs = [first, FakeProc([str(out)])]
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return procs[len(spawned) - 1]

    worker = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
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
    out = tmp_path / "episode.mp4"
    out.write_bytes(b"finished-media")
    first = FakeProc([
        _ytdlp_progress(downloaded=0, total=unknown_or_zero_total),
        _ytdlp_progress(
            downloaded=256 * 1024 * 10, total=unknown_or_zero_total),
        _ytdlp_progress(
            downloaded=256 * 1024 * 20, total=unknown_or_zero_total),
    ], rc=1)
    procs = [first, FakeProc([str(out)])]
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return procs[len(spawned) - 1]

    event = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
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
    out = tmp_path / "episode.mp4"
    out.write_bytes(b"finished-media")
    proc = FakeProc([
        _ytdlp_progress(downloaded=0),
        _ytdlp_progress(downloaded=256 * 1024 * 10),
        _ytdlp_progress(downloaded=256 * 1024 * 20),
        str(out),
    ])
    proc._done = True
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return proc

    event = _worker(
        tmp_path, locator="BV1234567890:1", popen=popen,
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
    stdout = BlockingStdout([
        _ytdlp_progress(downloaded=0),
        _ytdlp_progress(downloaded=256 * 1024 * 10),
        _ytdlp_progress(downloaded=256 * 1024 * 20),
    ])
    proc = ControlledProc(stdout, rc=1, terminate_exits=False)
    worker = _worker(
        tmp_path,
        locator="BV1234567890:1",
        popen=lambda argv, **kw: proc,
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
    stdout = BlockingStdout([
        _ytdlp_progress(downloaded=0),
        _ytdlp_progress(downloaded=256 * 1024 * 10),
        _ytdlp_progress(downloaded=256 * 1024 * 20),
    ], close_unblocks=False)
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
    stdout = BlockingStdout([
        _ytdlp_progress(downloaded=0),
        _ytdlp_progress(downloaded=256 * 1024 * 10),
        _ytdlp_progress(downloaded=256 * 1024 * 20),
    ])
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
    first = FakeProc([
        _ytdlp_progress(downloaded=0),
        _ytdlp_progress(downloaded=256 * 1024 * 10),
        _ytdlp_progress(downloaded=256 * 1024 * 20),
    ], rc=1)
    spawned = []

    def popen(argv, **kw):
        spawned.append((argv, kw))
        return first

    worker = _worker(
        tmp_path,
        locator="BV1234567890:1",
        popen=popen,
    )
    worker.reconnecting.connect(
        lambda rid, used, maximum, reason: worker.cancel())

    assert worker.execute() is None
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

    def popen(argv, **kw):
        spawned["argv"] = argv
        spawned["kw"] = kw
        return FakeProc([
            _ytdlp_progress(downloaded=423, total=1000),
            str(out),
        ])

    w = _worker(tmp_path, locator="BV1234567890:1", popen=popen)
    prog = []
    w.progress.connect(lambda rid, p, ph: prog.append(round(p, 3)))
    event = w.execute()
    assert spawned["kw"]["shell"] is False
    assert event.error is None
    assert event.save_path == str(out.resolve())
    assert event.elapsed_seconds is not None and event.elapsed_seconds > 0
    assert prog[0] == 0.423 and prog[-1] == 1.0


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
    assert w.execute() is None              # cancelled -> nothing emitted
    assert procs[0].terminated == 1         # subprocess terminated...
    assert part.exists()                    # ...and the .part survives


def test_classify_ytdlp_failure_categories():
    assert "充电" in _classify_ytdlp_failure("该视频为充电专属视频")
    assert "登录" in _classify_ytdlp_failure("ERROR: login required")
    assert "cookie" in _classify_ytdlp_failure("cookies expired").lower() or \
        "登录" in _classify_ytdlp_failure("cookies expired")
    assert "yt-dlp" in _classify_ytdlp_failure("some random failure")


def test_ytdlp_failure_classification_is_typed_and_ordered():
    auth = _analyze_ytdlp_failure(
        "Login required: cookie check timed out")
    unavailable = _analyze_ytdlp_failure(
        "ERROR: This video is unavailable")
    network = _analyze_ytdlp_failure(
        "ERROR: HTTP Error 503: Service Unavailable")
    unknown = _analyze_ytdlp_failure("some random failure")

    assert auth.kind is _YtDlpFailureKind.AUTH
    assert unavailable.kind is _YtDlpFailureKind.UNAVAILABLE
    assert network.kind is _YtDlpFailureKind.NETWORK
    assert unknown.kind is _YtDlpFailureKind.OTHER


def test_ytdlp_real_entitlement_and_unavailable_wording_are_terminal():
    entitlement = _analyze_ytdlp_failure(
        "ERROR: This video is for premium members only; login required")
    unavailable = _analyze_ytdlp_failure(
        "ERROR: This video may be deleted or geo-restricted")

    assert entitlement.kind is _YtDlpFailureKind.ENTITLEMENT
    assert unavailable.kind is _YtDlpFailureKind.UNAVAILABLE


@pytest.mark.parametrize(("message", "expected"), [
    ("Premium members only; [WinError 10054] connection closed",
     _YtDlpFailureKind.ENTITLEMENT),
    ("Login required; getaddrinfo failed", _YtDlpFailureKind.AUTH),
    ("This video is unavailable; Unable to download API page",
     _YtDlpFailureKind.UNAVAILABLE),
    ("ffmpeg postprocessing failed after IncompleteRead",
     _YtDlpFailureKind.LOCAL),
])
def test_ytdlp_terminal_failures_win_over_network_markers(message, expected):
    assert _analyze_ytdlp_failure(message).kind is expected


@pytest.mark.parametrize("message", [
    ("ERROR: [download] Got error: "
     "[WinError 10053] An established connection was aborted by the software "
     "in your host machine>"),
    ("ERROR: [download] Got error: "
     "[WinError 10054] An existing connection was forcibly closed by the "
     "remote host>"),
    ("ERROR: [download] Got error: "
     "[WinError 10060] A connection attempt failed because the connected "
     "party did not properly respond>"),
    ("ERROR: [download] Got error: "
     "[WinError 10061] No connection could be made because the target machine "
     "actively refused it>"),
    ("ERROR: [download] Got error: "
     "[WinError 11001] No such host is known>"),
])
def test_ytdlp_windows_socket_errors_are_retryable_network_errors(message):
    assert _analyze_ytdlp_failure(message).kind is _YtDlpFailureKind.NETWORK


@pytest.mark.parametrize("message", [
    "ERROR: [download] Got error: HTTP Error 403: Forbidden",
    "ERROR: [download] Got error: HTTP Error 404: Not Found",
    "ERROR: [download] Got error: HTTP Error 416: Requested Range Not Satisfiable",
    "ERROR: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol",
])
def test_ytdlp_cdn_and_tls_failures_are_retryable_network_errors(message):
    assert _analyze_ytdlp_failure(message).kind is _YtDlpFailureKind.NETWORK


def test_ytdlp_local_permission_error_is_not_misclassified_as_account_auth():
    failure = _analyze_ytdlp_failure(
        "ERROR: [Errno 13] Permission denied: '/home/account/anime.part'")

    assert failure.kind is _YtDlpFailureKind.LOCAL


# -- boundary: the worker holds no write authority ---------------------------------

def test_worker_has_no_host_or_library_hooks(qapp, tmp_path):
    w = _worker(tmp_path, torrent=FakeTorrent([_downloading(0.1)]))
    for forbidden in ("_library", "_register_download", "_mark_played",
                      "_host", "_play_file"):
        assert not hasattr(w, forbidden)
