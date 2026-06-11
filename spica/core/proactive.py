"""Proactive turn initiation (P3) -- the system-side "she speaks first" entry.

MODE-AGNOSTIC by design review: the request carries directive / source /
conversation_id / policy / ttl and NOTHING domain-specific -- the only thing a
domain (song report, galgame tease, video commentary) contributes is the
directive TEXT. The arbiter is a pure policy class over injected callables; it
knows nothing about UIs, songs or games. A system turn rides the ONE dialogue
path (CLAUDE.md #3): the directive is framed by ``compose_system_directive_message``
and goes through ``ChatEngine.stream_voice`` with ``interaction_mode="system"``
(the existing typed channel; the galgame gate set the precedent) -- run_turn /
orchestrator / stages never fork.

v1 arbitration = drop_if_busy only: a busy conversation (her speech, a song, a
user recording in flight) silently drops the request with a debug log -- a
proactive remark is disposable, and the user's next message always preempts via
the UI's stop_current anyway. ``queue_latest`` + ttl are reserved fields for
P5's tease policies. The ``VoiceInputGate`` is the full-duplex hook seam: v1
wires the null gate (zero behaviour); future AEC / input-filter work plugs in
here without touching the initiator or any domain.

Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProactiveTurnRequest:
    directive: str  # the system event for her to react to (domain-authored TEXT)
    source: str = ""  # telemetry label only ("song"/"galgame"/"video"); zero branching
    conversation_id: str | None = None  # None -> the default chat namespace
    policy: str = "drop_if_busy"  # v1 implements drop_if_busy; "queue_latest" reserved (P5)
    ttl_seconds: float | None = None  # staleness bound for the reserved queue policy


@runtime_checkable
class VoiceInputGate(Protocol):
    """Full-duplex hook seam: called around her system-initiated speech."""

    def before_system_speech(self) -> None: ...

    def after_system_speech(self) -> None: ...


class NullInputGate:
    """v1: zero behaviour -- the seam exists, nothing plugs in yet."""

    def before_system_speech(self) -> None:
        return None

    def after_system_speech(self) -> None:
        return None


def compose_system_directive_message(directive: str) -> str:
    """Frame a directive as a SYSTEM event message. The framing lives in the
    message text (single-sourced here, shared by the engine entry and the UI),
    so prompt_builder stays untouched and the recent-memory record is
    self-identifying -- it never impersonates the interlocutor."""
    return (
        f"【系统事件，不是麦说的话】{directive}\n"
        "请以 Spica 的口吻自然地主动说一句，简短、口语化、适合直接朗读。"
        "不要提到系统、事件、指令这些词。"
    )


class ProactiveTurnArbiter:
    """Pure policy over injected callables -- knows no UI, no domain.

    ``is_busy``: the composition root's busy truth (conversation + recording).
    ``start_turn``: actually launches the system turn (UI wires its stream entry).
    ``input_gate``: the full-duplex seam (NullInputGate by default).
    """

    def __init__(
        self,
        *,
        is_busy: Callable[[], bool],
        start_turn: Callable[[ProactiveTurnRequest], Any],
        input_gate: VoiceInputGate | None = None,
    ) -> None:
        self._is_busy = is_busy
        self._start_turn = start_turn
        self._input_gate: VoiceInputGate = input_gate or NullInputGate()

    def try_speak(self, request: ProactiveTurnRequest) -> bool:
        """v1: drop_if_busy. Returns whether the system turn was started."""
        if self._is_busy():
            logger.debug(
                "proactive turn dropped (busy): source=%s directive=%r",
                request.source, request.directive,
            )
            return False
        self._input_gate.before_system_speech()
        self._start_turn(request)
        return True

    def system_speech_finished(self) -> None:
        """The UI reports the system stream's playback ended (done OR stopped):
        the full-duplex gate's restore point."""
        self._input_gate.after_system_speech()
