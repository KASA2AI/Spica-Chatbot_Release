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

Stall detection is progress-driven: ``stalled`` fires once per dry spell (reset
when progress moves). On the yt-dlp lane it is line-driven -- a subprocess that
goes fully silent is only caught by cancel/exit (v1; qbt is the primary lane).
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QThread, Signal

from spica.core.anime_events import AnimeReadyEvent
from spica.ports.media_player import MEDIA_EXTENSIONS
from spica.ports.torrent_client import TorrentClientError

_BVID_PART_RE = re.compile(r"^(BV[0-9A-Za-z]{10}):(\d{1,4})$")
_PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
# The ONLY long-lived transient (P1-10): the qbt service being down/restarting
# means reconnect and keep polling. AUTH_FAILED is a credential problem (the
# adapter already re-logins on 403; what reaches us never self-heals) -> fail
# fast. API_ERROR gets a BOUNDED retry streak, then fails (F2).
_TRANSIENT_QBT = frozenset({"UNREACHABLE"})
_MAX_API_ERROR_STREAK = 12
# interruptible-sleep slice (F3): cancel/exit must never wait a full poll period
_SLEEP_SLICE = 0.1


def _classify_ytdlp_failure(tail: str) -> str:
    """Map a yt-dlp stderr tail onto the plan §7 user-facing categories."""
    low = tail.lower()
    if "充电" in tail or "大会员" in tail:
        return "这集是充电/大会员专属，当前账号看不了"
    if "cookies" in low or "login" in low or "登录" in tail or "account" in low:
        return "需要登录 B 站（cookie 缺失或已过期）"
    return f"yt-dlp 下载失败：{tail[-300:].strip() or '未知错误'}"


class AnimeDownloadWorker(QThread):
    # UI-internal signals (P2-19): worker thread -> controller (GUI thread).
    progress = Signal(str, float, str)   # request_id, 0..1, phase text
    ready = Signal(object)               # AnimeReadyEvent (success OR error)
    stalled = Signal(str, float)         # request_id, minutes without progress
    task_started = Signal(str, str)      # request_id, qbt task_id (btih)

    def __init__(
        self,
        *,
        request_id: str,
        episode_key: str,
        title: str,
        locator: str,
        torrent: Any,
        download_dir: str,
        poll_seconds: float = 5.0,
        stall_timeout_minutes: float = 30.0,
        ytdlp_format: str = "bv*[height<=1080]+ba/b[height<=1080]",
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
        self._download_dir = str(Path(download_dir).expanduser().resolve())
        self._poll_seconds = max(0.5, float(poll_seconds))
        self._stall_seconds = max(60.0, float(stall_timeout_minutes) * 60.0)
        self._ytdlp_format = ytdlp_format
        self._cookies_file = cookies_file
        self._popen = popen if popen is not None else subprocess.Popen
        self._clock = clock if clock is not None else time.monotonic
        self._sleep = sleep if sleep is not None else time.sleep
        self._proc: Any = None
        self._cancelled = False

    # -- lifecycle -------------------------------------------------------------

    def cancel(self) -> None:
        """P1-9 exit path: stop polling (qbt task keeps running in the external
        service) / terminate the yt-dlp subprocess KEEPING its .part file."""
        self._cancelled = True
        self.requestInterruption()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def force_kill(self) -> None:
        """Escalation for a terminate-resistant subprocess (controller calls it
        after a bounded wait)."""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def _interrupted(self) -> bool:
        # requestInterruption is a no-op while the thread is NOT running (e.g.
        # synchronous execute() in tests) -> the explicit flag must also count.
        return self._cancelled or self.isInterruptionRequested()

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
        if event is not None and not self._cancelled:
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
            task_id = self._torrent.add_magnet(loc)   # magnet-only port (P0-3)
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
            "--newline", "--no-warnings",
            "-f", self._ytdlp_format,
            "-I", str(part),
            "-P", self._download_dir,
            "-o", "%(title)s [P%(playlist_index)02d].%(ext)s",
            "--print", "after_move:filepath", "--no-simulate",
        ]
        if self._cookies_file and Path(self._cookies_file).is_file():
            argv += ["--cookies", self._cookies_file]
        argv.append(f"https://www.bilibili.com/video/{bvid}")
        return argv

    def _run_ytdlp(self, bvid: str, part: int) -> AnimeReadyEvent | None:
        started = self._clock()
        argv = self._ytdlp_argv(bvid, part)
        # stderr merged into stdout: a single streamed pipe cannot deadlock, and
        # the tail doubles as the error-classification input.
        self._proc = self._popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            shell=False, text=True, encoding="utf-8", errors="replace")
        tail: deque[str] = deque(maxlen=40)
        final_path: str | None = None
        stdout = self._proc.stdout
        if stdout is not None:
            for line in stdout:
                line = line.rstrip("\n")
                if line:
                    tail.append(line)
                pm = _PROGRESS_RE.search(line)
                if pm is not None:
                    self.progress.emit(self.request_id,
                                       float(pm.group(1)) / 100.0, "downloading")
                elif line.strip().startswith(("/", "\\")) or (
                        len(line) > 2 and line[1] == ":"):
                    final_path = line.strip()   # --print after_move:filepath
                if self._interrupted():
                    break
        rc = self._proc.wait()
        if self._interrupted():
            return None                       # .part kept -- resume on re-request
        if rc != 0:
            return self._ready_event(error=_classify_ytdlp_failure("\n".join(tail)))
        checked = self._validated_output(final_path)
        if checked is None:
            return self._ready_event(
                error="yt-dlp 结束但没有产出可信的输出文件路径")
        elapsed = self._clock() - started
        self.progress.emit(self.request_id, 1.0, "downloading")
        return self._ready_event(save_path=checked, elapsed_seconds=elapsed)

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
