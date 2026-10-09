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
from dataclasses import dataclass, field
from concurrent.futures import Future
import math
from spica.core.events import RuntimeEvent
from spica.ports.conversation import MaterialHint, BusinessEventBinding
from typing import Any, Callable, ClassVar, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class ProactiveTurnRequest:
    directive: str  # the system event for her to react to (domain-authored TEXT)
    source: str = ""  # origin/coalescing key; never selects a business implementation
    conversation_id: str | None = None  # None -> the default chat namespace
    policy: str = "drop_if_busy"
    ttl_seconds: float | None = None
    want_audio: bool = field(default=True, kw_only=True)
    # Ordinary proactive speech stays in its declared conversation even while
    # a game is active. Only the game domain opts into its live binding.
    inherit_active_domain: bool = field(default=False, kw_only=True)
    # Domain authority rechecks an already proposed business invitation. It
    # carries no tools or model instructions and is never sent over the wire.
    is_current: Callable[[], bool] | None = field(default=None, kw_only=True, repr=False, compare=False)
    # An ordinary utterance may become irrelevant before it starts. This
    # deadline grants no preemption or volume override and ends at first sound.
    first_sound_deadline: float | None = field(default=None, kw_only=True)
    material_hint: MaterialHint | None = field(default=None, kw_only=True)
    event_binding: BusinessEventBinding | None = field(default=None, kw_only=True)

    def __post_init__(self):
        if self.first_sound_deadline is not None and not math.isfinite(self.first_sound_deadline):
            raise ValueError('first-sound deadline must be finite')


@dataclass(frozen=True)
class ScheduledProactiveTurnRequest(ProactiveTurnRequest):
    """Trusted delivery timing, using this host's monotonic clock.

    Business owners choose the times and volume. The shared runtime knows no
    alarm schedule, vision state, escalation level or role-specific policy.
    """

    due_at: float = field(kw_only=True)
    yield_at: float = field(kw_only=True)
    first_sound_deadline: float = field(kw_only=True)
    volume: float = field(kw_only=True)
    response_window_seconds: float = field(default=0, kw_only=True)
    response_window_deadline: float | None = field(default=None, kw_only=True)
    response_scope_id: str = field(default='', kw_only=True)
    local_audio_cue: str | None = field(default=None, kw_only=True)

    def __post_init__(self):
        if self.local_audio_cue not in {None, 'home_wake'}:
            raise ValueError('unsupported local audio cue')
        if (not all(math.isfinite(value) for value in (self.due_at, self.yield_at, self.first_sound_deadline))
                or not self.due_at <= self.yield_at < self.first_sound_deadline):
            raise ValueError('scheduled speech requires ordered, finite delivery times')
        if not math.isfinite(self.volume) or not 0 <= self.volume <= 1:
            raise ValueError('playback volume must be between 0 and 1')
        if not math.isfinite(self.response_window_seconds) or not 0 <= self.response_window_seconds <= 60:
            raise ValueError('response window must be between 0 and 60 seconds')
        if self.response_window_deadline is not None and not math.isfinite(self.response_window_deadline):
            raise ValueError('response deadline must be finite')


@dataclass(frozen=True)
class PreparedProactiveTurnRequest(ProactiveTurnRequest):
    """Core-owned completed content, with a deadline but no right to preempt."""
    speech_id: str = field(kw_only=True)
    first_sound_deadline: float = field(kw_only=True)

    def __post_init__(self):
        if not self.speech_id or not math.isfinite(self.first_sound_deadline):
            raise ValueError('prepared speech requires its core identity and a finite deadline')
        if self.policy != 'drop_if_busy':
            raise ValueError('prepared presentation is idle-only')


@dataclass(frozen=True)
class ProactiveTurnResult:
    """One speech attempt, not a business task or proof that a person heard it."""

    status: str
    request_id: str = ""
    turn_id: str = ""
    endpoint_id: str = ""
    answer: str = ""
    presentation_outcome: str | None = None
    first_audio_started_at: float | None = None  # host monotonic clock
    audio_outcome: str | None = None
    generation_failed: bool = field(default=False, kw_only=True)


@dataclass(frozen=True)
class ProactiveTurnHandle:
    proposal_id: str
    admitted: Future[bool] = field(default_factory=Future)
    result: Future[ProactiveTurnResult] = field(default_factory=Future)
    audio_started: Future[float | None] = field(default_factory=Future)
    # A deadline may settle the business result before STOP finishes. This
    # receipt waits for the turn and proactive worker to release ownership,
    # so artifact owners can dispose audio and dependent speech can submit.
    released: Future[bool] = field(default_factory=Future)


@dataclass(frozen=True)
class ResponseWindowClosedEvent(RuntimeEvent):
    """The owning business withdrew this temporary conversation scope."""
    kind: ClassVar[str] = 'response_window_closed'
    scope_id: str
    preserve_inflight: bool = False

    def _data(self):
        return {'scope_id': self.scope_id, 'preserve_inflight': self.preserve_inflight}






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


# P5 (D-P5-5): the domain-agnostic "nothing worth saying" escape for system
# turns. A domain directive may offer it ("如果实在没什么值得说的,只输出
# NO_COMMENT"); the orchestrator swallows a system turn whose WHOLE answer is
# this sentinel -- no display, no TTS, no recent memory, silent completion.
NO_COMMENT_SENTINEL = "NO_COMMENT"

_SENTINEL_WRAPPERS = "\"'「」『』（）()"
_SENTINEL_TRAILING_PUNCT = "。．.,，!！?？~～…"


def _clean_sentinel_text(text: str) -> str:
    return "".join((text or "").split()).strip(_SENTINEL_WRAPPERS).upper()


def is_no_comment_answer(text: str) -> bool:
    """True when a COMPLETE system-turn answer is the sentinel -- tolerant of
    quotes / trailing punctuation / case, so a model writing "no_comment。"
    still counts."""
    return _clean_sentinel_text(text).rstrip(_SENTINEL_TRAILING_PUNCT) == NO_COMMENT_SENTINEL


def may_become_no_comment(partial: str) -> bool:
    """True while a PARTIAL streamed answer is still sentinel-compatible. The
    orchestrator's system-turn hold gates on this EXPLICITLY, so swallowing
    never depends on play-unit min_chars tuning (D-P5-5)."""
    cleaned = _clean_sentinel_text(partial)
    if len(cleaned) <= len(NO_COMMENT_SENTINEL):
        return NO_COMMENT_SENTINEL.startswith(cleaned)
    return is_no_comment_answer(partial)


def compose_system_directive_message(directive: str) -> str:
    """Frame a directive as a SYSTEM event message. The framing lives in the
    message text (single-sourced here, shared by the engine entry and the UI),
    so prompt_builder stays untouched and the recent-memory record is
    self-identifying -- it never impersonates the interlocutor."""
    return (
        f"【系统事件，不是用户说的话】{directive}\n"
        "请以当前角色的口吻自然回应，遵循上述事件对内容、长度和语气的具体要求，并让表达适合直接朗读。"
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
