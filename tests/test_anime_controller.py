"""Phase 4: AnimeController -- event->worker, completion policy, P1-5 retry,
startup reconcile (P1-9). Host closures and workers are normally fakes; the
shutdown boundary uses the real worker with an injected process."""

from __future__ import annotations

import os
import subprocess
import threading
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from spica.core.anime_events import AnimeReadyEvent, AnimeRequestEvent  # noqa: E402
from spica.ports.media_player import MediaPlayerError  # noqa: E402
from ui.controllers.anime_controller import AnimeController  # noqa: E402
from ui.workers.anime_worker import AnimeDownloadWorker  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeWorker(QObject):
    progress = Signal(str, float, str)
    ready = Signal(object)
    stalled = Signal(str, float)
    reconnecting = Signal(str, int, int, str)
    degraded = Signal(str)
    task_started = Signal(str, str)
    finished = Signal()

    def __init__(self, **kw):
        super().__init__()
        self.kw = kw
        self.request_id = kw["request_id"]
        self.title = kw.get("title", "")
        self.started = 0
        self.cancelled = 0
        self._running = False

    def start(self):
        self.started += 1
        self._running = True

    def isRunning(self):
        return self._running

    def cancel(self):
        self.cancelled += 1
        self._running = False

    def force_kill(self):
        pass

    def wait(self, ms=0):
        self._running = False
        return True

    def finish(self):
        self._running = False
        self.finished.emit()


class _FakeTimeoutSignal:
    def __init__(self):
        self._callback = None

    def connect(self, callback):
        self._callback = callback

    def emit(self):
        if self._callback is not None:
            self._callback()


class FakeSingleShotTimer:
    """Deterministic boundary fake for Qt's one-shot clock."""

    def __init__(self):
        self.timeout = _FakeTimeoutSignal()
        self._active = False
        self.single_shot = False
        self.delays: list[int] = []

    def setSingleShot(self, value):
        self.single_shot = bool(value)

    def start(self, delay_ms):
        self._active = True
        self.delays.append(int(delay_ms))

    def stop(self):
        self._active = False

    def isActive(self):
        return self._active

    def fire(self):
        self._active = False
        self.timeout.emit()


class _BlockingProcessOutput:
    """Pipe boundary that reaches EOF only when the controlled process exits."""

    def __init__(self):
        self._closed = threading.Event()
        self.read_started = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        self.read_started.set()
        if not self._closed.wait(3):
            raise RuntimeError("controlled process output was not closed")
        raise StopIteration

    def close(self):
        self._closed.set()


class _TerminateResistantProcess:
    """Popen boundary whose terminate is ignored but kill exits and reaps."""

    def __init__(self):
        self.stdout = _BlockingProcessOutput()
        self.terminate_called = threading.Event()
        self.kill_called = threading.Event()
        self.reaped = threading.Event()
        self._exited = threading.Event()

    def poll(self):
        if not self._exited.is_set():
            return None
        # subprocess.Popen.poll() performs the non-blocking reap when exit is
        # observed, so the boundary fake records the same externally relevant
        # lifecycle outcome.
        self.reaped.set()
        return -9

    def wait(self, timeout=None):
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired("controlled-yt-dlp", timeout)
        self.reaped.set()
        return -9

    def terminate(self):
        self.terminate_called.set()

    def kill(self):
        self.kill_called.set()
        self._exited.set()
        self.stdout.close()


def _request_event(rid="REQ1", key="无职转生|s3|e1", *, payload=None):
    return AnimeRequestEvent(
        request_id=rid, query="无职转生第三季第一集", title="无职转生 第三季",
        episode_key=key, source="mikan",
        locator="magnet:?xt=urn:btih:" + "a" * 40,
        torrent_payload_b64=payload)


def _ready(rid="REQ1", key="无职转生|s3|e1", *, save_path="/dl/ep1.mkv",
           elapsed=10.0, error=None):
    return AnimeReadyEvent(request_id=rid, episode_key=key, save_path=save_path,
                           elapsed_seconds=elapsed, error=error)


class Harness:
    def __init__(self, **over):
        self.status: list[str] = []
        self.spoken: list = []
        self.speak_result = True
        self.played: list[str] = []
        self.registered: list[tuple] = []
        self.marked: list[str] = []
        self.noted: list[tuple] = []
        self.dropped: list[str] = []
        self.pending: list[dict] = []
        self.played_keys: set[str] = set()
        self.workers: list[FakeWorker] = []
        self.busy = False
        self.galgame = False

        def try_speak(req):
            self.spoken.append(req)
            return self.speak_result

        def play_file(path):
            self.played.append(path)

        def register(rid, key, path):
            self.registered.append((rid, key, path))
            return SimpleNamespace(episode_key=key, title="无职转生", season=3,
                                   episode=1, file_path=path)

        def mark_played(key):
            self.marked.append(key)
            self.played_keys.add(key)

        def factory(**kw):
            w = FakeWorker(**kw)
            self.workers.append(w)
            return w

        kwargs = dict(
            set_anime_status=self.status.append,
            request_proactive_turn=try_speak,
            play_file=play_file,
            register_download=register,
            mark_played=mark_played,
            note_task_id=lambda rid, tid: self.noted.append((rid, tid)),
            list_pending=lambda: [dict(p) for p in self.pending],
            drop_pending=self.dropped.append,
            is_played=lambda key: key in self.played_keys,
            is_busy=lambda: self.busy,
            galgame_active=lambda: self.galgame,
            anime_config=lambda: SimpleNamespace(
                auto_play_threshold_seconds=50.0, qbittorrent_poll_seconds=5.0,
                stall_timeout_minutes=30.0, ytdlp_format="fmt",
                source_timeout_seconds=23.0,
                ytdlp_min_rate_kib_per_second=321.0),
            torrent_provider=lambda: "TORRENT",
            download_dir="/dl",
            cookies_file="/repo/data/cookies.txt",
            worker_factory=factory,
        )
        kwargs.update(over)
        self.controller = AnimeController(None, **kwargs)


# -- request event -> worker (single flight, F8) --------------------------------

def test_request_event_starts_worker_and_sets_in_flight(qapp):
    h = Harness()
    h.controller.handle_anime_request_event(_request_event())
    [w] = h.workers
    assert w.started == 1
    assert w.kw["locator"].startswith("magnet:?")
    assert w.kw["torrent"] == "TORRENT"
    assert w.kw["download_dir"] == "/dl"
    assert w.kw["cookies_file"] == "/repo/data/cookies.txt"
    assert w.kw["source_timeout_seconds"] == 23.0
    assert w.kw["ytdlp_min_rate_kib_per_second"] == 321.0
    assert h.controller.in_flight_state() == {"progress": 0.0,
                                              "title": "无职转生 第三季"}
    assert any("下载中" in s for s in h.status)


def test_request_event_passes_torrent_payload_to_worker(qapp):
    h = Harness()
    h.controller.handle_anime_request_event(
        _request_event(payload="dGVzdC10b3JyZW50"))

    assert h.workers[0].kw["torrent_payload_b64"] == "dGVzdC10b3JyZW50"


def test_second_request_while_active_is_dropped(qapp):
    h = Harness()
    h.controller.handle_anime_request_event(_request_event("REQ1"))
    h.controller.handle_anime_request_event(_request_event("REQ2"))
    assert len(h.workers) == 1                     # single flight
    assert h.dropped == ["REQ2"]                   # stale pending record erased


def test_progress_updates_in_flight_state(qapp):
    h = Harness()
    h.controller.handle_anime_request_event(_request_event())
    h.workers[0].progress.emit("REQ1", 0.42, "downloading")
    state = h.controller.in_flight_state()
    assert state["progress"] == pytest.approx(0.42)
    assert any("42%" in s for s in h.status)


def test_metadata_progress_has_explicit_status(qapp):
    h = Harness()
    h.controller.handle_anime_request_event(_request_event())

    h.workers[0].progress.emit("REQ1", 0.0, "metadata")

    assert "正在获取种子元数据" in h.status[-1]
    assert "0%" not in h.status[-1]


def test_task_started_forwards_to_host_pending(qapp):
    h = Harness()
    h.controller.handle_anime_request_event(_request_event())
    h.workers[0].task_started.emit("REQ1", "a" * 40)
    assert h.noted == [("REQ1", "a" * 40)]


# -- completion: auto-play vs announce (D5 / P1-7) --------------------------------

def test_ready_fast_and_idle_auto_plays_via_host_closure(qapp):
    h = Harness()
    h.controller.handle_anime_request_event(_request_event())
    h.workers[0].ready.emit(_ready(elapsed=10.0))
    assert h.registered == [("REQ1", "无职转生|s3|e1", "/dl/ep1.mkv")]
    assert h.played == ["/dl/ep1.mkv"]             # host play closure only
    assert h.marked == ["无职转生|s3|e1"]           # played -> pointer consumed
    assert h.spoken == []                          # no announce on auto-play
    assert h.controller.in_flight_state() is None
    assert h.status[-1] == "✅ 下好了，已开始播放：无职转生"


def test_duplicate_ready_event_does_not_play_twice(qapp):
    h = Harness()
    h.controller.handle_anime_request_event(_request_event())
    ready = _ready(elapsed=30.0)

    h.workers[0].ready.emit(ready)
    h.workers[0].ready.emit(ready)

    assert h.registered == [
        ("REQ1", "无职转生|s3|e1", "/dl/ep1.mkv"),
    ]
    assert h.played == ["/dl/ep1.mkv"]
    assert h.marked == ["无职转生|s3|e1"]
    assert h.spoken == []


def test_ready_for_already_played_episode_replaces_downloading_status(qapp):
    h = Harness()
    h.played_keys.add("无职转生|s3|e1")
    h.controller.handle_anime_request_event(_request_event())

    h.workers[0].ready.emit(_ready(elapsed=30.0))

    assert h.played == []
    assert h.spoken == []
    assert h.status[-1] == "✅ 下好了，已经播放过：无职转生"


def test_ready_fast_while_busy_waits_then_auto_plays_without_asking(qapp):
    timer = FakeSingleShotTimer()
    h = Harness(completion_timer=timer)
    h.busy = True
    h.controller.handle_anime_request_event(_request_event())

    h.workers[0].ready.emit(_ready(elapsed=30.0))

    assert h.played == []
    assert h.spoken == []
    assert timer.isActive()

    h.busy = False
    timer.fire()

    assert h.played == ["/dl/ep1.mkv"]
    assert h.marked == ["无职转生|s3|e1"]
    assert h.spoken == []


def test_ready_slow_while_busy_waits_then_asks_once(qapp):
    timer = FakeSingleShotTimer()
    h = Harness(completion_timer=timer)
    h.busy = True
    h.controller.handle_anime_request_event(_request_event())

    h.workers[0].ready.emit(_ready(elapsed=51.0))

    assert h.played == []
    assert h.spoken == []
    assert timer.isActive()

    h.busy = False
    timer.fire()

    assert h.played == []
    assert len(h.spoken) == 1
    assert "第3季" in h.spoken[0].directive
    assert not timer.isActive()


def test_ready_fast_during_galgame_waits_then_auto_plays_without_asking(qapp):
    timer = FakeSingleShotTimer()
    h = Harness(completion_timer=timer)
    h.galgame = True
    h.controller.handle_anime_request_event(_request_event())
    h.workers[0].ready.emit(_ready(elapsed=30.0))

    assert h.played == []
    assert h.spoken == []
    assert timer.isActive()

    h.galgame = False
    timer.fire()

    assert h.played == ["/dl/ep1.mkv"]
    assert h.marked == ["无职转生|s3|e1"]
    assert h.spoken == []


def test_ready_slow_during_galgame_waits_then_asks_once(qapp):
    timer = FakeSingleShotTimer()
    h = Harness(completion_timer=timer)
    h.galgame = True
    h.controller.handle_anime_request_event(_request_event())
    h.workers[0].ready.emit(_ready(elapsed=51.0))

    assert h.played == []
    assert h.spoken == []
    assert timer.isActive()

    h.galgame = False
    timer.fire()

    assert h.played == []
    assert len(h.spoken) == 1
    assert not timer.isActive()


def test_multiple_completions_waiting_for_idle_never_auto_play_in_sequence(qapp):
    timer = FakeSingleShotTimer()
    h = Harness(completion_timer=timer)
    h.busy = True

    h.controller.handle_anime_request_event(
        _request_event(rid="A", key="番剧|s1|e1"))
    h.workers[0].ready.emit(
        _ready(rid="A", key="番剧|s1|e1", save_path="/dl/A.mkv"))
    h.workers[0].finish()

    h.controller.handle_anime_request_event(
        _request_event(rid="B", key="番剧|s1|e2"))
    h.workers[1].ready.emit(
        _ready(rid="B", key="番剧|s1|e2", save_path="/dl/B.mkv"))
    h.workers[1].finish()

    h.busy = False
    timer.fire()

    assert h.played == []
    assert len(h.spoken) == 1
    assert timer.isActive()

    timer.fire()

    assert h.played == []
    assert len(h.spoken) == 2
    assert not timer.isActive()


def test_ready_slow_announces_with_normalized_title(qapp):
    h = Harness()
    h.controller.handle_anime_request_event(_request_event())
    h.workers[0].ready.emit(_ready(elapsed=1000.0))
    assert h.played == []
    [req] = h.spoken
    assert "《无职转生》" in req.directive          # P1-11①: reconstructible
    assert "第3季" in req.directive
    assert "第1集" in req.directive
    assert req.source == "anime"


def test_ready_play_failure_falls_back_to_announce(qapp):
    def boom(path):
        raise MediaPlayerError("UNSAFE_PATH", "bad")
    h = Harness(play_file=boom)
    h.controller.handle_anime_request_event(_request_event())
    h.workers[0].ready.emit(_ready(elapsed=10.0))
    assert h.marked == []                          # not played -> not consumed
    assert len(h.spoken) == 1


def test_ready_error_announces_and_never_registers(qapp):
    h = Harness()
    h.controller.handle_anime_request_event(_request_event())
    h.workers[0].ready.emit(_ready(error="需要登录 B 站", save_path=None))
    assert h.registered == []
    assert h.played == []
    [req] = h.spoken
    assert "失败" in req.directive
    assert any("失败" in s for s in h.status)


def test_ready_registration_rejection_announces_not_plays(qapp):
    def reject(rid, key, path):
        raise ValueError("outside download_dir")
    h = Harness(register_download=reject)
    h.controller.handle_anime_request_event(_request_event())
    h.workers[0].ready.emit(_ready(elapsed=10.0))
    assert h.played == []
    [req] = h.spoken
    assert "没通过" in req.directive


# -- completion idle scheduling ---------------------------------------------------

def _confirmation_waiting_after_arbiter_race(h, timer):
    h.speak_result = False
    h.controller.handle_anime_request_event(_request_event())
    h.workers[0].ready.emit(_ready(elapsed=51.0))
    assert len(h.spoken) == 1                       # request lost the idle race
    assert timer.isActive()


def test_confirmation_retries_promptly_after_arbiter_race(qapp):
    timer = FakeSingleShotTimer()
    h = Harness(completion_timer=timer)
    _confirmation_waiting_after_arbiter_race(h, timer)
    assert 0 < timer.delays[-1] <= 250

    h.speak_result = True
    timer.fire()

    assert len(h.spoken) == 2
    assert not timer.isActive()


def test_pending_confirmation_stops_when_episode_was_played(qapp):
    timer = FakeSingleShotTimer()
    h = Harness(completion_timer=timer)
    _confirmation_waiting_after_arbiter_race(h, timer)
    h.played_keys.add("无职转生|s3|e1")             # 「放吧」consumed it meanwhile

    timer.fire()

    assert len(h.spoken) == 1
    assert not timer.isActive()
    assert h.status[-1] == ""


def test_user_activity_delays_but_does_not_drop_required_confirmation(qapp):
    timer = FakeSingleShotTimer()
    h = Harness(completion_timer=timer)
    _confirmation_waiting_after_arbiter_race(h, timer)
    h.busy = True

    h.controller.notify_user_activity()
    timer.fire()

    assert len(h.spoken) == 1
    assert timer.isActive()

    h.busy = False
    h.speak_result = True
    timer.fire()

    assert len(h.spoken) == 2
    assert not timer.isActive()


# -- stall (v1: informational ask only) ---------------------------------------------

def test_stall_announces_once_and_flags_chip(qapp):
    h = Harness()
    h.controller.handle_anime_request_event(_request_event())
    h.workers[0].stalled.emit("REQ1", 30.0)
    [req] = h.spoken
    assert "没有进度" in req.directive
    assert any("卡住" in s for s in h.status)
    assert h.controller._retries == {}              # no retry queue for stalls


@pytest.mark.parametrize("reason,wording", [
    ("low_speed", "当前连接过慢"),
    ("network", "当前连接中断"),
])
def test_reconnecting_updates_status_without_proactive_speech(
        qapp, reason, wording):
    h = Harness()
    h.controller.handle_anime_request_event(_request_event())

    h.workers[0].reconnecting.emit("REQ1", 1, 2, reason)

    assert wording in h.status[-1]
    assert "正在重新连接 1/2" in h.status[-1]
    assert "无职转生 第三季" in h.status[-1]
    assert "换节点" not in h.status[-1]
    assert h.spoken == []


def test_degraded_status_is_replaced_by_visible_auto_play_completion(qapp):
    h = Harness()
    h.controller.handle_anime_request_event(_request_event())
    worker = h.workers[0]

    worker.degraded.emit("REQ1")
    worker.progress.emit("REQ1", 0.42, "downloading")

    assert "连接持续较慢" in h.status[-1]
    assert "继续下载" in h.status[-1]
    assert "42%" in h.status[-1]
    assert h.spoken == []

    worker.ready.emit(_ready(elapsed=10.0))

    assert h.controller.in_flight_state() is None
    assert h.status[-1] == "✅ 下好了，已开始播放：无职转生"
    assert h.registered == [
        ("REQ1", "无职转生|s3|e1", "/dl/ep1.mkv"),
    ]
    assert h.played == ["/dl/ep1.mkv"]
    assert h.marked == ["无职转生|s3|e1"]
    assert h.spoken == []


# -- startup reconcile (P1-9) --------------------------------------------------------

def test_reconcile_registers_and_announces_never_plays(qapp):
    h = Harness()
    h.pending = [
        {"request_id": "OLD1", "episode_key": "无职转生|s3|e1",
         "title": "无职转生 第三季", "season": 3, "episode": 1,
         "task_id": "b" * 40},
        {"request_id": "OLD2", "episode_key": "别的番|s1|e2",
         "title": "别的番", "season": 1, "episode": 2, "task_id": None},
    ]
    h.controller.run_reconcile()
    assert h.dropped == ["OLD2"]                   # yt-dlp leftover: forget it
    [w] = h.workers                                # one resume worker
    assert w.kw["resume_task_id"] == "b" * 40
    assert w.started == 1
    # the resumed task completes -- unknown age: register + announce, NO play
    w.ready.emit(_ready("OLD1", save_path="/dl/ep1.mkv", elapsed=None))
    assert h.registered == [("OLD1", "无职转生|s3|e1", "/dl/ep1.mkv")]
    assert h.played == []
    assert len(h.spoken) == 1


def test_reconcile_vanished_task_drops_pending_silently(qapp):
    h = Harness()
    h.pending = [{"request_id": "OLD1", "episode_key": "无职转生|s3|e1",
                  "title": "无职转生 第三季", "season": 3, "episode": 1,
                  "task_id": "b" * 40}]
    h.controller.run_reconcile()
    [w] = h.workers
    w.ready.emit(_ready("OLD1", save_path=None, elapsed=None,
                        error="下载任务丢失（qbittorrent: TASK_NOT_FOUND）"))
    assert h.dropped == ["OLD1"]
    assert h.spoken == []                          # user removed it: stay quiet
    assert h.registered == []


# -- F1: reconcile must enter the F8 busy seam, single-flight queue ---------------

def _pending_rec(rid, key="无职转生|s3|e1", title="无职转生 第三季",
                 task_id="b" * 40):
    return {"request_id": rid, "episode_key": key, "title": title,
            "season": 3, "episode": 1, "task_id": task_id}


def test_reconcile_enters_busy_seam_immediately(qapp):
    # F1: from the moment a resume worker exists, the host busy gate must see it
    h = Harness()
    h.pending = [_pending_rec("OLD1")]
    h.controller.run_reconcile()
    state = h.controller.in_flight_state()
    assert state is not None
    assert state["title"] == "无职转生 第三季"


def test_request_during_reconcile_dropped_and_busy_visible(qapp):
    h = Harness()
    h.pending = [_pending_rec("OLD1")]
    h.controller.run_reconcile()
    assert h.controller.in_flight_state() is not None   # host reads BUSY (F8)
    h.controller.handle_anime_request_event(_request_event("NEW"))
    assert len(h.workers) == 1                          # defensive drop kept
    assert h.dropped == ["NEW"]


def test_reconcile_queue_is_single_flight(qapp):
    # F1: two resumable pendings -> ONE active resume worker at a time (never
    # two threads sharing the qbt client); the next starts after finished.
    h = Harness()
    h.pending = [_pending_rec("OLD1"),
                 _pending_rec("OLD2", key="别的番|s1|e2", title="别的番",
                              task_id="c" * 40)]
    h.controller.run_reconcile()
    assert len(h.workers) == 1
    w1 = h.workers[0]
    assert w1.kw["resume_task_id"] == "b" * 40
    w1.ready.emit(_ready("OLD1", save_path="/dl/ep1.mkv", elapsed=None))
    assert h.controller.in_flight_state() is None       # cleared on ready
    w1.finish()                                         # thread ends -> next one
    assert len(h.workers) == 2
    w2 = h.workers[1]
    assert w2.kw["resume_task_id"] == "c" * 40
    assert h.controller.in_flight_state()["title"] == "别的番"
    w2.ready.emit(_ready("OLD2", key="别的番|s1|e2",
                         save_path="/dl/ep2.mkv", elapsed=None))
    w2.finish()
    assert h.controller.in_flight_state() is None
    assert h.played == []                               # reconcile never plays


# -- F4: terminal failures must erase the pending record ---------------------------

def test_ready_error_drops_pending(qapp):
    h = Harness()
    h.controller.handle_anime_request_event(_request_event())
    h.workers[0].ready.emit(_ready(error="需要登录 B 站", save_path=None))
    assert h.dropped == ["REQ1"]                        # no reconcile replay
    assert h.registered == []
    assert h.played == []


def test_registration_rejection_drops_pending(qapp):
    def reject(rid, key, path):
        raise ValueError("outside download_dir")
    h = Harness(register_download=reject)
    h.controller.handle_anime_request_event(_request_event())
    h.workers[0].ready.emit(_ready(elapsed=10.0))
    assert h.dropped == ["REQ1"]
    assert h.played == []


def test_reconciled_task_not_found_drops_exactly_once(qapp):
    # the silent drop of a vanished task stays, and stays a SINGLE drop
    h = Harness()
    h.pending = [_pending_rec("OLD1")]
    h.controller.run_reconcile()
    h.workers[0].ready.emit(_ready(
        "OLD1", save_path=None, elapsed=None,
        error="下载任务丢失（qbittorrent: TASK_NOT_FOUND）"))
    assert h.dropped == ["OLD1"]
    assert h.spoken == []


# -- shutdown (P1-9) -------------------------------------------------------------------

def test_shutdown_cancels_workers_and_retries(qapp):
    h = Harness()
    h.speak_result = False
    h.controller.handle_anime_request_event(_request_event())
    worker = h.workers[0]
    h.controller.shutdown(10)
    assert worker.cancelled == 1
    assert h.controller._retries == {}


def test_shutdown_cancels_deferred_auto_play(qapp):
    timer = FakeSingleShotTimer()
    h = Harness(completion_timer=timer)
    h.busy = True
    h.controller.handle_anime_request_event(_request_event())
    h.workers[0].ready.emit(_ready(elapsed=30.0))
    assert timer.isActive()

    h.controller.shutdown(10)
    h.busy = False
    timer.fire()

    assert h.played == []
    assert h.spoken == []
    assert not timer.isActive()


def test_shutdown_ignores_ready_already_queued_from_worker_thread(qapp):
    h = Harness()
    h.controller.handle_anime_request_event(_request_event())
    worker = h.workers[0]

    emitter = threading.Thread(target=lambda: worker.ready.emit(_ready()))
    emitter.start()
    emitter.join(timeout=1)
    assert not emitter.is_alive()

    h.controller.shutdown(10)
    qapp.processEvents()

    assert h.registered == []
    assert h.played == []
    assert h.marked == []
    assert h.spoken == []


def test_shutdown_force_kills_and_reaps_terminate_resistant_extractor(
        qapp, tmp_path):
    proc = _TerminateResistantProcess()
    workers = []

    def popen(argv, **kwargs):
        del argv, kwargs
        return proc

    def worker_factory(**kwargs):
        worker = AnimeDownloadWorker(**kwargs, popen=popen)
        workers.append(worker)
        return worker

    h = Harness(worker_factory=worker_factory, download_dir=str(tmp_path))
    request = AnimeRequestEvent(
        request_id="REQ1", query="无职转生第三季第一集",
        title="无职转生 第三季", episode_key="无职转生|s3|e1",
        source="bilibili", locator="BV1234567890:1")
    worker = None
    try:
        h.controller.handle_anime_request_event(request)
        [worker] = workers
        assert proc.stdout.read_started.wait(1)

        h.controller.shutdown(wait_ms=50)

        assert proc.terminate_called.is_set()
        assert proc.kill_called.is_set()
        assert proc.reaped.wait(1)
        assert not worker.isRunning()
    finally:
        if worker is not None and worker.isRunning():
            proc.kill()
            worker.wait(1000)
