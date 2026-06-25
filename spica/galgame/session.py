"""GalgameCompanionSession: the single owner of galgame play-session state (Phase 4).

CLAUDE.md §4.2 red lines:

- This is the ONLY thing that mutates the FSM state / session fields. ``_state`` is
  private with a read-only ``state`` property (NO setter); every transition goes
  through the single private ``_transition``, which rejects illegal edges with
  ``GalgameStateError`` (never silent). All mutation is serialized by an ``RLock``.
- It emits Host->UI events through an injected Qt-free sink on its OWN lifecycle --
  NOT tied to any ``run_turn`` / ``TurnContext``. It never imports Qt / touches a
  widget; it only ever emits ``RuntimeEvent`` dataclasses.

``PlaySession.state`` is a durable PROJECTION of the in-memory FSM (the FSM is the
owner truth, §4.2). Projection writes are best-effort (§4.2 / §13.7 philosophy): a
failed ``update_play_session`` does NOT roll back or wedge the FSM. Instead it sets
a dirty flag, logs a warning, and emits a context-rich ``galgame_error``. Because
every projection writes the ABSOLUTE current mapped state, the next transition
re-projects the current truth (self-heal) rather than stacking on a drifted value
-- which stops a normally-ended session from later looking like a crashed dangling
session on restart.

OCR loop, real screenshots, and LLM summarization are NOT here (Phase 7/8); this
phase builds the state-flow + projection + event skeleton with hook positions.
Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import uuid
from enum import Enum
from typing import Any

from spica.core.companion_events import (
    CompanionEventSink,
    GalgameChoiceDetectedEvent,
    GalgameChoiceRecordedEvent,
    GalgameErrorEvent,
    GalgameStableLineCommittedEvent,
    GalgameStatusChangedEvent,
    GalgameSummaryDoneEvent,
    GalgameSummaryStartedEvent,
    GalgameWindowLostEvent,
    GalgameWindowRecoveredEvent,
    noop_companion_sink,
)
from spica.galgame.models import (
    CharacterRelation,
    ChoiceEvent,
    GameProgressState,
    OCRProfile,
    PlaySession,
    StoryLine,
    StoryLineStatus,
    StorySummary,
    utc_now_iso,
)
from spica.galgame.text_stream import StableLineTracker, StableOutcome, resolve_speaker
from spica.ports.game_memory import GameMemoryPort
from spica.runtime.jobs import InlineJobRunner

logger = logging.getLogger(__name__)


class GalgameState(str, Enum):
    IDLE = "idle"
    GAME_LAUNCHED = "game_launched"
    CALIBRATING = "calibrating"
    PLAYING = "playing"
    PAUSED = "paused"
    WINDOW_LOST = "window_lost"
    CHOICE_CHECKING = "choice_checking"
    BACKGROUND_SUMMARIZING = "background_summarizing"
    SUMMARIZING = "summarizing"
    ENDING = "ending"
    ERROR = "error"


class GalgameStateError(RuntimeError):
    """Raised on an illegal FSM transition (never silently ignored)."""


_S = GalgameState

# Shared state-set constants (D-P5-8) -- the named truth for "which states are
# OCR-monitored visible" so the #1 privacy gate and the P5 reaction gates can't
# drift apart. The asymmetry is deliberate:
# - watch is USER-initiated (CHOICE_CHECKING is its primary scenario: "该选哪个");
# - a reaction is HER-initiated -- speaking up while the user is waiting on a
#   choice analysis would steal the interaction, so CHOICE_CHECKING observes
#   (cuts a beat, keeps context) but does not speak.
WATCH_SAFE_STATES: frozenset[GalgameState] = frozenset(
    {_S.PLAYING, _S.CHOICE_CHECKING, _S.BACKGROUND_SUMMARIZING}
)
REACTION_OBSERVE_STATES: frozenset[GalgameState] = frozenset(WATCH_SAFE_STATES)
REACTION_SPEAK_STATES: frozenset[GalgameState] = frozenset(
    {_S.PLAYING, _S.BACKGROUND_SUMMARIZING}
)

# §16.1 / §16.2 / §16.3 / §16.4 transition graph. The single source of truth for
# legality; _transition rejects anything not here.
ALLOWED_TRANSITIONS: dict[GalgameState, frozenset[GalgameState]] = {
    _S.IDLE: frozenset({_S.GAME_LAUNCHED}),
    _S.GAME_LAUNCHED: frozenset({_S.CALIBRATING, _S.PLAYING, _S.IDLE}),
    _S.CALIBRATING: frozenset({_S.PLAYING, _S.GAME_LAUNCHED, _S.ERROR}),
    _S.PLAYING: frozenset(
        {_S.PAUSED, _S.WINDOW_LOST, _S.CHOICE_CHECKING, _S.BACKGROUND_SUMMARIZING, _S.SUMMARIZING, _S.ERROR}
    ),
    _S.PAUSED: frozenset({_S.PLAYING, _S.WINDOW_LOST, _S.SUMMARIZING, _S.ERROR}),
    _S.WINDOW_LOST: frozenset({_S.PLAYING, _S.PAUSED, _S.SUMMARIZING, _S.ERROR}),
    # SUMMARIZING edge: review #2 -- stop() during a choice check must be able to
    # end() normally (end transitions source -> SUMMARIZING); without this edge
    # the session stayed active in the DB and dangling recovery treated a normal
    # stop as crash residue.
    _S.CHOICE_CHECKING: frozenset({_S.PLAYING, _S.WINDOW_LOST, _S.SUMMARIZING, _S.ERROR}),
    _S.BACKGROUND_SUMMARIZING: frozenset({_S.PLAYING, _S.SUMMARIZING, _S.ERROR}),
    _S.SUMMARIZING: frozenset({_S.ENDING, _S.ERROR}),
    _S.ENDING: frozenset({_S.GAME_LAUNCHED, _S.IDLE, _S.ERROR}),
    _S.ERROR: frozenset({_S.GAME_LAUNCHED, _S.IDLE, _S.ENDING}),
}

# §16.5 FSM -> PlaySession.state projection. idle/game_launched have no LIVE session
# (game_launched is "ended" only after end() finalizes); ending is active until
# finalize marks it ended; error -> crashed (Phase 4 conservative pick of §16.5's
# active/paused/crashed, so dangling recovery treats it as a crash residue).
_PLAYSESSION_STATE: dict[GalgameState, str] = {
    _S.CALIBRATING: "active",
    _S.PLAYING: "active",
    _S.PAUSED: "paused",
    _S.WINDOW_LOST: "paused",
    _S.CHOICE_CHECKING: "active",
    _S.BACKGROUND_SUMMARIZING: "active",
    _S.SUMMARIZING: "active",
    _S.ENDING: "active",
    _S.ERROR: "crashed",
}


def _new_id() -> str:
    return uuid.uuid4().hex


class GalgameCompanionSession:
    def __init__(
        self,
        game_memory: GameMemoryPort,
        emit: CompanionEventSink | None = None,
        *,
        character_id: str = "spica",
        user_id: str = "麦",
        jobs: Any = None,
        summarizer: Any = None,
        summary_trigger_chars: int = 2000,
    ) -> None:
        self._mem = game_memory
        self._emit: CompanionEventSink = emit or noop_companion_sink
        self._character_id = character_id
        self._user_id = user_id
        # Phase 8: background summarization. jobs runs the LLM off the OCR loop thread
        # (default Inline = synchronous, for tests); summarizer None -> no summaries
        # (keeps Phase 4-7 construction/tests unchanged).
        self._jobs = jobs or InlineJobRunner()
        self._summarizer = summarizer
        self._summary_trigger_chars = max(1, int(summary_trigger_chars))
        self._summary_in_flight = False
        self._lock = threading.RLock()
        self._state = GalgameState.IDLE
        self._game_id: str | None = None
        self._playthrough_id = "default"
        self._session_id: str | None = None
        self._play_session: PlaySession | None = None
        self._playsession_dirty = False
        # Phase 7 OCR text stream: the session owns the stable-line state (§4.2).
        self._tracker: StableLineTracker | None = None
        self._speaker_strategy = "region"
        self._pending_current_line: StoryLine | None = None
        # Phase 8: hold the committed StoryLine objects (not just ids) so the summary
        # trigger can char-count and the snapshot can carry text without a DB read.
        self._unsummarized_lines: list[StoryLine] = []

    # -- read-only views (NO setters: external code cannot mutate state) -------
    @property
    def state(self) -> GalgameState:
        return self._state

    @property
    def game_id(self) -> str | None:
        return self._game_id

    @property
    def playthrough_id(self) -> str:
        return self._playthrough_id

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def is_projection_dirty(self) -> bool:
        """True when the durable PlaySession projection is behind the in-memory FSM."""
        return self._playsession_dirty

    @property
    def pending_current_line(self) -> StoryLine | None:
        return self._pending_current_line

    @property
    def unsummarized_line_ids(self) -> tuple[str, ...]:
        """The in-memory Story buffer: committed-but-unsummarized line ids (§11)."""
        return tuple(line.line_id for line in self._unsummarized_lines)

    # -- the single state-mutation point --------------------------------------
    def _transition(self, target: GalgameState, *, message: str = "") -> None:
        # Caller MUST hold self._lock. Validate BEFORE mutating so an illegal call
        # leaves the FSM untouched.
        if target not in ALLOWED_TRANSITIONS[self._state]:
            raise GalgameStateError(
                f"illegal galgame transition: {self._state.value} -> {target.value}"
            )
        previous = self._state
        self._state = target
        self._project_playsession(target)  # best-effort durable projection (self-heals)
        self._emit(GalgameStatusChangedEvent(state=target.value, previous=previous.value, message=message))

    def _project_playsession(self, target: GalgameState) -> None:
        mapped = _PLAYSESSION_STATE.get(target)
        if mapped is None or self._session_id is None:
            # idle / game_launched (pre-start) have no live PlaySession to project.
            return
        # Always write the ABSOLUTE current mapped state -- never a delta -- so the
        # next projection after a failure re-syncs the DB to current truth.
        try:
            self._mem.update_play_session(self._session_id, state=mapped)
            if self._play_session is not None:
                self._play_session = dataclasses.replace(self._play_session, state=mapped)
            self._playsession_dirty = False
        except Exception as exc:  # noqa: BLE001 -- best-effort projection, never wedge the FSM
            self._playsession_dirty = True
            self._emit_projection_error("PLAYSESSION_PROJECTION_FAILED", target.value, exc)

    def _emit_projection_error(self, code: str, target_state: str, exc: BaseException) -> None:
        logger.warning(
            "galgame PlaySession projection failed (session_id=%s, target_state=%s): %s",
            self._session_id, target_state, exc, exc_info=True,
        )
        self._emit(
            GalgameErrorEvent(
                message=f"PlaySession projection failed: {exc}",
                code=code,
                session_id=self._session_id or "",
                target_state=target_state,
            )
        )

    # -- lifecycle: bind / start / calibrate ----------------------------------
    def bind_game(self, game_id: str, playthrough_id: str = "default") -> None:
        with self._lock:
            self._transition(_S.GAME_LAUNCHED)  # idle -> game_launched (raises if illegal)
            self._game_id = game_id
            self._playthrough_id = playthrough_id

    def start(self, *, needs_calibration: bool = False) -> str:
        with self._lock:
            target = _S.CALIBRATING if needs_calibration else _S.PLAYING
            self._transition(target)  # game_launched -> calibrating/playing
            # Create the durable PlaySession now (state mapped from target).
            self._session_id = _new_id()
            self._play_session = PlaySession(
                session_id=self._session_id,
                game_id=self._game_id or "",
                started_at=utc_now_iso(),
                playthrough_id=self._playthrough_id,
                state=_PLAYSESSION_STATE[target],
            )
            try:
                self._mem.add_play_session(self._play_session)
                self._playsession_dirty = False
            except Exception as exc:  # noqa: BLE001 -- best-effort
                self._playsession_dirty = True
                self._emit_projection_error("PLAYSESSION_CREATE_FAILED", target.value, exc)
            self._init_stable_line_tracker()
            return self._session_id

    def _init_stable_line_tracker(self) -> None:
        # Tracker params + speaker strategy come from the game's calibrated OCRProfile
        # (Phase 6); defaults apply when uncalibrated.
        ocr_profile = OCRProfile()
        profile = self._mem.get_game_profile(self._game_id) if self._game_id else None
        if profile is not None and profile.ocr_profile:
            ocr_profile = OCRProfile.from_dict(profile.ocr_profile)
        self._speaker_strategy = ocr_profile.speaker_strategy
        self._tracker = StableLineTracker(
            stability_required=ocr_profile.stability_required_count,
            similarity_threshold=ocr_profile.similarity_threshold,
        )
        self._pending_current_line = None
        self._unsummarized_lines = []

    def finish_calibration(self) -> None:
        with self._lock:
            self._transition(_S.PLAYING)  # calibrating -> playing

    # -- pause / resume / window ----------------------------------------------
    def pause(self) -> None:
        with self._lock:
            self._transition(_S.PAUSED)

    def resume(self) -> None:
        with self._lock:
            self._transition(_S.PLAYING)  # paused -> playing

    def on_window_lost(self, reason: str = "") -> None:
        with self._lock:
            self._transition(_S.WINDOW_LOST, message=reason)  # default suspend; no auto-end (§16.3)
            self._emit(GalgameWindowLostEvent(reason=reason))

    def on_window_recovered(self) -> None:
        with self._lock:
            self._transition(_S.PLAYING)  # window_lost -> playing
            self._emit(GalgameWindowRecoveredEvent())

    # -- choices --------------------------------------------------------------
    def begin_choice_check(self) -> None:
        with self._lock:
            self._transition(_S.CHOICE_CHECKING)

    def on_choice_detected(self, choice_event: ChoiceEvent) -> None:
        with self._lock:
            if self._state != _S.CHOICE_CHECKING:
                raise GalgameStateError(
                    f"on_choice_detected requires choice_checking, in {self._state.value}"
                )
            # Primary user data: a write failure propagates (NOT best-effort) so the
            # caller knows the choice was not recorded; FSM stays in choice_checking.
            self._mem.add_choice_event(choice_event)
            self._transition(_S.PLAYING)
            self._emit(GalgameChoiceDetectedEvent(choice_id=choice_event.choice_id, options=choice_event.options))

    def on_user_reported_choice(
        self, *, selected_index: int | None = None, selected_text: str | None = None
    ) -> str:
        with self._lock:
            if self._game_id is None or self._session_id is None:
                raise GalgameStateError("on_user_reported_choice requires an active play session")
            recent = self._mem.recent_choice_events(self._game_id, self._playthrough_id, limit=5)
            pending = next(
                (e for e in recent if e.selected_option_index is None and e.selected_option_text is None),
                None,
            )
            if pending is not None:  # link to the most recent unfinished ChoiceEvent (§14.4)
                self._mem.update_choice_event(
                    pending.choice_id,
                    selected_option_index=selected_index,
                    selected_option_text=selected_text,
                    selection_source="user_reported",
                )
                choice_id = pending.choice_id
            else:  # no pending event -> new manual ChoiceEvent (§14.4)
                manual = ChoiceEvent(
                    choice_id=_new_id(),
                    game_id=self._game_id,
                    playthrough_id=self._playthrough_id,
                    session_id=self._session_id,
                    timestamp=utc_now_iso(),
                    options=[],
                    selected_option_index=selected_index,
                    selected_option_text=selected_text,
                    selection_source="user_reported",
                )
                self._mem.add_choice_event(manual)
                choice_id = manual.choice_id
            self._emit(
                GalgameChoiceRecordedEvent(
                    choice_id=choice_id, selected_index=selected_index, selected_text=selected_text
                )
            )
            return choice_id

    # -- summaries ------------------------------------------------------------
    def begin_background_summary(self) -> None:
        with self._lock:
            self._transition(_S.BACKGROUND_SUMMARIZING)
            self._emit(GalgameSummaryStartedEvent(reason="background"))

    def on_summary_finished(self, summary_id: str | None = None) -> None:
        with self._lock:
            self._transition(_S.PLAYING)  # background_summarizing -> playing
            self._emit(GalgameSummaryDoneEvent(summary_id=summary_id))

    # -- abnormal condition ---------------------------------------------------
    def mark_error(self, reason: str = "") -> None:
        """Declare an abnormal condition -> error state (§16.1). Reachable from the
        active states (not idle/game_launched). Projects PlaySession.state=crashed."""
        with self._lock:
            self._transition(_S.ERROR, message=reason)
            self._emit(
                GalgameErrorEvent(
                    message=reason or "session entered error state",
                    code="SESSION_ERROR",
                    session_id=self._session_id or "",
                    target_state=_S.ERROR.value,
                )
            )

    # -- OCR text stream (Phase 7, §10.3) -------------------------------------
    def on_ocr_result(self, text: str, speaker: str | None = None) -> None:
        with self._lock:
            # OCR keeps flowing during a BACKGROUND_SUMMARIZING pass (§16.1) -- the
            # summary runs off this thread, so collecting must not require pure PLAYING.
            if self._state not in (_S.PLAYING, _S.BACKGROUND_SUMMARIZING):
                raise GalgameStateError(f"on_ocr_result requires playing, in {self._state.value}")
            if self._tracker is None:
                self._init_stable_line_tracker()
            resolved_speaker, resolved_text = resolve_speaker(self._speaker_strategy, speaker, text)
            outcome = self._tracker.feed(resolved_text)
            if outcome is StableOutcome.NEW_STABLE:
                # A new line settled: the previous pending line is now final.
                self._commit_pending_current()
                self._write_pending_current(resolved_speaker, resolved_text)
                self._maybe_trigger_background_summary()
            # PENDING (still typing) / SAME (line unchanged) / EMPTY -> no write.

    def _write_pending_current(self, speaker: str | None, text: str) -> None:
        line = StoryLine(
            line_id=_new_id(),
            session_id=self._session_id or "",
            game_id=self._game_id or "",
            text=text,
            timestamp=utc_now_iso(),
            playthrough_id=self._playthrough_id,
            speaker=speaker,
            source="ocr",
            confidence=0.0,
            raw_hash="",
            status=StoryLineStatus.PENDING_CURRENT,
        )
        self._mem.add_story_line(line)  # persist immediately (crash safety, §10.5)
        self._pending_current_line = line

    def _commit_pending_current(self) -> None:
        line = self._pending_current_line
        if line is None:
            return
        self._mem.update_story_line_status(line.line_id, StoryLineStatus.COMMITTED)
        self._unsummarized_lines.append(line.with_status(StoryLineStatus.COMMITTED))
        self._pending_current_line = None
        self._emit(GalgameStableLineCommittedEvent(line_id=line.line_id, speaker=line.speaker, text=line.text))

    # -- background summarization (Phase 8, §13; never blocks the OCR loop) ----
    def _unsummarized_chars(self) -> int:
        return sum(len(line.text) for line in self._unsummarized_lines)

    def _maybe_trigger_background_summary(self) -> None:
        # Caller holds the lock. Single in-flight (§27③); only from PLAYING.
        if self._summarizer is None or self._summary_in_flight:
            return
        if self._state != _S.PLAYING:
            return
        if self._unsummarized_chars() < self._summary_trigger_chars:
            return
        snapshot = list(self._unsummarized_lines)  # fixed batch, taken UNDER the lock
        self._summary_in_flight = True
        self.begin_background_summary()  # playing -> background_summarizing + emit started
        # The job runs the LLM OFF this thread (ThreadJobRunner) -- lock-free, so the
        # OCR loop keeps committing into _unsummarized_lines while it runs.
        self._jobs.submit(lambda: self._run_summary_job(snapshot))

    def _run_summary_job(self, snapshot: list[StoryLine]) -> None:
        # Runs on the JobRunner thread, NOT under the session lock: the LLM call is
        # lock-free, so OCR is never blocked on it. Context reads are per-call sqlite
        # connections (thread-safe).
        try:
            result = self._summarizer.summarize(
                snapshot,
                recent_summaries=self._mem.recent_summaries(self._game_id or "", self._playthrough_id, limit=3),
                progress=self._mem.get_progress_state(self._game_id or "", self._playthrough_id),
            )
        except Exception as exc:  # noqa: BLE001 -- best-effort; failure folds + retries
            logger.warning("galgame background summary failed: %s", exc, exc_info=True)
            self._fail_summary()
            return
        self._apply_summary(result, snapshot)

    def _apply_summary(self, result: Any, snapshot: list[StoryLine]) -> None:
        with self._lock:
            summary_id = self._persist_summary(result, snapshot)
            self._apply_progress_and_relations(result)
            self._advance_unsummarized(snapshot)  # only THIS batch leaves the buffer
            self._summary_in_flight = False
            self.on_summary_finished(summary_id)  # background_summarizing -> playing + emit done

    def _fail_summary(self) -> None:
        with self._lock:
            self._summary_in_flight = False
            # No StorySummary written -> snapshot lines stay unsummarized (reverse-lookup),
            # fold into the next attempt (§13.7). summary_id=None is the failure signal.
            self.on_summary_finished(None)

    def _persist_summary(self, result: Any, snapshot: list[StoryLine]) -> str:
        now = utc_now_iso()
        summary_id = _new_id()
        self._mem.add_summary(
            StorySummary(
                summary_id=summary_id,
                game_id=self._game_id or "",
                playthrough_id=self._playthrough_id,
                session_id=self._session_id or "",
                source_line_ids=[line.line_id for line in snapshot],
                summary_zh=result.summary_zh,
                key_original_lines=result.key_lines,
                characters=result.characters,
                major_events=result.major_events,
                unresolved_threads=result.unresolved_threads,
                route_guess=result.route_guess,
                created_at=now,
                updated_at=now,
                source="auto_summary",
            )
        )
        return summary_id

    def _advance_unsummarized(self, snapshot: list[StoryLine]) -> None:
        snap_ids = {line.line_id for line in snapshot}
        self._unsummarized_lines = [line for line in self._unsummarized_lines if line.line_id not in snap_ids]

    def _apply_progress_and_relations(self, result: Any) -> None:
        progress = self._mem.get_progress_state(self._game_id or "", self._playthrough_id) or GameProgressState(
            game_id=self._game_id or "", playthrough_id=self._playthrough_id
        )
        self._mem.upsert_progress_state(self._merge_progress(progress, result))
        for relation in result.relations:
            a, b = relation.get("character_a", ""), relation.get("character_b", "")
            if not a or not b:
                continue
            self._mem.upsert_character_relation(
                CharacterRelation(
                    relation_id=f"rel::{a}::{b}",  # stable -> re-summary upserts the same relation
                    game_id=self._game_id or "",
                    playthrough_id=self._playthrough_id,
                    character_a=a,
                    character_b=b,
                    relation_summary=relation.get("relation_summary", ""),
                    evidence=relation.get("evidence", []),
                    confidence=float(relation.get("confidence") or 0.0),
                    updated_at=utc_now_iso(),
                    source="auto_summary",
                )
            )

    def _merge_progress(self, progress: GameProgressState, result: Any) -> GameProgressState:
        import dataclasses as _dc

        # §13.5 LOAD-BEARING WALL: an LLM route proposal NEVER overwrites a route the
        # player confirmed. Only update the guess when it is not player-confirmed.
        route = dict(progress.route or {})
        if not route.get("confirmed"):
            guess = result.route_guess or {}
            if guess.get("name"):
                route = {
                    "confirmed": False,
                    "name": guess.get("name"),
                    "confidence": guess.get("confidence", 0.0),
                    "evidence": guess.get("evidence", []),
                    "source": "llm_guess",
                }
        chapter = dict(progress.chapter or {})
        chapter_guess = result.chapter_guess or {}
        if chapter_guess.get("title") and not chapter.get("title"):  # LLM guess fills only when empty
            chapter = {"title": chapter_guess.get("title"), "confidence": chapter_guess.get("confidence", 0.0), "source": "llm_guess"}
        major_events = list(progress.major_events) + [e for e in result.major_events if e not in progress.major_events]
        return _dc.replace(
            progress,
            route=route,
            chapter=chapter,
            major_events=major_events,
            unresolved_threads=result.unresolved_threads or progress.unresolved_threads,
            last_played_at=utc_now_iso(),
        )

    def declare_route(self, name: str) -> None:
        """Player authority over the route (§13.5): a declaration is the TRUTH and LLM
        summaries never overwrite it (see _merge_progress). SEAM ONLY this phase -- no
        real call site yet (the player cannot declare); the UI / command router wires
        the trigger later. Built now so the override rule is in place + tested."""
        with self._lock:
            progress = self._mem.get_progress_state(self._game_id or "", self._playthrough_id) or GameProgressState(
                game_id=self._game_id or "", playthrough_id=self._playthrough_id
            )
            import dataclasses as _dc

            progress = _dc.replace(
                progress,
                route={"confirmed": True, "name": name, "confidence": 1.0, "evidence": [], "source": "player"},
            )
            self._mem.upsert_progress_state(progress)

    # -- end (§16.4): commit pending -> final summary -> update progress/relations
    #    -> mark ended. The final-summary LLM runs OUTSIDE the lock. ------------
    def end(self) -> None:
        # Let any in-flight BACKGROUND summary finish + apply first (its batch leaves
        # the buffer). drain runs OUTSIDE the lock so that job can acquire it to apply.
        self._jobs.drain()
        with self._lock:
            self._commit_pending_current()  # §16.4 step 2: pending_current -> committed
            snapshot = list(self._unsummarized_lines)  # remaining unsummarized (under lock)
            self._transition(_S.SUMMARIZING)  # source -> summarizing
            self._emit(GalgameSummaryStartedEvent(reason="end"))
            game_id, playthrough_id, session_id = self._game_id or "", self._playthrough_id, self._session_id or ""

        # Final summary LLM call OUTSIDE the lock (OCR loop is being stopped by the
        # caller; a stray on_ocr_result would block only on the brief apply, not the LLM).
        result = None
        if self._summarizer is not None and snapshot:
            try:
                result = self._summarizer.summarize(
                    snapshot,
                    recent_summaries=self._mem.recent_summaries(game_id, playthrough_id, limit=3),
                    progress=self._mem.get_progress_state(game_id, playthrough_id),
                )
            except Exception as exc:  # noqa: BLE001 -- best-effort; lines stay unsummarized
                logger.warning("galgame end summary failed (session_id=%s): %s", session_id, exc, exc_info=True)
                self._emit(
                    GalgameErrorEvent(
                        message=f"end summary failed: {exc}", code="END_SUMMARY_FAILED",
                        session_id=session_id, target_state=_S.SUMMARIZING.value,
                    )
                )

        with self._lock:
            summary_id: str | None = None
            # An end-summary FAILURE (we HAD lines to summarize but the LLM returned
            # nothing) must NOT finalize the PlaySession: finalizing stamps
            # state=ended + ended_at, which makes it invisible to
            # dangling_play_sessions (scans active/paused + ended_at IS NULL) -> the
            # batch is orphaned FOREVER (the 06-23 47becb69 bug). Skipping finalize
            # leaves the row at the ENDING projection (state="active", ended_at NULL)
            # = the exact dangling shape, so next startup's recover_dangling_sessions
            # retries the summary. Gated tightly so NORMAL ends are byte-identical:
            #   - empty snapshot (everything already background-summarized) -> finalize
            #   - no summarizer wired (tests) -> finalize
            # only "had residue AND the summary failed" diverges. Failure persists
            # NOTHING (the if-result block holds persist + advance together), so there
            # is no half-summarized batch -> recovery's retry is idempotent.
            summary_failed = self._summarizer is not None and bool(snapshot) and result is None
            if result is not None:
                summary_id = self._persist_summary(result, snapshot)
                self._apply_progress_and_relations(result)
                self._advance_unsummarized(snapshot)
            self._emit(GalgameSummaryDoneEvent(summary_id=summary_id))
            self._transition(_S.ENDING)  # summarizing -> ending
            if summary_failed:
                # Leave it dangling for retry -- withhold ONLY the durable "ended"
                # stamp. The FSM still lands in game_launched below (a new game can
                # start); the DB row stays active/NULL for next-startup recovery.
                logger.warning(
                    "galgame end summary failed (session_id=%s): left dangling for "
                    "next-startup recovery retry (%d lines unsummarized)",
                    session_id, len(snapshot),
                )
            else:
                self._finalize_play_session()  # mark PlaySession ended (+ ended_at)
            self._transition(_S.GAME_LAUNCHED)  # ending -> game_launched (window still open)
            self._session_id = None
            self._play_session = None

    def _finalize_play_session(self) -> None:
        if self._session_id is None:
            return
        ended_at = utc_now_iso()
        try:
            self._mem.update_play_session(self._session_id, state="ended", ended_at=ended_at)
            if self._play_session is not None:
                self._play_session = dataclasses.replace(self._play_session, state="ended", ended_at=ended_at)
            self._playsession_dirty = False
        except Exception as exc:  # noqa: BLE001 -- best-effort
            self._playsession_dirty = True
            self._emit_projection_error("PLAYSESSION_FINALIZE_FAILED", _S.ENDING.value, exc)
