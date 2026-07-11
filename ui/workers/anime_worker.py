"""Anime download worker (Phase 4) -- download / poll / signal ONLY.

The worker NEVER touches host or library (P1-6): completion is reported as an
``AnimeReadyEvent`` payload on a Qt signal, consumed by ``AnimeController`` on
the GUI thread, which calls the host-injected write closures. Progress is a
UI-internal Qt signal too -- it never crosses the host->UI RuntimeEvent boundary
(P2-19).

Two download lanes, dispatched by locator:
- ``magnet:?xt=urn:btih:..`` -> qbt status polling after either a verified
  in-memory ``.torrent`` upload (new Mikan requests) or ``add_magnet`` (legacy
  events/pending tasks). qbt is an EXTERNAL resident service: app exit stops OUR
  POLLING only and preserves the task (P1-9). A short connection error reconnects,
  but continuous no-progress beyond ``stall_timeout_minutes`` is a hard cutoff:
  the worker asks qbt to remove the category-owned task and delete partial data
  through its completion-safe protocol, then returns a typed terminal event so
  the controller releases the single-flight slot.
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
task. Stall age inherits qBT's last-activity timestamp (pending creation is the
fallback), while playback elapsed remains unknown, so the ready event carries
``elapsed_seconds=None`` -> playback policy always ANNOUNCEs, never auto-plays.

qbt stall timeout remains progress-driven: any effective progress resets it. The
manual stop path is terminal and deletes category-scoped qBT partial data; app
shutdown remains local-only and preserves the external task. The yt-dlp lane
uses machine-readable byte counters: a connection below the configured floor
for 15 seconds restarts the whole extractor (while preserving ``.part``) up to
two times. The final attempt continues at the available rate instead of treating
the heuristic as a hard failure.
"""

from __future__ import annotations

import base64
import json
import math
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QThread, Signal

from spica.anime.download_health import DownloadHealthMonitor, DownloadProgressSample
from spica.anime.models import (
    DownloadStatus,
    DownloadTerminalCause,
    DownloadTerminalOwner,
    DownloadTerminalResult,
    anime_dirname,
)
from spica.core.anime_events import AnimeReadyEvent
from spica.ports.media_player import MEDIA_EXTENSIONS
from spica.ports.torrent_client import TorrentCancelResult, TorrentClientError

_BVID_PART_RE = re.compile(r"^(BV[0-9A-Za-z]{10}):(\d{1,4})$")
_BTIH_RE = re.compile(r"^urn:btih:([0-9a-fA-F]{40})$")
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
        torrent_payload_b64: str | None = None,
        poll_seconds: float = 5.0,
        stall_timeout_minutes: float = 10.0,
        ytdlp_format: str = "bv*[height<=1080]+ba/b[height<=1080]",
        source_timeout_seconds: float = 15.0,
        ytdlp_min_rate_kib_per_second: float = 512.0,
        cookies_file: str = "",
        resume_task_id: str | None = None,
        resume_created_at: str | None = None,
        parent: Any = None,
        popen: Callable[..., Any] | None = None,
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.request_id = request_id
        self.episode_key = episode_key
        self.title = title
        self.locator = locator
        self.torrent_payload_b64 = torrent_payload_b64
        self.resume_task_id = resume_task_id
        self._resume_created_at = resume_created_at
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
        self._wall_clock = wall_clock if wall_clock is not None else time.time
        self._sleep = sleep if sleep is not None else time.sleep
        self._proc: Any = None
        # ONE lock owns both the process hand-off and the terminal-decision CAS.
        # Outcome/result stays on AnimeReadyEvent and is deliberately separate.
        self._state_lock = threading.Lock()
        self._terminal_owner = DownloadTerminalOwner.RUNNING

    # -- lifecycle -------------------------------------------------------------

    def cancel(self) -> None:
        """P1-9 exit path: stop polling (qbt task keeps running in the external
        service) / terminate the yt-dlp subprocess KEEPING its .part file."""
        with self._state_lock:
            claimed = self._terminal_owner is DownloadTerminalOwner.RUNNING
            if claimed:
                self._terminal_owner = DownloadTerminalOwner.SHUTDOWN_PRESERVE
            proc = self._proc
        # Manual/stall/completion already owns the decision; a later shutdown
        # may wait for it but must not overwrite or interrupt that owner.
        if claimed:
            self.requestInterruption()
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def request_download_cancel(self) -> bool:
        """Request a terminal user cancellation.

        Unlike ``cancel()`` (application shutdown), this deliberately does not
        request QThread interruption: ``run()`` must still emit a terminal
        ready event so the controller can erase pending state and release the
        single-flight slot. qBT deletion remains in the worker thread.
        """
        with self._state_lock:
            if self._terminal_owner is not DownloadTerminalOwner.RUNNING:
                return False
            self._terminal_owner = DownloadTerminalOwner.MANUAL_CANCEL
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        return True

    def force_kill(self) -> None:
        """Escalation for a terminate-resistant subprocess (controller calls it
        after a bounded wait)."""
        with self._state_lock:
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
        with self._state_lock:
            self._proc = proc
            return self._terminal_owner is not DownloadTerminalOwner.RUNNING

    def _clear_proc(self, proc: Any) -> None:
        with self._state_lock:
            if self._proc is proc:
                self._proc = None

    def _owner(self) -> DownloadTerminalOwner:
        with self._state_lock:
            return self._terminal_owner

    def _claim_stall_cancel(self) -> bool:
        with self._state_lock:
            if self._terminal_owner is not DownloadTerminalOwner.RUNNING:
                return False
            self._terminal_owner = DownloadTerminalOwner.STALL_CANCEL
            return True

    def _claim_completed(self) -> DownloadTerminalOwner:
        """Record completion where allowed; return the owner it follows."""
        with self._state_lock:
            owner = self._terminal_owner
            if owner in (
                DownloadTerminalOwner.RUNNING,
                DownloadTerminalOwner.STALL_CANCEL,
            ):
                self._terminal_owner = DownloadTerminalOwner.COMPLETED
            # MANUAL stays MANUAL: its accepted suppress semantic must survive.
            return owner

    def _shutdown_requested(self) -> bool:
        return self._owner() is DownloadTerminalOwner.SHUTDOWN_PRESERVE

    def _download_cancel_pending(self) -> bool:
        return self._owner() is DownloadTerminalOwner.MANUAL_CANCEL

    def _stop_requested(self) -> bool:
        return self._owner() in (
            DownloadTerminalOwner.MANUAL_CANCEL,
            DownloadTerminalOwner.SHUTDOWN_PRESERVE,
        )

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

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep in small slices so cancel/exit returns within ~_SLEEP_SLICE,
        never a full poll period (F3 -- shutdown's bounded wait must suffice)."""
        remaining = float(seconds)
        while remaining > 0 and not self._stop_requested():
            step = min(_SLEEP_SLICE, remaining)
            self._sleep(step)
            remaining -= step

    def run(self) -> None:  # QThread entry
        try:
            event = self.execute()
        except Exception as exc:  # noqa: BLE001 -- a worker must never crash the UI
            # Even an unexpected exception must compete on the same terminal
            # owner as every classified failure.  A valid locator/resume hash
            # is also the safe disambiguation key if manual stop already won.
            event = self._finalize_failure(
                exc,
                qbt_task_id=(
                    self.resume_task_id
                    or self._magnet_infohash(self.locator or "")
                ),
            )
        if (event is not None
                and event.terminal_result is not DownloadTerminalResult.PRESERVED
                and not self._shutdown_requested()):
            self.ready.emit(event)

    # -- core (synchronous; unit tests call this directly) ----------------------

    def execute(self) -> AnimeReadyEvent:
        """Run the download to one typed terminal outcome."""
        if self._shutdown_requested():
            return self._shutdown_preserved_event()
        if self.resume_task_id:
            return self._poll_qbt(self.resume_task_id, started=None)
        if self._download_cancel_pending():
            return self._manual_cancelled_without_qbt()
        loc = self.locator or ""
        if self.torrent_payload_b64 is not None:
            try:
                payload = base64.b64decode(
                    self.torrent_payload_b64, validate=True)
            except (ValueError, TypeError) as exc:
                return self._finalize_failure(
                    f"BAD_TORRENT_PAYLOAD: {exc}")
            expected_infohash = self._magnet_infohash(loc)
            if expected_infohash is None:
                return self._finalize_failure(
                    f"BAD_LOCATOR: {loc[:80]!r}")
            started = self._clock()
            try:
                task_id = self._torrent.add_torrent_bytes(
                    payload, expected_infohash=expected_infohash,
                    subfolder=self._subdir)
            except Exception as exc:  # noqa: BLE001 -- acknowledgement may be lost
                return self._terminal_after_qbt_add_error(
                    expected_infohash, started=started, error=exc)
            self.task_started.emit(self.request_id, task_id)
            return self._poll_qbt(task_id, started=started)
        if loc.startswith("magnet:?"):
            expected_infohash = self._magnet_infohash(loc)
            if expected_infohash is None:
                return self._finalize_failure(
                    f"BAD_LOCATOR: {loc[:80]!r}")
            started = self._clock()
            # Legacy events remain resumable even though new Mikan requests
            # carry verified torrent bytes with their original tracker tiers.
            try:
                task_id = self._torrent.add_magnet(
                    loc, subfolder=self._subdir)
            except Exception as exc:  # noqa: BLE001 -- acknowledgement may be lost
                return self._terminal_after_qbt_add_error(
                    expected_infohash, started=started, error=exc)
            self.task_started.emit(self.request_id, task_id)
            return self._poll_qbt(task_id, started=started)
        m = _BVID_PART_RE.match(loc)
        if m is not None:
            return self._run_ytdlp(m.group(1), int(m.group(2)))
        # never execute an unrecognized locator (whitelist, 铁律 #9)
        return self._finalize_failure(f"BAD_LOCATOR: {loc[:80]!r}")

    @staticmethod
    def _magnet_infohash(locator: str) -> str | None:
        if not locator.startswith("magnet:?"):
            return None
        for xt in urllib.parse.parse_qs(
                urllib.parse.urlsplit(locator).query).get("xt", []):
            match = _BTIH_RE.fullmatch(xt)
            if match is not None:
                return match.group(1).lower()
        return None

    def _terminal_after_qbt_add_error(
        self,
        expected_task_id: str,
        *,
        started: float,
        error: Exception,
    ) -> AnimeReadyEvent:
        """Preserve an accepted terminal decision when add acknowledgement is
        uncertain.  The known infohash is the only safe disambiguation key."""
        return self._finalize_failure(
            error, qbt_task_id=expected_task_id, started=started)

    def _finalize_failure(
        self,
        error: object,
        *,
        qbt_task_id: str | None = None,
        started: float | None = None,
    ) -> AnimeReadyEvent:
        """Linearize every failure against manual stop and shutdown.

        Error formatting deliberately happens before the CAS: a manual request
        accepted while an external exception is being classified must retain
        ownership.  Once FAILED wins the same lock, later manual requests are
        rejected instead of being accepted after a failure event was decided.
        """
        detail = str(error)
        with self._state_lock:
            owner = self._terminal_owner
            if owner is DownloadTerminalOwner.RUNNING:
                self._terminal_owner = DownloadTerminalOwner.FAILED
        if owner is DownloadTerminalOwner.SHUTDOWN_PRESERVE:
            return self._shutdown_preserved_event()
        if owner is DownloadTerminalOwner.MANUAL_CANCEL:
            if qbt_task_id is not None:
                return self._resolve_qbt_manual_cancel(
                    qbt_task_id, started=started)
            return self._manual_cancelled_without_qbt()
        return self._ready_event(
            error=detail,
            terminal_result=DownloadTerminalResult.FAILED,
            terminal_cause=DownloadTerminalCause.NORMAL,
        )

    # -- qbt lane ---------------------------------------------------------------

    @staticmethod
    def _iso_timestamp(value: str | None) -> float | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            timestamp = float(parsed.timestamp())
        except (TypeError, ValueError, OverflowError):
            return None
        return timestamp if math.isfinite(timestamp) and timestamp > 0 else None

    def _resume_inactivity_seconds(
        self,
        status: DownloadStatus | None,
    ) -> float:
        """Translate persisted wall-clock evidence into this process' monotonic
        time domain.  A valid qBT ``last_activity`` (Unix epoch seconds) is the
        sole anchor; pending creation is used only when that value is absent or
        malformed.  Future/skewed values are treated as fresh so suspect clock
        data can never trigger immediate destructive cleanup."""
        try:
            wall_now = float(self._wall_clock())
        except (TypeError, ValueError, OverflowError):
            return 0.0
        if not math.isfinite(wall_now):
            return 0.0
        raw_activity = (
            getattr(status, "last_activity_at", None)
            if status is not None else None
        )
        try:
            activity = float(raw_activity) if raw_activity is not None else None
        except (TypeError, ValueError, OverflowError):
            activity = None
        if (activity is not None and math.isfinite(activity)
                and activity > 0):
            if activity > wall_now:
                return 0.0
            return max(0.0, wall_now - activity)
        created_at = self._iso_timestamp(self._resume_created_at)
        if created_at is None:
            return 0.0
        if created_at > wall_now:
            return 0.0
        return max(0.0, wall_now - created_at)

    def _stall_cutoff_reached(self, inactivity_seconds: float) -> bool:
        # Poll sleeps are sliced into fractional seconds; tolerate only their
        # sub-microsecond floating-point drift while preserving the 599.9/600
        # boundary promised by configuration.
        return inactivity_seconds + 1e-6 >= self._stall_seconds

    def _poll_qbt(self, task_id: str, *, started: float | None) -> AnimeReadyEvent:
        # None means no trustworthy sample has been observed yet.  The first
        # value is a baseline, never proof of growth since this worker started.
        previous_progress: float | None = None
        last_change = self._clock()
        resume_baseline_pending = self.resume_task_id is not None
        if resume_baseline_pending:
            last_change -= self._resume_inactivity_seconds(None)
        api_error_streak = 0
        while True:
            if self._shutdown_requested():
                return self._shutdown_preserved_event()
            if self._download_cancel_pending():
                return self._resolve_qbt_manual_cancel(
                    task_id, started=started)
            try:
                st = self._torrent.status(task_id)
            except TorrentClientError as e:
                # A decision may have been accepted while status() was blocked.
                # Classify that owner before classifying the status failure.
                if self._shutdown_requested():
                    return self._shutdown_preserved_event()
                if self._download_cancel_pending():
                    return self._cancel_manual_qbt(task_id)
                if e.code == "AUTH_FAILED":       # credentials: never self-heals (F2)
                    return self._finalize_failure(
                        "qbittorrent 登录失败（AUTH_FAILED）："
                        "请检查 Web UI 用户名/密码配置",
                        qbt_task_id=task_id,
                        started=started,
                    )
                if e.code in _TRANSIENT_QBT:      # reconnect until hard cutoff
                    if self._stall_cutoff_reached(
                            self._clock() - last_change):
                        resolution = self._resolve_qbt_stall_timeout(
                            task_id, started=started,
                            previous_progress=previous_progress,
                            resume_baseline=resume_baseline_pending)
                        if isinstance(resolution, AnimeReadyEvent):
                            return resolution
                        now = self._clock()
                        if resume_baseline_pending:
                            last_change = (
                                now - self._resume_inactivity_seconds(
                                    resolution))
                        elif (previous_progress is not None
                              and resolution.progress
                              > previous_progress + 1e-6):
                            last_change = now
                        resume_baseline_pending = False
                        previous_progress = float(resolution.progress)
                        self._emit_qbt_progress(resolution)
                    self._interruptible_sleep(self._poll_seconds)
                    continue
                if e.code == "API_ERROR":         # bounded retry, then fail (F2)
                    api_error_streak += 1
                    if api_error_streak >= _MAX_API_ERROR_STREAK:
                        return self._finalize_failure(
                            f"qbittorrent 接口连续出错（API_ERROR×"
                            f"{api_error_streak}）：{e}",
                            qbt_task_id=task_id,
                            started=started,
                        )
                    self._interruptible_sleep(self._poll_seconds)
                    continue
                return self._finalize_failure(
                    f"下载任务丢失（qbittorrent: {e.code}）",
                    qbt_task_id=task_id,
                    started=started,
                )
            if self._shutdown_requested():
                return self._shutdown_preserved_event()
            if self._download_cancel_pending():
                if st.is_done:
                    return self._completed_qbt_event(st, started=started)
                return self._cancel_manual_qbt(task_id)
            api_error_streak = 0
            if resume_baseline_pending:
                # The first observed value after a restart is a baseline, not
                # proof that progress happened since this app process started.
                last_change = (
                    self._clock() - self._resume_inactivity_seconds(st))
                resume_baseline_pending = False
            elif (previous_progress is not None
                  and st.progress > previous_progress + 1e-6):
                last_change = self._clock()
            previous_progress = float(st.progress)
            self._emit_qbt_progress(st)
            if st.is_done:
                return self._completed_qbt_event(st, started=started)
            if self._stall_cutoff_reached(self._clock() - last_change):
                resolution = self._resolve_qbt_stall_timeout(
                    task_id, started=started,
                    previous_progress=previous_progress)
                if isinstance(resolution, AnimeReadyEvent):
                    return resolution
                previous_progress = float(resolution.progress)
                last_change = self._clock()
                self._emit_qbt_progress(resolution)
            if st.state == "error":
                return self._finalize_failure(
                    f"下载出错：{st.error or 'qbittorrent errored'}",
                    qbt_task_id=task_id,
                    started=started,
                )
            self._interruptible_sleep(self._poll_seconds)

    def _resolve_qbt_stall_timeout(
        self,
        task_id: str,
        *,
        started: float | None,
        previous_progress: float | None,
        resume_baseline: bool = False,
    ) -> AnimeReadyEvent | DownloadStatus:
        """Linearize the cutoff on one final category-scoped status read.

        Completion wins when it is observed here.  Fresh progress resets the
        dry-period clock in the caller.  Otherwise this is the sole path that
        removes a qbt task because of runtime health; ordinary ``cancel()``
        remains the app-shutdown/local-polling path and never reaches qbt.
        """
        try:
            final_status = self._torrent.status(task_id)
        except TorrentClientError as exc:
            if self._shutdown_requested():
                return self._shutdown_preserved_event()
            if self._download_cancel_pending():
                return self._cancel_manual_qbt(task_id)
            return self._cancel_stalled_qbt(task_id)

        if self._shutdown_requested():
            return self._shutdown_preserved_event()
        if self._download_cancel_pending():
            if final_status.is_done:
                return self._completed_qbt_event(
                    final_status, started=started)
            return self._cancel_manual_qbt(task_id)

        if final_status.is_done:
            return self._completed_qbt_event(
                final_status, started=started)
        if resume_baseline:
            if not self._stall_cutoff_reached(
                    self._resume_inactivity_seconds(final_status)):
                return final_status
            return self._cancel_stalled_qbt(task_id)
        if (previous_progress is not None
                and final_status.progress > previous_progress + 1e-6):
            return final_status
        return self._cancel_stalled_qbt(task_id)

    def _cancel_stalled_qbt(self, task_id: str) -> AnimeReadyEvent:
        if not self._claim_stall_cancel():
            if self._shutdown_requested():
                return self._shutdown_preserved_event()
            if self._download_cancel_pending():
                return self._cancel_manual_qbt(task_id)
            return self._ready_event(
                error="下载终态已由其他路径认领",
                terminal_result=DownloadTerminalResult.UNCONFIRMED,
                terminal_cause=DownloadTerminalCause.STALL,
            )
        minutes = self._stall_seconds / 60.0
        minute_text = f"{minutes:g}"
        return self._cancel_qbt_task(
            task_id,
            success_detail=(f"连续 {minute_text} 分钟没有进度，"
                            "已让 qBittorrent 移除任务并请求删除未完成数据"),
            missing_detail=(f"连续 {minute_text} 分钟没有进度，"
                            "qBittorrent 任务已不存在，已结束本次下载"),
            unconfirmed_detail=(f"连续 {minute_text} 分钟没有进度，"
                                "已停止等待，但 qBittorrent 未确认取消，"
                                "后台任务可能仍在"),
            expected_owner=DownloadTerminalOwner.STALL_CANCEL,
        )

    def _resolve_qbt_manual_cancel(
        self,
        task_id: str,
        *,
        started: float | None,
    ) -> AnimeReadyEvent:
        """One final status read gives an already-completed task precedence."""
        try:
            final_status = self._torrent.status(task_id)
        except TorrentClientError:
            return self._cancel_manual_qbt(task_id)
        except Exception:  # noqa: BLE001 -- cancellation still gets one try
            return self._cancel_manual_qbt(task_id)
        if final_status.is_done:
            elapsed = (
                self._clock() - started if started is not None else None)
            return self._ready_event(
                save_path=final_status.save_path,
                elapsed_seconds=elapsed,
                terminal_cause=DownloadTerminalCause.MANUAL,
            )
        return self._cancel_manual_qbt(task_id)

    def _cancel_manual_qbt(self, task_id: str) -> AnimeReadyEvent:
        return self._cancel_qbt_task(
            task_id,
            success_detail=(
                "已让 qBittorrent 移除任务并请求删除未完成数据"),
            missing_detail="qBittorrent 任务已不存在，已结束本次下载",
            unconfirmed_detail=("已停止本地等待，但 qBittorrent 未确认取消，"
                                "后台任务可能仍在"),
            terminal_cause=DownloadTerminalCause.MANUAL,
            expected_owner=DownloadTerminalOwner.MANUAL_CANCEL,
        )

    def _cancel_qbt_task(
        self,
        task_id: str,
        *,
        success_detail: str,
        missing_detail: str,
        unconfirmed_detail: str,
        terminal_cause: DownloadTerminalCause = DownloadTerminalCause.STALL,
        expected_owner: DownloadTerminalOwner,
    ) -> AnimeReadyEvent:
        # Last worker-side owner check immediately before the destructive port
        # call. The adapter independently performs freeze/re-read checks too.
        if self._owner() is not expected_owner:
            if self._shutdown_requested():
                return self._shutdown_preserved_event()
            return self._ready_event(
                error="取消所有权在执行前发生变化，未操作 qBittorrent",
                terminal_result=DownloadTerminalResult.UNCONFIRMED,
                terminal_cause=terminal_cause,
            )
        try:
            outcome = self._torrent.cancel(task_id)
        except TorrentClientError as exc:
            return self._ready_event(
                error=(f"{unconfirmed_detail}（{exc.code}: {exc}）"),
                terminal_result=DownloadTerminalResult.UNCONFIRMED,
                terminal_cause=terminal_cause,
            )
        except Exception as exc:  # noqa: BLE001 -- release local single-flight
            return self._ready_event(
                error=(f"{unconfirmed_detail}"
                       f"（{type(exc).__name__}: {exc}）"),
                terminal_result=DownloadTerminalResult.UNCONFIRMED,
                terminal_cause=terminal_cause,
            )
        result = getattr(outcome, "result", None)
        if result is TorrentCancelResult.ALREADY_COMPLETED:
            return self._completed_qbt_event(
                DownloadStatus(
                    task_id=task_id,
                    state="completed",
                    progress=1.0,
                    save_path=getattr(outcome, "save_path", None),
                ),
                started=None,
            )
        if result is TorrentCancelResult.MISSING:
            return self._ready_event(
                error=missing_detail,
                terminal_result=DownloadTerminalResult.CANCELLED,
                terminal_cause=terminal_cause,
            )
        if result is not TorrentCancelResult.CANCELLED:
            return self._ready_event(
                error=(f"{unconfirmed_detail}"
                       "（qBittorrent 返回了未知取消结果）"),
                terminal_result=DownloadTerminalResult.UNCONFIRMED,
                terminal_cause=terminal_cause,
            )
        return self._ready_event(
            error=success_detail,
            terminal_result=DownloadTerminalResult.CANCELLED,
            terminal_cause=terminal_cause,
        )

    def _manual_cancelled_without_qbt(self) -> AnimeReadyEvent:
        if _BVID_PART_RE.match(self.locator or "") is not None:
            detail = "已停止 B 站下载，临时文件已保留供安全续传"
        else:
            detail = "已停止下载（任务尚未提交给 qBittorrent）"
        return self._ready_event(
            error=detail,
            terminal_result=DownloadTerminalResult.CANCELLED,
            terminal_cause=DownloadTerminalCause.MANUAL,
        )

    def _completed_qbt_event(
        self,
        status: DownloadStatus,
        *,
        started: float | None,
    ) -> AnimeReadyEvent:
        owner = self._claim_completed()
        if owner is DownloadTerminalOwner.SHUTDOWN_PRESERVE:
            return self._shutdown_preserved_event()
        cause = {
            DownloadTerminalOwner.MANUAL_CANCEL: DownloadTerminalCause.MANUAL,
            DownloadTerminalOwner.STALL_CANCEL: DownloadTerminalCause.STALL,
        }.get(owner, DownloadTerminalCause.NORMAL)
        elapsed = self._clock() - started if started is not None else None
        return self._ready_event(
            save_path=status.save_path,
            elapsed_seconds=elapsed,
            terminal_result=DownloadTerminalResult.COMPLETED,
            terminal_cause=cause,
        )

    def _shutdown_preserved_event(self) -> AnimeReadyEvent:
        return self._ready_event(
            error="应用正在退出；已停止本地等待并保留后台下载数据",
            terminal_result=DownloadTerminalResult.PRESERVED,
            terminal_cause=DownloadTerminalCause.SHUTDOWN,
        )

    def _emit_qbt_progress(self, status: DownloadStatus) -> None:
        phase = "metadata" if status.state == "metadata" else "downloading"
        self.progress.emit(
            self.request_id, float(status.progress), phase)

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

    def _run_ytdlp(self, bvid: str, part: int) -> AnimeReadyEvent:
        overall_started = self._clock()
        argv = self._ytdlp_argv(bvid, part)
        reconnects = 0
        while not self._shutdown_requested():
            if self._download_cancel_pending():
                return self._manual_cancelled_without_qbt()
            attempt = self._run_ytdlp_attempt(
                argv, stop_on_low_speed=reconnects < _MAX_YTDLP_RECONNECTS)
            if attempt.cancelled:
                if self._download_cancel_pending():
                    return self._manual_cancelled_without_qbt()
                return self._shutdown_preserved_event()
            if self._shutdown_requested():
                return self._shutdown_preserved_event()
            if attempt.lifecycle_error is not None:
                return self._finalize_failure(attempt.lifecycle_error)
            if attempt.low_speed:
                reconnects += 1
                self.reconnecting.emit(
                    self.request_id, reconnects, _MAX_YTDLP_RECONNECTS,
                    "low_speed")
                self._interruptible_sleep(_YTDLP_RECONNECT_DELAY_SECONDS)
                continue
            if attempt.returncode != 0:
                if self._download_cancel_pending():
                    return self._manual_cancelled_without_qbt()
                failure = _analyze_ytdlp_failure("\n".join(attempt.tail))
                if (failure.kind is _YtDlpFailureKind.NETWORK
                        and reconnects < _MAX_YTDLP_RECONNECTS):
                    reconnects += 1
                    self.reconnecting.emit(
                        self.request_id, reconnects, _MAX_YTDLP_RECONNECTS,
                        "network")
                    self._interruptible_sleep(_YTDLP_RECONNECT_DELAY_SECONDS)
                    continue
                return self._finalize_failure(failure.message)
            checked = self._validated_output(attempt.final_path)
            if checked is None:
                return self._finalize_failure(
                    "yt-dlp 结束但没有产出可信的输出文件路径")
            elapsed = self._clock() - overall_started
            self.progress.emit(self.request_id, 1.0, "downloading")
            owner = self._claim_completed()
            if owner is DownloadTerminalOwner.SHUTDOWN_PRESERVE:
                return self._shutdown_preserved_event()
            cause = (
                DownloadTerminalCause.MANUAL
                if owner is DownloadTerminalOwner.MANUAL_CANCEL
                else DownloadTerminalCause.NORMAL)
            return self._ready_event(
                save_path=checked, elapsed_seconds=elapsed,
                terminal_cause=cause)
        return self._shutdown_preserved_event()

    def _run_ytdlp_attempt(
        self,
        argv: list[str],
        *,
        stop_on_low_speed: bool,
    ) -> _YtDlpAttemptResult:
        if self._stop_requested():
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
                if ((cancel_seen_on_adopt or self._stop_requested())
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
                     error: str | None = None,
                     terminal_result: DownloadTerminalResult | None = None,
                     terminal_cause: DownloadTerminalCause = (
                         DownloadTerminalCause.NORMAL)) -> AnimeReadyEvent:
        if error is not None and terminal_result is None:
            raise AssertionError(
                "failure events must pass through _finalize_failure")
        result = terminal_result or DownloadTerminalResult.COMPLETED
        return AnimeReadyEvent(
            request_id=self.request_id, episode_key=self.episode_key,
            save_path=save_path, elapsed_seconds=elapsed_seconds, error=error,
            terminal_result=result, terminal_cause=terminal_cause)
