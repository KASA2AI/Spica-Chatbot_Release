from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any


def now_ms() -> float:
    return time.perf_counter() * 1000


def elapsed_ms(start_ms: float) -> float:
    return round(now_ms() - start_ms, 2)


def log_timing(step: str, duration_ms: float, **fields: Any) -> None:
    details = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    suffix = f" {details}" if details else ""
    print(f"[TIMING] step={step} duration_ms={duration_ms:.2f}{suffix}", flush=True)


@contextmanager
def timed_step(step: str, **fields: Any):
    start = now_ms()
    try:
        yield
    finally:
        log_timing(step, elapsed_ms(start), **fields)
