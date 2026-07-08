"""Phase 4: AnimeDownloadWorker -- qbt polling / yt-dlp argv safety / lifecycle.

The worker's core loop is exercised SYNCHRONOUSLY (``execute()``; injectable
popen/clock/sleep) -- no thread is started, no network, no subprocess.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from spica.anime.models import DownloadStatus  # noqa: E402
from spica.ports.torrent_client import TorrentClientError  # noqa: E402
from ui.workers.anime_worker import (  # noqa: E402
    AnimeDownloadWorker,
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

    def wait(self):
        self._done = True
        return self.rc

    def poll(self):
        return self.rc if self._done else None

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1


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


def test_ytdlp_argv_fixed_and_pinned(qapp, tmp_path):
    w = _worker(tmp_path, locator="BV1234567890:3")
    argv = w._ytdlp_argv("BV1234567890", 3)
    assert argv[-1] == "https://www.bilibili.com/video/BV1234567890"
    i = argv.index("-P")
    # output pinned AND grouped by anime NAME (series_title), not the release title
    assert argv[i + 1] == str(tmp_path.resolve() / "无职转生")
    j = argv.index("-I")
    assert argv[j + 1] == "3"                          # the requested part only
    assert "--no-part" not in argv                     # .part kept on terminate
    assert "--cookies" not in argv                     # no cookies file -> absent


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
        return FakeProc(["[download]  42.3% of 300MB", str(out)])

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


# -- boundary: the worker holds no write authority ---------------------------------

def test_worker_has_no_host_or_library_hooks(qapp, tmp_path):
    w = _worker(tmp_path, torrent=FakeTorrent([_downloading(0.1)]))
    for forbidden in ("_library", "_register_download", "_mark_played",
                      "_host", "_play_file"):
        assert not hasattr(w, forbidden)
