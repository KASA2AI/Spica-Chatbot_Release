"""Ordered release primitive for the streaming turn (Phase 6C / core C1).

A ``Sequencer`` turns "completed in any order, released strictly 0,1,2,..." into
a tiny pure data structure, replacing the orchestrator's hand-rolled
``ready_units`` dict + ``next_emit`` + ``ready_lock`` reorder buffer.

It is an ORDERING primitive, not a concurrency primitive. The contract is
**single-consumer**: exactly one thread (the producer main loop) calls
``complete``. Concurrency is somebody else's job -- workers finish in any order
and hand their ``(index, payload)`` to the consumer (via a queue); the consumer
feeds them to ``complete`` and emits the returned in-order batch. Because only
one thread ever touches it, this class does no locking.

Pure: no threading, no I/O, no ``agent`` import. Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from typing import Generic, TypeVar

T = TypeVar("T")


class Sequencer(Generic[T]):
    """Release values by contiguous index, regardless of completion order.

    ``complete(index, value)`` records a finished item and returns the longest
    run of values that is now releasable in order, starting at the next expected
    index. Items completed ahead of a gap are buffered until the gap fills::

        seq = Sequencer()
        seq.complete(2, "c")  # -> []          (waiting for 0, 1)
        seq.complete(0, "a")  # -> ["a"]       (1 still missing)
        seq.complete(1, "b")  # -> ["b", "c"]  (gap filled, 2 flushes too)
    """

    def __init__(self, start: int = 0) -> None:
        self._next = start
        self._done: dict[int, T] = {}

    def complete(self, index: int, value: T) -> list[T]:
        """Record item ``index`` and return the values now releasable in order.

        Raises ``ValueError`` for a duplicate or already-released (late) index --
        the single-consumer contract means each index is completed exactly once.
        """
        if index in self._done or index < self._next:
            raise ValueError(f"duplicate/late index {index} (next={self._next})")
        self._done[index] = value
        released: list[T] = []
        while self._next in self._done:
            released.append(self._done.pop(self._next))
            self._next += 1
        return released

    @property
    def pending(self) -> int:
        """How many completed-but-not-yet-released items are buffered."""
        return len(self._done)

    @property
    def next_index(self) -> int:
        """The index the sequencer is currently waiting to release."""
        return self._next
