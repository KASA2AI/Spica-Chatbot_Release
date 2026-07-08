"""Concurrency strategy for the streaming turn (core C2).

The orchestrator fans each play unit out across three lanes -- visual
classification, TTS synthesis, and the finalize join. C2 makes that fan-out an
*injected policy* instead of three hard-coded ThreadPoolExecutors, so:

- streaming injects ``Threaded`` -- the original pools, behaviour-identical;
- the synchronous path injects ``Inline`` -- each lane runs immediately in the
  caller's thread, so a turn folds to a response payload with no thread pools.

The three lanes are pinned into the protocol on purpose: TTS being a *serial*
lane (``max_workers=1``) is product behaviour (one voice at a time), not an
implementation detail, so it can't be widened by accident.

Pure: no ``agent`` import, Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Protocol, TypeVar

T = TypeVar("T")


class ExecStrategy(Protocol):
    """How a turn submits its three per-unit lanes; each returns a Future."""

    def submit_visual(self, fn: Callable[[], T]) -> "Future[T]": ...
    def submit_tts(self, fn: Callable[[], T]) -> "Future[T]": ...        # serial lane
    def submit_finalize(self, fn: Callable[[], T]) -> "Future[T]": ...
    def shutdown(self) -> None: ...


def _run_now(fn: Callable[[], T]) -> "Future[T]":
    """Run ``fn`` now and return an already-resolved Future.

    Exceptions are propagated via ``set_exception`` (NOT swallowed), so an Inline
    turn fails exactly where a Threaded one would -- when the Future is awaited.
    """
    future: "Future[T]" = Future()
    try:
        future.set_result(fn())
    except BaseException as exc:  # noqa: BLE001 -- mirror Threaded: surface on .result()
        future.set_exception(exc)
    return future


class Inline:
    """Synchronous strategy: every lane runs now, in the calling thread."""

    def submit_visual(self, fn: Callable[[], T]) -> "Future[T]":
        return _run_now(fn)

    def submit_tts(self, fn: Callable[[], T]) -> "Future[T]":
        return _run_now(fn)

    def submit_finalize(self, fn: Callable[[], T]) -> "Future[T]":
        return _run_now(fn)

    def shutdown(self) -> None:
        return None


class Threaded:
    """Threaded strategy: the orchestrator's original three pools.

    visual is parallel (``visual_workers``), TTS is serial (1), finalize joins on
    a small pool (4) -- the exact worker counts of the pre-C2 inline pools, so
    streaming behaviour is unchanged.
    """

    def __init__(self, visual_workers: int = 2) -> None:
        self._visual = ThreadPoolExecutor(max_workers=max(1, visual_workers))
        self._tts = ThreadPoolExecutor(max_workers=1)        # serial: one voice at a time
        self._finalize = ThreadPoolExecutor(max_workers=4)

    def submit_visual(self, fn: Callable[[], T]) -> "Future[T]":
        return self._visual.submit(fn)

    def submit_tts(self, fn: Callable[[], T]) -> "Future[T]":
        return self._tts.submit(fn)

    def submit_finalize(self, fn: Callable[[], T]) -> "Future[T]":
        return self._finalize.submit(fn)

    def shutdown(self) -> None:
        # Match the orchestrator's old finally: don't block, don't cancel queued.
        self._visual.shutdown(wait=False, cancel_futures=False)
        self._tts.shutdown(wait=False, cancel_futures=False)
        self._finalize.shutdown(wait=False, cancel_futures=False)
