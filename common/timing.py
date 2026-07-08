from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any

_timing_logger = logging.getLogger("common.timing")


def now_ms() -> float:
    return time.perf_counter() * 1000


def elapsed_ms(start_ms: float) -> float:
    return round(now_ms() - start_ms, 2)


def log_timing(step: str, duration_ms: float, **fields: Any) -> None:
    # Historical verification scaffolding (platform phases): same format, now at
    # DEBUG -- quiet by default, set the logging level to DEBUG to get the full
    # timing trace back when profiling.
    details = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    suffix = f" {details}" if details else ""
    _timing_logger.debug("[TIMING] step=%s duration_ms=%.2f%s", step, duration_ms, suffix)


@contextmanager
def timed_step(step: str, **fields: Any):
    start = now_ms()
    try:
        yield
    finally:
        log_timing(step, elapsed_ms(start), **fields)
