"""Phase 4: AnimeController -- event->worker, completion policy, P1-5 retry,
startup reconcile (P1-9). All host closures and workers are fakes; signals are
delivered synchronously (same thread, direct connection)."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from spica.core.anime_events import AnimeReadyEvent, AnimeRequestEvent  # noqa: E402
from spica.ports.media_player import MediaPlayerError  # noqa: E402
from ui.controllers.anime_controller import AnimeController  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeWorker(QObject):
    progress = Signal(str, float, str)
    ready = Signal(object)
    stalled = Signal(str, float)
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


def _request_event(rid="REQ1", key="无职转生|s3|e1"):
    return AnimeRequestEvent(
        request_id=rid, query="无职转生第三季第一集", title="无职转生 第三季",
        episode_key=key, source="mikan",
        locator="magnet:?xt=urn:btih:" + "a" * 40)


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

        def factory(**kw):
            w = FakeWorker(**kw)
            self.workers.append(w)
            return w

        kwargs = dict(
            set_anime_status=self.status.append,
            request_proactive_turn=try_speak,
            play_file=play_file,
            register_download=register,
            mark_played=self.marked.append,
            note_task_id=lambda rid, tid: self.noted.append((rid, tid)),
            list_pending=lambda: [dict(p) for p in self.pending],
            drop_pending=self.dropped.append,
            is_played=lambda key: key in self.played_keys,
            is_busy=lambda: self.busy,
            galgame_active=lambda: self.galgame,
            anime_config=lambda: SimpleNamespace(
                auto_play_threshold_seconds=300.0, qbittorrent_poll_seconds=5.0,
                stall_timeout_minutes=30.0, ytdlp_format="fmt"),
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
    assert h.controller.in_flight_state() == {"progress": 0.0,
                                              "title": "无职转生 第三季"}
    assert any("下载中" in s for s in h.status)


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


@pytest.mark.parametrize("mutate,reason", [
    (lambda h: setattr(h, "busy", True), "busy"),
    (lambda h: setattr(h, "galgame", True), "galgame"),
])
def test_ready_busy_or_galgame_announces_instead(qapp, mutate, reason):
    h = Harness()
    mutate(h)
    h.controller.handle_anime_request_event(_request_event())
    h.workers[0].ready.emit(_ready(elapsed=10.0))
    assert h.played == []
    assert len(h.spoken) == 1


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


# -- P1-5 completion retry ---------------------------------------------------------

def _announce_dropped(h):
    h.speak_result = False
    h.controller.handle_anime_request_event(_request_event())
    h.workers[0].ready.emit(_ready(elapsed=1000.0))
    assert len(h.spoken) == 1                       # first try, dropped
    assert len(h.controller._retries) == 1          # backoff timer armed
    return list(h.controller._retries.keys())[0]


def test_retry_after_busy_drop_until_success(qapp):
    h = Harness()
    key = _announce_dropped(h)
    directive = h.spoken[0].directive
    # tick 1: still busy -> re-armed
    h.controller._on_retry_fired(key, "无职转生|s3|e1", directive, attempt=1)
    assert len(h.spoken) == 2
    assert len(h.controller._retries) == 1
    # tick 2: she is free now -> spoken, retry chain ends
    h.speak_result = True
    h.controller._on_retry_fired(key, "无职转生|s3|e1", directive, attempt=2)
    assert len(h.spoken) == 3
    assert h.controller._retries == {}


def test_retry_stops_when_episode_consumed(qapp):
    h = Harness()
    key = _announce_dropped(h)
    directive = h.spoken[0].directive
    h.played_keys.add("无职转生|s3|e1")             # 「放吧」consumed it meanwhile
    h.controller._on_retry_fired(key, "无职转生|s3|e1", directive, attempt=1)
    assert len(h.spoken) == 1                       # no further announce
    assert h.controller._retries == {}


def test_retry_stops_on_user_activity(qapp):
    h = Harness()
    _announce_dropped(h)
    h.controller.notify_user_activity()             # typed OR voice entry
    assert h.controller._retries == {}


# -- stall (v1: informational ask only) ---------------------------------------------

def test_stall_announces_once_and_flags_chip(qapp):
    h = Harness()
    h.controller.handle_anime_request_event(_request_event())
    h.workers[0].stalled.emit("REQ1", 30.0)
    [req] = h.spoken
    assert "没有进度" in req.directive
    assert any("卡住" in s for s in h.status)
    assert h.controller._retries == {}              # no retry queue for stalls


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
