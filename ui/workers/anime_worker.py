"""Anime download worker (Phase 4) -- download / poll / signal ONLY.

The worker NEVER touches host or library (P1-6): completion is reported as an
``AnimeReadyEvent`` payload on a Qt signal, consumed by ``AnimeController`` on
the GUI thread, which calls the host-injected write closures. Progress is a
UI-internal Qt signal too -- it never crosses the host->UI RuntimeEvent boundary
(P2-19).

Two download lanes, dispatched by locator:
- ``magnet:?xt=urn:btih:..`` -> qbt ``add_magnet`` + status polling. qbt is an
  EXTERNAL resident service: cancel / app exit stops OUR POLLING only, never the
  service or the task (P1-9). A transient connection error means reconnect and
  keep polling, never a failure verdict (P1-10).
- ``BV<10 alnum>:<part>``    -> yt-dlp subprocess with a FIXED argv list and
  ``shell=False``.

yt-dlp safety (plan §5.2 / review):
- the locator must match the whitelist regex; the URL is built ONLY from the
  validated bvid template -- the LLM/user never supplies a URL, path or flag.
- output is pinned under download_dir (``-P`` + fixed ``-o`` template).
- cookies only ever via ``--cookies <file>`` (a config-resolved path); the
  cookie VALUE never enters argv or logs.
- the final path yt-dlp reports (``--print after_move:filepath``) is
  RE-VALIDATED (real containment in download_dir + media extension) before it
  is trusted (the controller's register closure validates again host-side).
- termination keeps ``.part`` files: no ``--no-part``, terminate only, never
  delete (P1-9 -- a re-request resumes the partial download).

``resume_task_id`` mode (startup reconcile, P1-9): poll an ALREADY-RUNNING qbt
task; elapsed is unknown across a restart, so the ready event carries
``elapsed_seconds=None`` -> playback policy always ANNOUNCEs, never auto-plays.

qbt stall detection remains progress-driven and informational. The yt-dlp lane
uses machine-readable byte counters: a connection below the configured floor
for 15 seconds restarts the whole extractor (while preserving ``.part``) up to
two times. The final attempt continues at the available rate instead of treating
the heuristic as a hard failure.
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QThread, Signal

from spica.anime.download_health import DownloadHealthMonitor, DownloadProgressSample
from spica.anime.models import anime_dirname
from spica.core.anime_events import AnimeReadyEvent
from spica.ports.media_player import MEDIA_EXTENSIONS
from spica.ports.torrent_client import TorrentClientError

_BVID_PART_RE = re.compile(r"^(BV[0-9A-Za-z]{10}):(\d{1,4})$")
_YTDLP_PROGRESS_TEMPLATE = (
    'download:SPICA:{"format_id":%(info.format_id|null)j,'
    '"progress":%(progress)j}'
)
_YTDLP_PROGRESS_PREFIX = "SPICA:"
_YTDLP_LOW_SPEED_WINDOW_SECONDS = 15.0
_MAX_YTDLP_RECONNECTS = 2
_YTDLP_RECONNECT_DELAY_SECONDS = 1.0
_YTDLP_READER_POLL_SECONDS = 0.1
_PROCESS_TERMINATE_TIMEOUT_SECONDS = 1.5
_PROCESS_KILL_TIMEOUT_SECONDS = 0.5
_READER_JOIN_TIMEOUT_SECONDS = 0.5  # total cleanup budget, not per join
_READER_EOF = object()
# The ONLY long-lived transient (P1-10): the qbt service being down/restarting
# means reconnect and keep polling. AUTH_FAILED is a credential problem (the
# adapter already re-logins on 403; what reaches us never self-heals) -> fail
# fast. API_ERROR gets a BOUNDED retry streak, then fails (F2).
_TRANSIENT_QBT = frozenset({"UNREACHABLE"})
_MAX_API_ERROR_STREAK = 12
# interruptible-sleep slice (F3): cancel/exit must never wait a full poll period
_SLEEP_SLICE = 0.1


class _YtDlpFailureKind(Enum):
    ENTITLEMENT = auto()
    AUTH = auto()
    UNAVAILABLE = auto()
    LOCAL = auto()
    NETWORK = auto()
    OTHER = auto()


@dataclass(frozen=True)
class _YtDlpFailure:
    kind: _YtDlpFailureKind
    message: str


def _analyze_ytdlp_failure(tail: str) -> _YtDlpFailure:
    """Classify raw yt-dlp output before producing localized UI text."""
    low = tail.lower()
    if any(token in low for token in (
            "充电", "大会员", "会员专属", "premium-only",
            "premium only", "premium member", "members only",
            "purchase required")):
        return _YtDlpFailure(
            _YtDlpFailureKind.ENTITLEMENT,
            "这集是充电/大会员专属，当前账号看不了")
    if any(token in low for token in (
            "no space left on device", "disk quota", "permission denied",
            "read-only file system", "postprocessing", "post-processing",
            "ffmpeg", "unable to rename")):
        return _YtDlpFailure(
            _YtDlpFailureKind.LOCAL,
            f"本地保存或合并失败：{tail[-300:].strip() or '未知错误'}")
    if any(token in low for token in (
            "cookies", "cookie ", "login", "log in", "sign in",
            "登录", "account required", "account is required",
            "account to view", "authentication required")):
        return _YtDlpFailure(
            _YtDlpFailureKind.AUTH,
            "需要登录 B 站（cookie 缺失或已过期）")
    if any(token in low for token in (
            "this video is unavailable", "video is unavailable",
            "video has been removed", "private video", "geo restricted",
            "geo-restricted", "may be deleted", "not available in your country",
            "不存在", "已删除",
            "区域限制", "版权限制")):
        return _YtDlpFailure(
            _YtDlpFailureKind.UNAVAILABLE,
            "这集当前不可用（可能已删除、设为私密或受地区限制）")
    if (
        re.search(
            r"\bwinerror\s+(?:10053|10054|10060|10061|11001)\b", low)
        or any(token in low for token in (
            "incompleteread", "getaddrinfo failed",
            "unable to download json metadata", "unable to download api page",
            "timed out", "timeout", "connection reset",
            "connection refused", "network is unreachable",
            "temporary failure in name resolution", "name or service not known",
            "remote end closed connection", "unable to download webpage",
            "unable to download video data", "read error", "broken pipe",
            "unexpected_eof_while_reading", "eof occurred in violation"))
        or re.search(
            r"http error (?:403|404|408|416|425|429|5\d\d)\b", low)
    ):
        return _YtDlpFailure(
            _YtDlpFailureKind.NETWORK,
            f"B 站连接失败：{tail[-300:].strip() or '未知网络错误'}")
    return _YtDlpFailure(
        _YtDlpFailureKind.OTHER,
        f"yt-dlp 下载失败：{tail[-300:].strip() or '未知错误'}")


@dataclass(frozen=True)
class _OutputLine:
    observed_at: float
    text: str


@dataclass(frozen=True)
class _YtDlpAttemptResult:
    returncode: int
    final_path: str | None
    tail: tuple[str, ...]
    low_speed: bool = False
    cancelled: bool = False
    lifecycle_error: str | None = None


@dataclass(frozen=True)
class _ProcessStopResult:
    returncode: int | None


class AnimeDownloadWorker(QThread):
    # UI-internal signals (P2-19): worker thread -> controller (GUI thread).
    progress = Signal(str, float, str)   # request_id, 0..1, phase text
    ready = Signal(object)               # AnimeReadyEvent (success OR error)
    stalled = Signal(str, float)         # request_id, minutes without progress
    task_started = Signal(str, str)      # request_id, qbt task_id (btih)
    reconnecting = Signal(str, int, int, str)  # request_id, used, max, reason
    degraded = Signal(str)               # request_id

    def __init__(
        self,
        *,
        request_id: str,
        episode_key: str,
        title: str,
        locator: str,
        torrent: Any,
        download_dir: str,
        series_title: str = "",
        poll_seconds: float = 5.0,
        stall_timeout_minutes: float = 30.0,
        ytdlp_format: str = "bv*[height<=1080]+ba/b[height<=1080]",
        source_timeout_seconds: float = 15.0,
        ytdlp_min_rate_kib_per_second: float = 512.0,
        cookies_file: str = "",
        resume_task_id: str | None = None,
        parent: Any = None,
        popen: Callable[..., Any] | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.request_id = request_id
        self.episode_key = episode_key
        self.title = title
        self.locator = locator
        self.resume_task_id = resume_task_id
        self._torrent = torrent
        # base = the containment root (media_player / _validated_output check
        # against THIS); target = base/<anime> so the cache groups by anime NAME
        # (series_title = RequestSpec.title_query, NOT the full source release
        # name in `title`). Fall back to title only if the name is unavailable.
        self._download_dir = str(Path(download_dir).expanduser().resolve())
        self._subdir = anime_dirname(series_title or title)
        self._target_dir = str(Path(self._download_dir) / self._subdir)
        self._poll_seconds = max(0.5, float(poll_seconds))
        self._stall_seconds = max(60.0, float(stall_timeout_minutes) * 60.0)
        self._ytdlp_format = ytdlp_format
        self._source_timeout_seconds = max(1.0, float(source_timeout_seconds))
        self._ytdlp_min_rate_bytes = max(
            0.0, float(ytdlp_min_rate_kib_per_second) * 1024.0)
        self._cookies_file = cookies_file
        self._popen = popen if popen is not None else subprocess.Popen
        self._clock = clock if clock is not None else time.monotonic
        self._sleep = sleep if sleep is not None else time.sleep
        self._proc: Any = None
        self._proc_lock = threading.Lock()
        self._cancelled = False

    # -- lifecycle -------------------------------------------------------------

    def cancel(self) -> None:
        """P1-9 exit path: stop polling (qbt task keeps running in the external
        service) / terminate the yt-dlp subprocess KEEPING its .part file."""
        with self._proc_lock:
            self._cancelled = True
            proc = self._proc
        self.requestInterruption()
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def force_kill(self) -> None:
        """Escalation for a terminate-resistant subprocess (controller calls it
        after a bounded wait)."""
        with self._proc_lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def _adopt_proc(self, proc: Any) -> bool:
        """Publish a spawned process and atomically observe prior cancel.

        Popen itself intentionally stays outside this lock.  If cancel wins the
        lock first, this method sees its flag; if adoption wins, cancel sees the
        process.  Process signalling and waiting always happen after unlock.
        """
        with self._proc_lock:
            self._proc = proc
            return self._cancelled

    def _clear_proc(self, proc: Any) -> None:
        with self._proc_lock:
            if self._proc is proc:
                self._proc = None

    def _cancel_requested(self) -> bool:
        with self._proc_lock:
            return self._cancelled

    @staticmethod
    def _terminate_and_wait(proc: Any) -> _ProcessStopResult:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            return _ProcessStopResult(
                int(proc.wait(timeout=_PROCESS_TERMINATE_TIMEOUT_SECONDS)))
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                return _ProcessStopResult(
                    int(proc.wait(timeout=_PROCESS_KILL_TIMEOUT_SECONDS)))
            except subprocess.TimeoutExpired:
                polled = proc.poll()
                return _ProcessStopResult(
                    None if polled is None else int(polled))

    @staticmethod
    def _start_stdout_close(proc: Any) -> threading.Thread | None:
        stdout = getattr(proc, "stdout", None)
        if stdout is None or not hasattr(stdout, "close"):
            return None

        def close_pipe() -> None:
            try:
                stdout.close()
            except (OSError, RuntimeError, ValueError):
                pass

        closer = threading.Thread(
            target=close_pipe,
            name="anime-ytdlp-stdout-closer",
            daemon=True,
        )
        closer.start()
        return closer

    @staticmethod
    def _start_process_reaper(proc: Any) -> None:
        """Retain and reap a process that did not exit within the kill budget."""
        def reap() -> None:
            while proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    continue
                except OSError:
                    return
            try:
                proc.wait(timeout=0)
            except (OSError, subprocess.TimeoutExpired):
                pass

        threading.Thread(
            target=reap,
            name="anime-ytdlp-process-reaper",
            daemon=True,
        ).start()

    def _interrupted(self) -> bool:
        # requestInterruption is a no-op while the thread is NOT running (e.g.
        # synchronous execute() in tests) -> the explicit flag must also count.
        return self._cancel_requested() or self.isInterruptionRequested()

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep in small slices so cancel/exit returns within ~_SLEEP_SLICE,
        never a full poll period (F3 -- shutdown's bounded wait must suffice)."""
        remaining = float(seconds)
        while remaining > 0 and not self._interrupted():
            step = min(_SLEEP_SLICE, remaining)
            self._sleep(step)
            remaining -= step

    def run(self) -> None:  # QThread entry
        try:
            event = self.execute()
        except Exception as exc:  # noqa: BLE001 -- a worker must never crash the UI
            event = self._ready_event(error=str(exc))
        if event is not None and not self._interrupted():
            self.ready.emit(event)

    # -- core (synchronous; unit tests call this directly) ----------------------

    def execute(self) -> AnimeReadyEvent | None:
        """Run the download to completion; returns the ready payload, or None
        when cancelled (nothing is emitted then)."""
        if self.resume_task_id:
            return self._poll_qbt(self.resume_task_id, started=None)
        loc = self.locator or ""
        if loc.startswith("magnet:?"):
            started = self._clock()
            task_id = self._torrent.add_magnet(loc, subfolder=self._subdir)   # magnet-only port (P0-3), grouped by anime
            self.task_started.emit(self.request_id, task_id)
            return self._poll_qbt(task_id, started=started)
        m = _BVID_PART_RE.match(loc)
        if m is not None:
            return self._run_ytdlp(m.group(1), int(m.group(2)))
        # never execute an unrecognized locator (whitelist, 铁律 #9)
        return self._ready_event(error=f"BAD_LOCATOR: {loc[:80]!r}")

    # -- qbt lane ---------------------------------------------------------------

    def _poll_qbt(self, task_id: str, *, started: float | None) -> AnimeReadyEvent | None:
        last_progress = -1.0
        last_change = self._clock()
        stall_reported = False
        api_error_streak = 0
        while not self._interrupted():
            try:
                st = self._torrent.status(task_id)
            except TorrentClientError as e:
                if e.code == "AUTH_FAILED":       # credentials: never self-heals (F2)
                    return self._ready_event(
                        error="qbittorrent 登录失败（AUTH_FAILED）："
                              "请检查 Web UI 用户名/密码配置")
                if e.code in _TRANSIENT_QBT:      # reconnect, never fail (P1-10)
                    # P2-2: a LONG outage still owes the user one notice -- the
                    # normal (progress-driven) stall check below is skipped on
                    # this continue path, so fire it here too. last_change never
                    # advances while unreachable, so this trips once per dry
                    # spell and resets when progress resumes (never permanent
                    # silent 0% busy).
                    if (not stall_reported
                            and self._clock() - last_change >= self._stall_seconds):
                        self.stalled.emit(self.request_id, self._stall_seconds / 60.0)
                        stall_reported = True
                    self._interruptible_sleep(self._poll_seconds)
                    continue
                if e.code == "API_ERROR":         # bounded retry, then fail (F2)
                    api_error_streak += 1
                    if api_error_streak >= _MAX_API_ERROR_STREAK:
                        return self._ready_event(
                            error=f"qbittorrent 接口连续出错（API_ERROR×"
                                  f"{api_error_streak}）：{e}")
                    self._interruptible_sleep(self._poll_seconds)
                    continue
                return self._ready_event(
                    error=f"下载任务丢失（qbittorrent: {e.code}）")
            api_error_streak = 0
            if st.progress > last_progress + 1e-6:
                last_progress = st.progress
                last_change = self._clock()
                stall_reported = False
            self.progress.emit(self.request_id, float(st.progress), "downloading")
            if st.is_done:
                elapsed = (self._clock() - started) if started is not None else None
                return self._ready_event(save_path=st.save_path,
                                         elapsed_seconds=elapsed)
            if st.state == "error":
                return self._ready_event(
                    error=f"下载出错：{st.error or 'qbittorrent errored'}")
            if (not stall_reported
                    and self._clock() - last_change >= self._stall_seconds):
                self.stalled.emit(self.request_id, self._stall_seconds / 60.0)
                stall_reported = True
            self._interruptible_sleep(self._poll_seconds)
        return None                                    # cancelled

    # -- yt-dlp lane ------------------------------------------------------------

    def _ytdlp_argv(self, bvid: str, part: int) -> list[str]:
        """FIXED argv (shell=False). The URL comes only from the validated bvid;
        cookies only as a file path; output pinned under download_dir."""
        argv = [
            sys.executable, "-m", "yt_dlp",
            "--ignore-config", "--encoding", "utf-8",
            "--newline", "--no-warnings", "--progress",
            "--progress-delta", "1",
            "--progress-template", _YTDLP_PROGRESS_TEMPLATE,
            "--socket-timeout", str(self._source_timeout_seconds),
            "--retries", "1", "--retry-sleep", "http:1",
            "--part", "--continue",
            "-f", self._ytdlp_format,
            "-I", str(part),
            "-P", self._target_dir,
            "-o", "%(title)s [P%(playlist_index)02d].%(ext)s",
            "--print", "after_move:filepath", "--no-simulate",
        ]
        if self._cookies_file and Path(self._cookies_file).is_file():
            argv += ["--cookies", self._cookies_file]
        argv.append(f"https://www.bilibili.com/video/{bvid}")
        return argv

    def _run_ytdlp(self, bvid: str, part: int) -> AnimeReadyEvent | None:
        overall_started = self._clock()
        argv = self._ytdlp_argv(bvid, part)
        reconnects = 0
        while not self._interrupted():
            attempt = self._run_ytdlp_attempt(
                argv, stop_on_low_speed=reconnects < _MAX_YTDLP_RECONNECTS)
            if attempt.cancelled or self._interrupted():
                return None
            if attempt.lifecycle_error is not None:
                return self._ready_event(error=attempt.lifecycle_error)
            if attempt.low_speed:
                reconnects += 1
                self.reconnecting.emit(
                    self.request_id, reconnects, _MAX_YTDLP_RECONNECTS,
                    "low_speed")
                self._interruptible_sleep(_YTDLP_RECONNECT_DELAY_SECONDS)
                continue
            if attempt.returncode != 0:
                failure = _analyze_ytdlp_failure("\n".join(attempt.tail))
                if (failure.kind is _YtDlpFailureKind.NETWORK
                        and reconnects < _MAX_YTDLP_RECONNECTS):
                    reconnects += 1
                    self.reconnecting.emit(
                        self.request_id, reconnects, _MAX_YTDLP_RECONNECTS,
                        "network")
                    self._interruptible_sleep(_YTDLP_RECONNECT_DELAY_SECONDS)
                    continue
                return self._ready_event(
                    error=failure.message)
            checked = self._validated_output(attempt.final_path)
            if checked is None:
                return self._ready_event(
                    error="yt-dlp 结束但没有产出可信的输出文件路径")
            elapsed = self._clock() - overall_started
            self.progress.emit(self.request_id, 1.0, "downloading")
            return self._ready_event(save_path=checked, elapsed_seconds=elapsed)
        return None

    def _run_ytdlp_attempt(
        self,
        argv: list[str],
        *,
        stop_on_low_speed: bool,
    ) -> _YtDlpAttemptResult:
        if self._interrupted():
            return _YtDlpAttemptResult(
                returncode=0, final_path=None, tail=(), cancelled=True)

        # stderr is merged into the one pipe. Exactly one reader thread owns that
        # pipe; the worker consumes timestamped records from the queue.  A cancel
        # can land during this intentionally unlocked Popen call; adoption below
        # then makes the returned process owned and immediately stoppable.
        proc = self._popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            shell=False, text=True, encoding="utf-8", errors="replace")
        cancel_seen_on_adopt = self._adopt_proc(proc)
        records: queue.Queue[Any] = queue.Queue()
        reader: threading.Thread | None = None

        health = DownloadHealthMonitor(
            min_rate_bytes_per_second=self._ytdlp_min_rate_bytes,
            window_seconds=_YTDLP_LOW_SPEED_WINDOW_SECONDS)
        tail: deque[str] = deque(maxlen=40)
        final_path: str | None = None
        returncode: int | None = None
        eof_seen = False
        low_speed = False
        cancelled = False
        degraded_emitted = False
        reader_cleanup_started = False
        process_stop_attempted = False
        process_handed_off = False
        lifecycle_error: str | None = None
        pipe_closer: threading.Thread | None = None
        reader_cleanup_deadline: float | None = None

        try:
            reader = threading.Thread(
                target=self._pump_ytdlp_stdout,
                args=(proc.stdout, records),
                name=f"anime-ytdlp-reader-{self.request_id}", daemon=True)
            reader.start()
            while returncode is None or not eof_seen:
                if ((cancel_seen_on_adopt or self._interrupted())
                        and not cancelled):
                    cancel_seen_on_adopt = False
                    cancelled = True
                    process_stop_attempted = True
                    stopped = self._terminate_and_wait(proc)
                    reader_cleanup_deadline = (
                        time.monotonic() + _READER_JOIN_TIMEOUT_SECONDS)
                    if stopped.returncode is None:
                        self._start_process_reaper(proc)
                        process_handed_off = True
                        lifecycle_error = (
                            "yt-dlp 进程在强制终止后仍未退出")
                        returncode = -1
                    else:
                        returncode = stopped.returncode

                try:
                    record = records.get(timeout=_YTDLP_READER_POLL_SECONDS)
                except queue.Empty:
                    record = None

                if record is _READER_EOF:
                    eof_seen = True
                elif isinstance(record, _OutputLine):
                    line = record.text
                    progress = self._parse_ytdlp_progress(line)
                    if progress is not None:
                        fraction, sample = progress(record.observed_at)
                        if fraction is not None:
                            self.progress.emit(
                                self.request_id, fraction, "downloading")
                        if (not low_speed and not cancelled
                                and health.observe(sample)):
                            polled = proc.poll()
                            if polled is not None:
                                returncode = int(polled)
                            elif stop_on_low_speed:
                                low_speed = True
                                process_stop_attempted = True
                                stopped = self._terminate_and_wait(proc)
                                reader_cleanup_deadline = (
                                    time.monotonic()
                                    + _READER_JOIN_TIMEOUT_SECONDS)
                                if stopped.returncode is None:
                                    self._start_process_reaper(proc)
                                    process_handed_off = True
                                    lifecycle_error = (
                                        "yt-dlp 进程在强制终止后仍未退出")
                                    returncode = -1
                                else:
                                    returncode = stopped.returncode
                            elif not degraded_emitted:
                                degraded_emitted = True
                                health.reset()
                                self.degraded.emit(self.request_id)
                        continue

                    if line.startswith(_YTDLP_PROGRESS_PREFIX):
                        health.reset()
                        if line:
                            tail.append(line)
                        continue

                    if line:
                        tail.append(line)
                    stripped = line.strip()
                    if stripped.startswith(("/", "\\")) or (
                            len(stripped) > 2 and stripped[1] == ":"):
                        final_path = stripped  # --print after_move:filepath

                if returncode is None:
                    polled = proc.poll()
                    if polled is not None:
                        returncode = int(polled)

                if eof_seen and returncode is None:
                    try:
                        returncode = int(proc.wait(
                            timeout=_YTDLP_READER_POLL_SECONDS))
                    except subprocess.TimeoutExpired:
                        pass

                if (returncode is not None and not eof_seen
                        and process_stop_attempted
                        and not reader_cleanup_started):
                    reader_cleanup_started = True
                    deadline = reader_cleanup_deadline or time.monotonic()
                    remaining = max(0.0, deadline - time.monotonic())
                    reader.join(min(0.05, remaining / 2.0))
                    if reader.is_alive():
                        pipe_closer = self._start_stdout_close(proc)
                        remaining = max(0.0, deadline - time.monotonic())
                        reader.join(remaining)
                    if reader.is_alive():
                        lifecycle_error = (
                            lifecycle_error
                            or "yt-dlp 输出读取线程未能在进程退出后结束")
                        break

            if reader_cleanup_deadline is None:
                reader.join(_READER_JOIN_TIMEOUT_SECONDS)
            else:
                reader.join(max(
                    0.0, reader_cleanup_deadline - time.monotonic()))
            return _YtDlpAttemptResult(
                returncode=int(returncode or 0), final_path=final_path,
                tail=tuple(tail), low_speed=low_speed, cancelled=cancelled,
                lifecycle_error=lifecycle_error)
        finally:
            if proc.poll() is None and not process_stop_attempted:
                stopped = self._terminate_and_wait(proc)
                if stopped.returncode is None:
                    self._start_process_reaper(proc)
                    process_handed_off = True
            if reader is not None and reader.is_alive():
                if pipe_closer is None:
                    pipe_closer = self._start_stdout_close(proc)
                if reader_cleanup_deadline is None:
                    reader.join(_READER_JOIN_TIMEOUT_SECONDS)
                else:
                    reader.join(max(
                        0.0, reader_cleanup_deadline - time.monotonic()))
            if proc.poll() is not None or process_handed_off:
                self._clear_proc(proc)

    def _pump_ytdlp_stdout(self, stdout: Any,
                            records: queue.Queue[Any]) -> None:
        try:
            if stdout is not None:
                for raw_line in stdout:
                    records.put(_OutputLine(
                        observed_at=self._clock(),
                        text=str(raw_line).rstrip("\r\n")))
        except Exception as exc:  # noqa: BLE001 -- surfaced in the attempt tail
            records.put(_OutputLine(
                observed_at=self._clock(),
                text=f"yt-dlp stdout reader failed: {exc}"))
        finally:
            records.put(_READER_EOF)

    @staticmethod
    def _parse_ytdlp_progress(
        line: str,
    ) -> Callable[[float], tuple[float | None, DownloadProgressSample]] | None:
        if not line.startswith(_YTDLP_PROGRESS_PREFIX):
            return None

        def optional_nonnegative_int(value: Any) -> int | None:
            if value is None:
                return None
            if type(value) is not int or value < 0:
                raise ValueError("invalid yt-dlp byte counter")
            return value

        def optional_text(value: Any) -> str | None:
            if value is None:
                return None
            if not isinstance(value, str):
                raise TypeError("invalid yt-dlp progress text field")
            return value

        try:
            payload = json.loads(line[len(_YTDLP_PROGRESS_PREFIX):])
            if not isinstance(payload, dict):
                return None
            raw_progress = payload["progress"]
            if not isinstance(raw_progress, dict):
                return None
            status = optional_text(raw_progress.get("status"))
            if status is None:
                raise ValueError("missing yt-dlp progress status")
            downloaded = optional_nonnegative_int(
                raw_progress.get("downloaded_bytes"))
            total_bytes = optional_nonnegative_int(
                raw_progress.get("total_bytes"))
            total_estimate = optional_nonnegative_int(
                raw_progress.get("total_bytes_estimate"))
            total = (total_bytes if total_bytes is not None
                     else total_estimate)
            format_id = optional_text(payload.get("format_id"))
            tmp_value = optional_text(raw_progress.get("tmpfilename"))
            filename_value = optional_text(raw_progress.get("filename"))
            tmpfilename = (tmp_value if tmp_value is not None
                           else filename_value)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

        def parsed(observed_at: float) -> tuple[
                float | None, DownloadProgressSample]:
            fraction = None
            if downloaded is not None and total is not None and total > 0:
                fraction = max(0.0, min(1.0, downloaded / total))
            return fraction, DownloadProgressSample(
                observed_at=observed_at, status=status,
                downloaded_bytes=downloaded, format_id=format_id,
                tmpfilename=tmpfilename)

        return parsed

    def _validated_output(self, path: str | None) -> str | None:
        """Second validation gate (review): the path yt-dlp printed must be a
        real media file inside download_dir, else it is NOT trusted."""
        if not path:
            return None
        try:
            rp = Path(path).resolve()
        except OSError:
            return None
        if not rp.is_relative_to(Path(self._download_dir)):
            return None
        if not rp.is_file() or rp.suffix.lower() not in MEDIA_EXTENSIONS:
            return None
        return str(rp)

    # -- helpers ---------------------------------------------------------------

    def _ready_event(self, *, save_path: str | None = None,
                     elapsed_seconds: float | None = None,
                     error: str | None = None) -> AnimeReadyEvent:
        return AnimeReadyEvent(
            request_id=self.request_id, episode_key=self.episode_key,
            save_path=save_path, elapsed_seconds=elapsed_seconds, error=error)
