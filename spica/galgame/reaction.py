"""P5 剧情反应系统 step 1: the reaction engine skeleton (Qt-free, CLAUDE.md #1).

Consumes the companion event stream (committed stable lines / FSM status /
choice detection), aggregates lines into beats, runs the gate chain and -- when
everything passes -- asks the injected ``speak`` callable (step 3 wires it to
``ProactiveTurnArbiter.try_speak`` + directive composition) to start a system
turn. Scoring here is the placeholder ``null_scorer`` (always 0); step 2 swaps
in the lexicon scorer through the SAME ``Callable[[ReactionBeat], ScoreResult]``
seam, and a future LLM re-judge (v2) is just another implementation of it.

CONCURRENCY RED LINE (D-P5-0): ``enqueue_event`` is the ONLY sink-facing entry
and does nothing but ``put_nowait`` + return. ``GalgameStableLineCommittedEvent``
is emitted INSIDE the session lock on the OCR thread (``_commit_pending_current``
<- ``on_ocr_result``), so any synchronous work here -- scoring, DB reads,
``try_speak`` -- would block the OCR loop and can deadlock through re-entry.
All real work happens on the engine's own worker thread.

The synchronous core (``handle_event`` / ``handle_idle``) takes an explicit
``now`` so tests drive it with a fake clock and zero threads; the worker is a
thin shell that feeds it real time. The idle-flush debounce (D-P5-1) is the
worker's ``queue.get`` timeout -- no extra timer thread, no Qt.

Gate chain order (D-P5-10, cheap first; semantics per the approved design):
observe-state gate (line level) -> dedupe hash -> cooldown/budget -> score
threshold -> [step 3: similarity vs recent CompanionBeats, the one DB gate]
-> speak-state gate -> arbiter. A beat that fails ONLY the speak gate (cut
during CHOICE_CHECKING) is held as the single pending candidate and gets its
chance when the state returns to a speak state, if still fresh (D-P5-8).
"""

from __future__ import annotations

import hashlib
import logging
import queue
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from spica.core.companion_events import (
    GalgameChoiceDetectedEvent,
    GalgameStableLineCommittedEvent,
    GalgameStatusChangedEvent,
)
from spica.galgame.session import (
    REACTION_OBSERVE_STATES,
    REACTION_SPEAK_STATES,
    GalgameState,
)

logger = logging.getLogger(__name__)

# -- tunables (step 4 calibrates; one place to touch) ---------------------------

IDLE_FLUSH_SECONDS = 8.0  # D-P5-1: one-shot debounce -- a pause after a hot line
#   is exactly when she should get to speak, so the timer fires the cut instead
#   of waiting for the next line (the rejected "lazy timeout" would miss it).
PENDING_FRESHNESS_SECONDS = 30.0  # D-P5-8: a choice-held beat older than this is
#   stale -- commenting on it after the choice resolved would be 吐旧剧情.
MAX_BEAT_LINES = 8
MIN_LINES_FOR_PUNCT_CUT = 3
BUDGET_WINDOW_SECONDS = 600.0
DEDUPE_LRU_SIZE = 50

_STRONG_PUNCT = "！!？?…‼⁉"


@dataclass(frozen=True)
class ReactionModeParams:
    min_score: int
    max_per_window: int
    cooldown_seconds: float


# D-P5-3: the mode table is ONE dict constant so step-4 calibration touches one
# place. "off" is not a row -- the host simply never attaches the engine.
# low.min_score 6->5 (step 2, data-driven): the real-corpus distribution report
# (scripts/reaction_score_report.py over 1178 LimeLight lines) showed max
# observed score = 5, so a threshold of 6 could NEVER pass -- "low" would have
# been silence, not low-frequency. At 5 it passes the top 4.1% of beats.
REACTION_MODE_TABLE: dict[str, ReactionModeParams] = {
    "low": ReactionModeParams(min_score=5, max_per_window=1, cooldown_seconds=180.0),
    "normal": ReactionModeParams(min_score=4, max_per_window=3, cooldown_seconds=90.0),
    "high": ReactionModeParams(min_score=3, max_per_window=6, cooldown_seconds=45.0),
}


# -- data shapes -----------------------------------------------------------------


@dataclass(frozen=True)
class BeatLine:
    speaker: str | None
    text: str
    line_id: str


@dataclass(frozen=True)
class ReactionBeat:
    lines: tuple[BeatLine, ...]
    game_id: str
    cut_reason: str  # strong_punct | choice | max_lines | idle_flush
    choice_options: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScoreResult:
    score: int = 0
    reasons: tuple[str, ...] = ()


def null_scorer(beat: ReactionBeat) -> ScoreResult:
    """Placeholder scorer: never speaks. Production wires the lexicon scorer
    (``score_beat`` below) through the same seam; v2's LLM re-judge is another
    implementation of it."""
    del beat
    return ScoreResult(0, ())


# -- lexicon (step 2) ---------------------------------------------------------------
# The lexicon is a DATA file (D1 character-data class, like tts.yaml/visual.yaml),
# NOT a config carrier: it does not live in app.yaml and never touches the
# manager. Base file + optional per-game override, language follows the game's
# text (LimeLight -> Chinese).

_REACTION_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "galgame" / "reaction"

# Signal TRIGGER conditions are fixed in code (documented in default.yaml);
# their WEIGHTS are data-driven. choice_pending keys on cut_reason == "choice"
# -- a STATE signal from the session FSM, never a text regex (approved design).
_SIGNAL_EXCLAMATION_MIN_MARKS = 2
_SIGNAL_SWARM_MIN_SPEAKERS = 3
_NORMALIZED_STRONG_PUNCT = "!?…‼⁉"


@dataclass(frozen=True)
class LexiconCategory:
    name: str
    weight: int
    words: tuple[str, ...]  # normalized at load time (same normalize as matching)


@dataclass(frozen=True)
class ReactionLexicon:
    categories: tuple[LexiconCategory, ...]
    signals: dict[str, int]


def _parse_lexicon_mapping(data: Any) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Tolerant extraction: invalid entries are skipped with a warning, never raise."""
    categories: dict[str, dict[str, Any]] = {}
    signals: dict[str, int] = {}
    if not isinstance(data, dict):
        return categories, signals
    raw_categories = data.get("categories")
    if isinstance(raw_categories, dict):
        for name, entry in raw_categories.items():
            if not isinstance(entry, dict):
                logger.warning("reaction lexicon: category %r is not a mapping, skipped", name)
                continue
            words = entry.get("words")
            try:
                weight = int(entry.get("weight", 0))
            except (TypeError, ValueError):
                logger.warning("reaction lexicon: category %r has a bad weight, skipped", name)
                continue
            if not isinstance(words, list) or weight <= 0:
                logger.warning("reaction lexicon: category %r missing words/weight, skipped", name)
                continue
            categories[str(name)] = {"weight": weight, "words": [str(w) for w in words]}
    raw_signals = data.get("signals")
    if isinstance(raw_signals, dict):
        for name, value in raw_signals.items():
            try:
                signals[str(name)] = int(value)
            except (TypeError, ValueError):
                logger.warning("reaction lexicon: signal %r has a bad weight, skipped", name)
    return categories, signals


def load_reaction_lexicon(
    game_id: str | None = None, base_dir: str | Path | None = None
) -> ReactionLexicon:
    """default.yaml + optional <game_id>.yaml, DEEP-MERGED (D-P5-9): a same-name
    category in the game file replaces the default one wholesale; new categories
    are added; signals merge per key. Words are normalized at load so matching
    and the lexicon agree on one normal form."""
    root = Path(base_dir) if base_dir is not None else _REACTION_DATA_DIR
    categories: dict[str, dict[str, Any]] = {}
    signals: dict[str, int] = {}
    for name in ("default", str(game_id) if game_id else None):
        if not name:
            continue
        path = root / f"{name}.yaml"
        if not path.is_file():
            if name == "default":
                logger.warning("reaction lexicon: %s missing -- scoring will be inert", path)
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 -- a broken data file must not crash play
            logger.warning("reaction lexicon: failed to read %s: %s", path, exc)
            continue
        file_categories, file_signals = _parse_lexicon_mapping(data)
        categories.update(file_categories)  # same name -> wholesale replace
        signals.update(file_signals)
    parsed = tuple(
        LexiconCategory(
            name=name,
            weight=entry["weight"],
            words=tuple(
                w for w in (normalize_reaction_text(word) for word in entry["words"]) if w
            ),
        )
        for name, entry in categories.items()
    )
    return ReactionLexicon(categories=parsed, signals=signals)


def score_beat(
    beat: ReactionBeat, lexicon: ReactionLexicon, context: Any = None
) -> ScoreResult:
    """Pure, deterministic, zero-LLM zero-IO (CLAUDE.md #3): same beat + same
    lexicon -> same score. ``context`` is the v2 upgrade seam (LLM re-judge /
    summarizer progress); v1 always passes None and it is ignored."""
    del context
    text = normalize_reaction_text("\n".join(line.text for line in beat.lines))
    score = 0
    reasons: list[str] = []
    for category in lexicon.categories:
        if any(word in text for word in category.words):
            score += category.weight
            reasons.append(f"category:{category.name}")
    if beat.cut_reason == "choice" and lexicon.signals.get("choice_pending"):
        score += lexicon.signals["choice_pending"]
        reasons.append("signal:choice_pending")
    marks = sum(text.count(mark) for mark in _NORMALIZED_STRONG_PUNCT)
    if marks >= _SIGNAL_EXCLAMATION_MIN_MARKS and lexicon.signals.get("exclamation_density"):
        score += lexicon.signals["exclamation_density"]
        reasons.append("signal:exclamation_density")
    speakers = {line.speaker for line in beat.lines if line.speaker}
    if len(speakers) >= _SIGNAL_SWARM_MIN_SPEAKERS and lexicon.signals.get("speaker_swarm"):
        score += lexicon.signals["speaker_swarm"]
        reasons.append("signal:speaker_swarm")
    return ScoreResult(score=score, reasons=tuple(reasons))


@dataclass(frozen=True)
class ReactionDecision:
    """One terminal outcome per processed beat (plus observe flushes) -- the
    deterministic trail the golden tests pin."""

    kind: str  # spoke|busy_drop|speak_hold|pending_dropped|dedupe_hash_drop|
    #            cooldown_drop|budget_capped_drop|below_threshold|observe_flush
    detail: str = ""  # cut_reason for beat outcomes; "stale"/"replaced"/...
    score: int = 0
    line_ids: tuple[str, ...] = ()


# -- shared text normal form (dedupe hash + lexicon matching, OCR-noise robust) ----

_NORMALIZE_TRANS = str.maketrans({"！": "!", "？": "?", "。": ".", "，": ",", "　": None})


def normalize_reaction_text(text: str) -> str:
    """One normal form for hashing AND word matching: whitespace stripped,
    common fullwidth/halfwidth confusions unified (the punctuation shapes OCR
    actually misreads), lowercased. Substring matching over this needs no
    Chinese segmentation."""
    return "".join((text or "").split()).translate(_NORMALIZE_TRANS).lower()


def beat_hash(beat: ReactionBeat) -> str:
    payload = "\n".join(normalize_reaction_text(line.text) for line in beat.lines)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


_STOP = object()


class ReactionEngine:
    """Aggregation + gates + budget over the companion event stream.

    Single-consumer: all state below is touched only by the worker thread (or
    by the test driving ``handle_event``/``handle_idle`` synchronously instead).
    """

    def __init__(
        self,
        *,
        speak: Callable[[ReactionBeat, int], bool],
        params_provider: Callable[[], ReactionModeParams] | None = None,
        scorer: Callable[[ReactionBeat], ScoreResult] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._speak = speak
        # D-P5-4 hot-swap seam: a holder callable, re-read per beat. v1 wires a
        # constant (restart-effective); a future settings panel swaps the holder
        # value without touching the engine.
        self._params = params_provider or (lambda: REACTION_MODE_TABLE["normal"])
        self._scorer = scorer or null_scorer
        self._clock = clock or time.monotonic
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread: threading.Thread | None = None
        # -- worker-owned state ----------------------------------------------
        self._state: GalgameState = GalgameState.IDLE
        self._game_id: str = ""
        self._buffer: list[BeatLine] = []
        self._idle_deadline: float | None = None
        self._seen_hashes: OrderedDict[str, None] = OrderedDict()
        self._spoken_at: deque[float] = deque()
        self._last_spoken_at: float | None = None
        self._pending: tuple[ReactionBeat, int, float] | None = None
        self.decisions: deque[ReactionDecision] = deque(maxlen=200)

    # -- sink-facing entry (D-P5-0 red line) ----------------------------------

    def enqueue_event(self, event: Any) -> None:
        """CompanionEventSink-compatible. put_nowait + return -- NOTHING else may
        run here: the caller may be the OCR thread inside the session lock."""
        self._queue.put_nowait(event)

    def set_active_game(self, game_id: str) -> None:
        """Step-3 wiring calls this at companion start, BEFORE events flow."""
        self._game_id = str(game_id or "")

    # -- worker shell -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._worker_loop, name="reaction-engine", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._queue.put_nowait(_STOP)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _worker_loop(self) -> None:
        while True:
            timeout = None
            if self._idle_deadline is not None:
                timeout = max(0.0, self._idle_deadline - self._clock())
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                self.handle_idle(self._clock())
                continue
            if item is _STOP:
                return
            try:
                self.handle_event(item, self._clock())
            except Exception:  # noqa: BLE001 -- a bad event must not kill the worker
                logger.exception("reaction engine: event handling failed")

    # -- synchronous core (fake-clock testable; the worker is its only prod caller)

    def handle_event(self, event: Any, now: float) -> None:
        if isinstance(event, GalgameStatusChangedEvent):
            self._on_status(event, now)
        elif isinstance(event, GalgameStableLineCommittedEvent):
            self._on_line(event, now)
        elif isinstance(event, GalgameChoiceDetectedEvent):
            self._on_choice(event, now)
        # every other companion event (summary progress, previews...) is noise here

    def handle_idle(self, now: float) -> None:
        if self._idle_deadline is None or now < self._idle_deadline:
            return
        self._idle_deadline = None
        if self._buffer:
            self._cut("idle_flush", now)

    # -- event handlers -----------------------------------------------------------

    def _on_status(self, event: GalgameStatusChangedEvent, now: float) -> None:
        try:
            new_state = GalgameState(event.state)
        except ValueError:
            return
        old_state, self._state = self._state, new_state
        left_observe = (
            old_state in REACTION_OBSERVE_STATES and new_state not in REACTION_OBSERVE_STATES
        )
        if left_observe:
            # D-P5-1: leaving the monitored-visible trio discards the buffer
            # unscored (the safety gate would refuse it anyway; discard is clean).
            if self._buffer:
                self._decide("observe_flush", detail=old_state.value,
                             line_ids=tuple(l.line_id for l in self._buffer))
                self._buffer.clear()
            self._idle_deadline = None
            if self._pending is not None:
                self._decide("pending_dropped", detail="left_observe")
                self._pending = None
        if new_state in REACTION_SPEAK_STATES and self._pending is not None:
            self._try_pending(now)

    def _on_line(self, event: GalgameStableLineCommittedEvent, now: float) -> None:
        if self._state not in REACTION_OBSERVE_STATES:
            return  # not monitored-visible: lines are not even buffered
        self._buffer.append(BeatLine(event.speaker, event.text, event.line_id))
        self._idle_deadline = now + IDLE_FLUSH_SECONDS
        # Speaker switches are a SOFT boundary only (D-P5-1): they never cut, so
        # back-and-forth dialogue doesn't fragment into single-line beats.
        tail = (event.text or "").rstrip()
        if len(self._buffer) >= MAX_BEAT_LINES:
            self._cut("max_lines", now)
        elif (
            len(self._buffer) >= MIN_LINES_FOR_PUNCT_CUT
            and bool(tail)
            and tail[-1] in _STRONG_PUNCT
        ):
            self._cut("strong_punct", now)

    def _on_choice(self, event: GalgameChoiceDetectedEvent, now: float) -> None:
        if self._state not in REACTION_OBSERVE_STATES or not self._buffer:
            return
        options = tuple(
            str(o.get("text", "") if isinstance(o, dict) else o) for o in event.options
        )
        self._cut("choice", now, options=options)

    # -- beat pipeline ---------------------------------------------------------------

    def _cut(self, reason: str, now: float, options: tuple[str, ...] = ()) -> None:
        beat = ReactionBeat(
            lines=tuple(self._buffer),
            game_id=self._game_id,
            cut_reason=reason,
            choice_options=options,
        )
        self._buffer.clear()
        self._idle_deadline = None
        self._process_beat(beat, now)

    def _process_beat(self, beat: ReactionBeat, now: float) -> None:
        line_ids = tuple(line.line_id for line in beat.lines)
        digest = beat_hash(beat)
        if digest in self._seen_hashes:
            self._decide("dedupe_hash_drop", detail=beat.cut_reason, line_ids=line_ids)
            return
        self._seen_hashes[digest] = None
        while len(self._seen_hashes) > DEDUPE_LRU_SIZE:
            self._seen_hashes.popitem(last=False)

        params = self._params()
        if not self._budget_allows(params, now):
            kind = "cooldown_drop" if self._in_cooldown(params, now) else "budget_capped_drop"
            self._decide(kind, detail=beat.cut_reason, line_ids=line_ids)
            return

        result = self._scorer(beat)
        if result.score < params.min_score:
            self._decide("below_threshold", detail=beat.cut_reason,
                         score=result.score, line_ids=line_ids)
            return

        # [step 3 seam] similarity gate vs recent CompanionBeats goes HERE -- the
        # one DB read in the chain, last on purpose (D-P5-10), worker-thread only
        # by construction (D-P5-0).

        if self._state not in REACTION_SPEAK_STATES:
            if self._pending is not None:
                self._decide("pending_dropped", detail="replaced")
            self._pending = (beat, result.score, now)
            self._decide("speak_hold", detail=beat.cut_reason,
                         score=result.score, line_ids=line_ids)
            return
        self._speak_now(beat, result.score, now)

    def _try_pending(self, now: float) -> None:
        beat, score, cut_at = self._pending  # type: ignore[misc]
        self._pending = None
        line_ids = tuple(line.line_id for line in beat.lines)
        if now - cut_at > PENDING_FRESHNESS_SECONDS:
            self._decide("pending_dropped", detail="stale", score=score, line_ids=line_ids)
            return
        params = self._params()
        if not self._budget_allows(params, now):
            kind = "cooldown_drop" if self._in_cooldown(params, now) else "budget_capped_drop"
            self._decide(kind, detail="pending", score=score, line_ids=line_ids)
            return
        self._speak_now(beat, score, now)

    def _speak_now(self, beat: ReactionBeat, score: int, now: float) -> None:
        line_ids = tuple(line.line_id for line in beat.lines)
        if self._speak(beat, score):
            # Budget/cooldown charge only on an ACTUAL utterance (D-P5-2); the
            # NO_COMMENT refund hook arrives with step 3's turn-finished callback.
            self._spoken_at.append(now)
            self._last_spoken_at = now
            self._decide("spoke", detail=beat.cut_reason, score=score, line_ids=line_ids)
        else:
            # Arbiter busy: processed (hash stays recorded -- a stale tease is
            # worse than none), no budget consumed, no cooldown stamped (D-P5-2).
            self._decide("busy_drop", detail=beat.cut_reason, score=score, line_ids=line_ids)

    # -- budget -----------------------------------------------------------------------

    def _in_cooldown(self, params: ReactionModeParams, now: float) -> bool:
        return (
            self._last_spoken_at is not None
            and now - self._last_spoken_at < params.cooldown_seconds
        )

    def _budget_allows(self, params: ReactionModeParams, now: float) -> bool:
        if self._in_cooldown(params, now):
            return False
        while self._spoken_at and now - self._spoken_at[0] > BUDGET_WINDOW_SECONDS:
            self._spoken_at.popleft()
        return len(self._spoken_at) < params.max_per_window

    def _decide(
        self,
        kind: str,
        *,
        detail: str = "",
        score: int = 0,
        line_ids: tuple[str, ...] = (),
    ) -> None:
        decision = ReactionDecision(kind=kind, detail=detail, score=score, line_ids=line_ids)
        self.decisions.append(decision)
        logger.debug("reaction decision: %s", decision)
