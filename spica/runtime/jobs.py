"""Background job runner (C6).

``JobRunner`` is the injected policy for fire-and-forget work that must NOT block
the turn's hot path -- specifically the long-term memory commit
(``save_stream_memory``). The recent-memory append is deliberately NOT a job: the
next turn's recent context needs it before this turn's ``done``, so it stays
synchronous.

``InlineJobRunner`` runs the job synchronously in the calling thread (tests + the
sync path, so cross-turn assertions still see the commit). ``ThreadJobRunner`` runs
it on a daemon thread (streaming, so ``done`` is emitted without waiting); the
streaming orchestrator drains it before the stream closes, so no commit thread
leaks past the turn.

Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Protocol

logger = logging.getLogger(__name__)


class JobRunner(Protocol):
    def submit(self, fn: Callable[[], None]) -> None: ...  # fire-and-forget
    def drain(self, timeout: float | None = None) -> None: ...


class InlineJobRunner:
    """Runs each job synchronously in the calling thread (sync path / tests)."""

    def submit(self, fn: Callable[[], None]) -> None:
        fn()

    def drain(self, timeout: float | None = None) -> None:
        return None


class ThreadJobRunner:
    """Runs each job on its own daemon thread; ``drain`` joins them.

    There is one job per turn (the long-term commit), so this stays a simple
    thread-per-submit. The SQLite store opens a fresh connection per call, so
    committing off the producer thread is safe.
    """

    def __init__(self) -> None:
        self._threads: list[threading.Thread] = []

    def submit(self, fn: Callable[[], None]) -> None:
        def _run() -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001 -- a fire-and-forget job (the memory
                # commit) must never die with only a bare-thread stderr traceback.
                logger.exception("background job failed")

        thread = threading.Thread(target=_run, daemon=True)
        self._threads.append(thread)
        thread.start()

    def drain(self, timeout: float | None = None) -> None:
        for thread in self._threads:
            thread.join(timeout)
        self._threads = [t for t in self._threads if t.is_alive()]
