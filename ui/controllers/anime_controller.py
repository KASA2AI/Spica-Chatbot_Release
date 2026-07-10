"""AnimeController (Phase 4): download lifecycle + completion behaviour.

Sits between the host->UI bridge and the download worker:

- consumes ``AnimeRequestEvent`` (host watch_anime closure -> anime bridge ->
  qt_overlay dispatch) and starts an ``AnimeDownloadWorker``. v1 is
  single-flight: while any worker is active the F8 ``in_flight_state`` seam
  reports ``{"progress","title"}`` (read by the host closure on the ChatWorker
  thread -- the dict is swapped whole under the GIL, never mutated in place)
  and a second request event is dropped defensively (the host busy gate is the
  first line; this covers the emit/attach race).
- consumes the worker's ``AnimeReadyEvent`` payload ON THE GUI THREAD and calls
  the HOST-INJECTED write closures (register / mark_played / pending ops) --
  the controller never touches the library or its files itself (P1-6), and
  auto-play only ever goes through the host play closure -> MediaPlayerPort
  (P0-4c: the adapter stays the single validation point).
- completion behaviour = ``spica.anime.playback_policy.decide_playback`` (D5 /
  P1-7): AUTO_PLAY -> host play closure (+ mark_played); ANNOUNCE -> a system
  turn via ``ProactiveTurnRequest`` -> arbiter ``try_speak``.
- P1-5 completion retry: ``try_speak`` returning False means silently dropped,
  but 「下好了」 must not be lost -- retry on a backoff QTimer until it speaks,
  the episode is consumed (played via 「放吧」), or the user speaks first
  (``notify_user_activity`` from BOTH the typed and the voice entry). The
  directive embeds the normalized 「标题 第x季 第x集」 so the LLM can
  reconstruct the watch_anime parameters next turn. ``proactive.py`` untouched.
- startup reconcile (P1-9): for each persisted pending record WITH a qbt
  task_id, resume polling (elapsed unknown -> ``reconciled_unknown_age=True``
  -> policy guarantees ANNOUNCE, never auto-play). Records without a task_id
  (an interrupted yt-dlp run) are dropped with a warning -- the ``.part`` file
  remains and a re-request resumes it.
- stall (v1 scope): a single informational system-turn ask + status chip. No
  auto-cancel, no auto source-switch -- full stall handling is Phase 5.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QTimer

from spica.anime.playback_policy import AUTO_PLAY, decide_playback
from spica.core.proactive import ProactiveTurnRequest
from spica.ports.media_player import MediaPlayerError
from ui.workers.anime_worker import AnimeDownloadWorker

logger = logging.getLogger(__name__)

# P1-5 retry backoff (seconds); past the end it keeps cycling at the last value.
_RETRY_BACKOFF_S: tuple[float, ...] = (15.0, 30.0, 60.0, 120.0)


class AnimeController(QObject):
    def __init__(
        self,
        parent: QObject | None,
        *,
        set_anime_status: Callable[[str], None],
        request_proactive_turn: Callable[[ProactiveTurnRequest], bool],
        play_file: Callable[[str], None],
        register_download: Callable[[str, str, str], Any],
        mark_played: Callable[[str], None],
        note_task_id: Callable[[str, str], None],
        list_pending: Callable[[], list],
        drop_pending: Callable[[str], None],
        is_played: Callable[[str], bool],
        is_busy: Callable[[], bool],
        galgame_active: Callable[[], bool],
        anime_config: Callable[[], Any],
        torrent_provider: Callable[[], Any],
        download_dir: str,
        cookies_file: str = "",
        worker_factory: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self._set_status = set_anime_status
        self._try_speak = request_proactive_turn
        self._play_file = play_file
        self._register_download = register_download
        self._mark_played = mark_played
        self._note_task_id = note_task_id
        self._list_pending = list_pending
        self._drop_pending = drop_pending
        self._is_played = is_played
        self._is_busy = is_busy
        self._galgame_active = galgame_active
        self._anime_config = anime_config
        self._torrent_provider = torrent_provider
        self._download_dir = download_dir
        self._cookies_file = cookies_file
        self._worker_factory = worker_factory or AnimeDownloadWorker

        self._workers: list[Any] = []        # request worker + resume workers
        self._in_flight: dict | None = None  # swapped whole (GIL-atomic read)
        self._degraded_requests: set[str] = set()
        # F1: resumable pendings wait here; ONE resume worker runs at a time
        # (single flight -- two threads must never share the qbt client).
        self._reconcile_queue: list[dict] = []
        self._reconciled_ids: set[str] = set()
        # episode_key (or synthetic error key) -> (QTimer, attempt, directive)
        self._retries: dict[str, tuple[QTimer, int, str]] = {}

    # -- F8 seam (called from the ChatWorker thread via host._anime_in_flight) --

    def in_flight_state(self) -> dict | None:
        return self._in_flight

    def _has_active_worker(self) -> bool:
        return any(w.isRunning() for w in self._workers)

    # -- AnimeRequestEvent (bridge dispatch, GUI thread) -------------------------

    def handle_anime_request_event(self, event: Any) -> None:
        if self._has_active_worker():
            # the host busy gate should have refused already -- emit/attach race
            logger.warning("anime request %s dropped: a download is active",
                           getattr(event, "request_id", "?"))
            self._drop_pending(getattr(event, "request_id", ""))
            return
        title = (getattr(event, "title", "") or
                 getattr(event, "display_title", "") or "这一集")
        cfg = self._anime_config()
        worker = self._worker_factory(
            request_id=event.request_id,
            episode_key=event.episode_key,
            title=title,
            series_title=getattr(event, "series_title", ""),  # anime name -> subfolder
            locator=event.locator,
            torrent=self._torrent_provider(),
            download_dir=self._download_dir,
            poll_seconds=float(getattr(cfg, "qbittorrent_poll_seconds", 5.0)),
            stall_timeout_minutes=float(getattr(cfg, "stall_timeout_minutes", 30.0)),
            ytdlp_format=str(getattr(
                cfg, "ytdlp_format", "bv*[height<=1080]+ba/b[height<=1080]")),
            source_timeout_seconds=float(getattr(
                cfg, "source_timeout_seconds", 15.0)),
            ytdlp_min_rate_kib_per_second=float(getattr(
                cfg, "ytdlp_min_rate_kib_per_second", 512.0)),
            cookies_file=self._cookies_file,
            parent=self,
        )
        self._start_worker(worker, reconciled=False)
        self._set_in_flight(0.0, title)
        self._set_status(f"⬇ 下载中：{title}")

    def _start_worker(self, worker: Any, *, reconciled: bool) -> None:
        if reconciled:
            self._reconciled_ids.add(worker.request_id)
        worker.progress.connect(self._on_worker_progress)
        worker.task_started.connect(self._on_worker_task_started)
        worker.stalled.connect(self._on_worker_stalled)
        worker.reconnecting.connect(self._on_worker_reconnecting)
        worker.degraded.connect(self._on_worker_degraded)
        worker.ready.connect(
            lambda ev, w=worker: self._on_worker_ready(ev, w))
        worker.finished.connect(
            lambda w=worker: self._on_worker_finished(w))
        self._workers.append(worker)
        worker.start()

    # -- worker signals (delivered queued onto the GUI thread) -------------------

    def _on_worker_progress(self, request_id: str, progress: float,
                            phase: str) -> None:
        del phase
        current = self._in_flight or {}
        title = str(current.get("title") or self._title_for(request_id))
        self._set_in_flight(progress, title)
        if request_id in self._degraded_requests:
            self._set_status(
                f"当前连接持续较慢，继续下载 {int(progress * 100)}%：{title}")
        else:
            self._set_status(f"⬇ 下载中 {int(progress * 100)}%：{title}")

    def _on_worker_task_started(self, request_id: str, task_id: str) -> None:
        # persist the qbt hash so a restart can reconcile this task (P1-9)
        self._note_task_id(request_id, task_id)

    def _on_worker_stalled(self, request_id: str, minutes: float) -> None:
        title = self._title_for(request_id)
        self._set_status(f"⏸ 下载卡住了：{title}")
        # single informational ask (v1): no retry queue, no auto-cancel/switch.
        self._try_speak(ProactiveTurnRequest(
            source="anime",
            directive=(f"你帮麦下载的动漫《{title}》已经超过 {int(minutes)} 分钟"
                       "没有进度，可能卡住了，跟麦说一声。"),
            policy="drop_if_busy",
        ))

    def _on_worker_reconnecting(self, request_id: str, used: int,
                                maximum: int, reason: str) -> None:
        title = self._title_for(request_id)
        condition = "当前连接过慢" if reason == "low_speed" else "当前连接中断"
        self._set_status(
            f"{condition}，正在重新连接 {used}/{maximum}：{title}")

    def _on_worker_degraded(self, request_id: str) -> None:
        self._degraded_requests.add(request_id)
        title = self._title_for(request_id)
        self._set_status(
            f"当前连接持续较慢，已停止自动重连，继续下载：{title}")

    def _on_worker_ready(self, event: Any, worker: Any) -> None:
        self._in_flight = None
        self._degraded_requests.discard(event.request_id)
        reconciled = event.request_id in self._reconciled_ids
        self._reconciled_ids.discard(event.request_id)
        title = getattr(worker, "title", "") or "这一集"

        if event.error:
            # F4: a terminal failure is no longer in flight -- erase the pending
            # record, or every restart would reconcile-and-refail it again.
            self._drop_pending(event.request_id)
            if reconciled and "TASK_NOT_FOUND" in str(event.error):
                # the user removed the task in qbt while we were away: stay quiet
                logger.warning("reconcile: pending task %s vanished from qbt",
                               event.request_id)
                self._set_status("")
                return
            logger.warning("anime download failed: %s", event.error)
            self._set_status(f"⚠ 下载失败：{title}")
            self._announce_with_retry(
                key=None,
                directive=f"你帮麦下载动漫《{title}》失败了：{event.error}。跟麦说一声。")
            return

        try:
            entry = self._register_download(
                event.request_id, event.episode_key, event.save_path or "")
        except (ValueError, OSError) as exc:
            # containment / extension / stat rejection: NOT registered (review).
            # F4: also terminal -- drop the pending record (a rejected file would
            # otherwise re-announce on every startup reconcile).
            logger.warning("anime registration rejected: %s", exc)
            self._drop_pending(event.request_id)
            self._set_status(f"⚠ 下载结果异常：{title}")
            self._announce_with_retry(
                key=None,
                directive=(f"你帮麦下载的动漫《{title}》下载完了，但文件检查没通过"
                           f"（{exc}），先别播。跟麦说一声。"))
            return

        cfg = self._anime_config()
        decision = decide_playback(
            elapsed_seconds=event.elapsed_seconds,
            threshold_seconds=float(getattr(
                cfg, "auto_play_threshold_seconds", 300.0)),
            is_busy=self._is_busy(),
            galgame_active=self._galgame_active(),
            reconciled_unknown_age=reconciled,
        )
        if decision.action == AUTO_PLAY:
            try:
                self._play_file(entry.file_path)   # host closure -> port checks
            except MediaPlayerError as exc:
                logger.warning("auto-play failed (%s); falling back to announce",
                               exc)
            else:
                self._mark_played(entry.episode_key)
                self._set_status("")
                return
        self._set_status(f"✅ 下好了：{entry.title}")
        self._announce_completion(entry)

    def _on_worker_finished(self, worker: Any) -> None:
        self._degraded_requests.discard(
            str(getattr(worker, "request_id", "")))
        if worker in self._workers:
            self._workers.remove(worker)
        try:
            worker.deleteLater()
        except Exception:  # noqa: BLE001
            pass
        # F1: the single-flight slot is free -- resume the next queued reconcile
        self._start_next_reconcile()

    def _title_for(self, request_id: str) -> str:
        for w in self._workers:
            if getattr(w, "request_id", None) == request_id:
                return getattr(w, "title", "") or "这一集"
        return "这一集"

    # -- completion announce + P1-5 retry ----------------------------------------

    def _announce_completion(self, entry: Any) -> None:
        # the normalized 「标题 第x季 第x集」 lets the LLM reconstruct the
        # watch_anime parameters when 麦 answers 「放吧」 (P1-11①).
        directive = (f"你帮麦下载的动漫《{entry.title}》第{entry.season}季 "
                     f"第{entry.episode}集 下载完成了，可以看了。"
                     "问问麦要不要现在看。")
        self._announce_with_retry(key=entry.episode_key, directive=directive)

    def _announce_with_retry(self, *, key: str | None, directive: str) -> None:
        retry_key = key if key is not None else f"__err_{id(directive)}"
        self._cancel_retry(retry_key)
        self._attempt_announce(retry_key, key, directive, attempt=0)

    def _attempt_announce(self, retry_key: str, key: str | None,
                          directive: str, attempt: int) -> None:
        if key is not None and self._is_played(key):
            return                     # consumed via 「放吧」/dedup play -- stop
        ok = self._try_speak(ProactiveTurnRequest(
            source="anime", directive=directive, policy="drop_if_busy"))
        if ok:
            return
        delay = _RETRY_BACKOFF_S[min(attempt, len(_RETRY_BACKOFF_S) - 1)]
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(
            lambda: self._on_retry_fired(retry_key, key, directive, attempt + 1))
        self._retries[retry_key] = (timer, attempt, directive)
        timer.start(int(delay * 1000))

    def _on_retry_fired(self, retry_key: str, key: str | None,
                        directive: str, attempt: int) -> None:
        self._retries.pop(retry_key, None)
        self._attempt_announce(retry_key, key, directive, attempt)

    def _cancel_retry(self, retry_key: str) -> None:
        pending = self._retries.pop(retry_key, None)
        if pending is not None:
            pending[0].stop()

    def notify_user_activity(self) -> None:
        """The user spoke first (typed OR voice entry): drop every pending
        announce retry (P1-5 stop condition). The 「放吧」 pointer still covers
        the episode when 麦 asks for it later."""
        for timer, _, _ in self._retries.values():
            timer.stop()
        self._retries.clear()

    # -- startup reconcile (P1-9) -------------------------------------------------

    def start_reconcile(self, delay_ms: int = 3000) -> None:
        """Schedule the qbt reconcile shortly after startup (never during
        AppHost.initialize -- reconcile talks to the network)."""
        QTimer.singleShot(delay_ms, self.run_reconcile)

    def run_reconcile(self) -> None:
        for rec in self._list_pending():
            request_id = str(rec.get("request_id") or "")
            task_id = rec.get("task_id")
            if not task_id:
                # interrupted yt-dlp run: nothing external finishes it; the
                # .part remains and a re-request resumes the download.
                logger.warning("reconcile: dropping pending %s (no task_id)",
                               request_id)
                self._drop_pending(request_id)
                continue
            self._reconcile_queue.append(dict(rec))
        self._start_next_reconcile()

    def _start_next_reconcile(self) -> None:
        """F1: start AT MOST one resume worker, and only while nothing else is
        downloading -- resume workers occupy the same single-flight slot (and
        the F8 busy seam) as a fresh request, so the host busy gate refuses new
        downloads for their whole lifetime and the qbt client is never shared
        across worker threads."""
        if self._has_active_worker() or not self._reconcile_queue:
            return
        rec = self._reconcile_queue.pop(0)
        title = str(rec.get("title") or "这一集")
        cfg = self._anime_config()
        worker = self._worker_factory(
            request_id=str(rec.get("request_id") or ""),
            episode_key=str(rec.get("episode_key") or ""),
            title=title,
            locator="",
            torrent=self._torrent_provider(),
            download_dir=self._download_dir,
            poll_seconds=float(getattr(cfg, "qbittorrent_poll_seconds", 5.0)),
            stall_timeout_minutes=float(
                getattr(cfg, "stall_timeout_minutes", 30.0)),
            cookies_file=self._cookies_file,
            resume_task_id=str(rec.get("task_id")),
            parent=self,
        )
        self._start_worker(worker, reconciled=True)
        self._set_in_flight(0.0, title)          # F8: busy from the first moment
        self._set_status(f"⬇ 继续下载：{title}")

    # -- misc ---------------------------------------------------------------------

    def _set_in_flight(self, progress: float, title: str) -> None:
        # swap the WHOLE dict: host reads it from the ChatWorker thread (F8)
        self._in_flight = {"progress": float(progress), "title": title}

    def shutdown(self, wait_ms: int = 1500) -> None:
        """P1-9 exit: stop retry timers; terminate yt-dlp keeping .part; stop
        qbt polling only (the external service keeps downloading)."""
        self.notify_user_activity()
        # a finished signal during shutdown must not start the next reconcile
        self._reconcile_queue.clear()
        for worker in list(self._workers):
            try:
                worker.cancel()
            except Exception:  # noqa: BLE001
                pass
        for worker in list(self._workers):
            if worker.isRunning() and not worker.wait(wait_ms):
                worker.force_kill()
                worker.wait(max(1000, int(wait_ms)))
        self._workers.clear()
