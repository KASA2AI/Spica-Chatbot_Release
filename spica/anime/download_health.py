"""Download-health policy for resumable yt-dlp attempts.

The policy is Qt-free.  It observes machine-readable byte counters and decides
when one HTTP stream has remained below the configured floor for a complete
window.  Process restarts and user-facing state remain the worker's job.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class DownloadProgressSample:
    observed_at: float
    status: str
    downloaded_bytes: int | None
    format_id: str | None = None
    tmpfilename: str | None = None


class DownloadHealthMonitor:
    """Detect sustained low throughput without relying on yt-dlp's avg speed."""

    def __init__(self, *, min_rate_bytes_per_second: float,
                 window_seconds: float) -> None:
        self._min_rate = max(0.0, float(min_rate_bytes_per_second))
        self._window = max(0.1, float(window_seconds))
        self._stream_key: tuple[str, str] | None = None
        self._last_sample: tuple[float, int] | None = None
        self._continuous_slow_seconds = 0.0

    def reset(self) -> None:
        self._stream_key = None
        self._last_sample = None
        self._continuous_slow_seconds = 0.0

    def _start_stream(self, key: tuple[str, str], observed_at: float,
                      downloaded_bytes: int) -> None:
        self._stream_key = key
        self._last_sample = (observed_at, downloaded_bytes)
        self._continuous_slow_seconds = 0.0

    def observe(self, sample: DownloadProgressSample) -> bool:
        """Return True once the current stream is slow for a full window."""
        if self._min_rate <= 0 or sample.status != "downloading":
            self.reset()
            return False
        if (not math.isfinite(sample.observed_at)
                or sample.downloaded_bytes is None
                or sample.downloaded_bytes < 0):
            self.reset()
            return False

        key = (sample.format_id or "", sample.tmpfilename or "")
        now = float(sample.observed_at)
        downloaded = int(sample.downloaded_bytes)
        if key != self._stream_key or self._last_sample is None:
            self._start_stream(key, now, downloaded)
            return False

        previous_at, previous_bytes = self._last_sample
        elapsed = now - previous_at
        if elapsed <= 0 or downloaded < previous_bytes:
            self._start_stream(key, now, downloaded)
            return False

        self._last_sample = (now, downloaded)
        interval_rate = (downloaded - previous_bytes) / elapsed
        if interval_rate >= self._min_rate:
            self._continuous_slow_seconds = 0.0
            return False

        self._continuous_slow_seconds += elapsed
        return self._continuous_slow_seconds >= self._window
