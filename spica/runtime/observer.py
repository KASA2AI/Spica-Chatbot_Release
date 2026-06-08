"""Turn observability (C5).

``TurnObserver`` pulls timing + structured logging out of the stage logic: the
turn/stage layer calls ``deps.observer.span/mark/...`` instead of touching
``ctx.timing`` or ``log_timing`` directly. That makes observability a cross-cutting
concern the host can swap (silent in tests, structured in prod) without the stages
knowing.

``DefaultTurnObserver`` is a thin, thread-safe facade over a per-turn timing
*sink* -- the turn passes ``ctx.timing`` as the sink, so ``done.timing`` /
``response_payload["timing"]`` are exactly the same dict as before; the observer
just becomes the single write path (C5 Option X). It also re-emits the same
structured log lines through the injected logger (``services.logger or
log_timing``), so logging behaviour is unchanged.

``NoopTurnObserver`` records nothing -- the ``TurnDeps`` default until a turn
injects a real observer (the per-turn instance is wired in by the turn entry via
``dataclasses.replace``).

INVARIANT (N4-observe, C5): the turn/stage layer under ``spica/runtime`` must not
call ``log_timing`` directly -- it routes through the observer. Adapter-internal
diagnostics (``spica/adapters/*``) are exempt. Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator, Protocol

from common.timing import elapsed_ms, log_timing, now_ms


class TurnObserver(Protocol):
    """Timing/logging sink for one turn.

    ``span`` times a block (stores ``{name}_ms`` + logs); ``mark`` / ``mark_once``
    / ``bump`` store a timing value (set / keep-first / accumulate, no log);
    ``event`` emits a structured diagnostic log line (no store); ``snapshot``
    returns the accumulated timing for ``done.timing``.
    """

    def span(self, name: str, **fields: Any) -> Any: ...  # AbstractContextManager[None]
    def mark(self, name: str, value: Any) -> None: ...
    def mark_once(self, name: str, value: Any) -> None: ...
    def bump(self, name: str, delta: float) -> None: ...
    def event(self, name: str, value: float = 0.0, **fields: Any) -> None: ...
    def snapshot(self) -> dict[str, Any]: ...


class DefaultTurnObserver:
    """Thread-safe facade over a per-turn timing sink + a structured logger.

    The sink is the turn's ``ctx.timing`` dict; all writes go through here so the
    stages never touch it directly. The lock makes the concurrent visual/TTS job
    threads' ``mark_once`` / ``bump`` safe -- the role the old ``timing_lock``
    closure played.
    """

    def __init__(self, sink: dict[str, Any] | None = None, logger: Any = None) -> None:
        self._sink: dict[str, Any] = sink if sink is not None else {}
        self._log = logger or log_timing
        self._lock = threading.Lock()

    @contextmanager
    def span(self, name: str, **fields: Any) -> Iterator[None]:
        start = now_ms()
        try:
            yield
        finally:
            duration = elapsed_ms(start)
            with self._lock:
                self._sink[f"{name}_ms"] = duration
            self._log(name, duration, **fields)

    def mark(self, name: str, value: Any) -> None:
        with self._lock:
            self._sink[name] = value

    def mark_once(self, name: str, value: Any) -> None:
        with self._lock:
            self._sink.setdefault(name, value)

    def bump(self, name: str, delta: float) -> None:
        with self._lock:
            self._sink[name] = self._sink.get(name, 0) + delta

    def event(self, name: str, value: float = 0.0, **fields: Any) -> None:
        self._log(name, value, **fields)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._sink)


class NoopTurnObserver:
    """Records nothing. The ``TurnDeps`` default until a turn injects a real one."""

    @contextmanager
    def span(self, name: str, **fields: Any) -> Iterator[None]:
        yield

    def mark(self, name: str, value: Any) -> None:
        return None

    def mark_once(self, name: str, value: Any) -> None:
        return None

    def bump(self, name: str, delta: float) -> None:
        return None

    def event(self, name: str, value: float = 0.0, **fields: Any) -> None:
        return None

    def snapshot(self) -> dict[str, Any]:
        return {}
