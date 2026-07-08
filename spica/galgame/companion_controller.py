"""Galgame companion controller (Path B, stage 1). Qt-free thin orchestration.

Ties the already-verified parts -- session (Phase 4), OCR stream runner (Phase 7),
summarizer (Phase 8) -- into a start/stop lifecycle, persisting through the INJECTED
game-memory adapter (the real ``spica_data/galgame.sqlite3`` when the host wires it,
vs the demo's tempfile). It invents no new logic and writes no persistence itself:
the session already persists (add_story_line / add_summary / add_play_session / ...);
the controller just hands it the real adapter.

Dependency-injected: it takes ports/adapters + an optional summarizer + the event
sink, and does NOT import ``spica.host`` (no galgame->host inversion). ``AppHost``
provides a thin ``new_companion_controller`` factory that injects the real adapters.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable

from spica.core.companion_events import CompanionEventSink, noop_companion_sink
from spica.galgame.history import compose_play_history
from spica.galgame.models import OCRProfile, WindowMatchRule, game_conversation_id
from spica.galgame.ocr_loop import OcrStreamRunner
from spica.galgame.session import GalgameCompanionSession, GalgameState
from spica.runtime.context import GameContextRequest, GameTurnBinding
from spica.runtime.jobs import ThreadJobRunner

logger = logging.getLogger(__name__)

Ratios = tuple[float, float, float, float]

# States with a live (active) PlaySession that end() can finalize. game_launched/idle
# have no PlaySession to finalize; ending/error/summarizing are not stop targets.
# Guards stop()/cleanup from an illegal end() transition. CHOICE_CHECKING joined
# in review #2 (the stage-1 scoping is lifted): stopping mid-choice used to skip
# end(), leaving an active PlaySession for dangling recovery to mop up.
_ENDABLE = frozenset(
    {
        GalgameState.PLAYING,
        GalgameState.PAUSED,
        GalgameState.WINDOW_LOST,
        GalgameState.CHOICE_CHECKING,
        GalgameState.BACKGROUND_SUMMARIZING,
    }
)


class GalgameCompanionError(RuntimeError):
    """A controller-level error (e.g. start without a resolvable game_id, or start
    while already running)."""


def _region_ratios(region: Any) -> Ratios | None:
    """An OCRRegion dict (§9.5) -> (x, y, w, h) ratios; None when absent/degenerate
    (zero width/height = never actually calibrated)."""
    if not isinstance(region, dict) or not region:
        return None
    ratios = (
        float(region.get("x_ratio", 0.0)),
        float(region.get("y_ratio", 0.0)),
        float(region.get("w_ratio", 0.0)),
        float(region.get("h_ratio", 0.0)),
    )
    if ratios[2] <= 0.0 or ratios[3] <= 0.0:
        return None
    return ratios


def guess_game_id_from_title(title: str | None) -> str:
    """Default game_id from a window title. A latin-titled game uses its leading
    latin-alphanumeric run, lowercased ("LimeLight Lemonade Jam" -> "limelight";
    "anemoi gemini-3.1-pro 機翻" -> "anemoi"). A CJK-titled game (Chinese/Japanese
    -- the common case for fan-translated galgames, which have no latin slug) uses
    the leading NAME segment up to the first metadata boundary (a latin letter, or
    a bracket / pipe / slash / @), with only its EDGE punctuation trimmed -- in-title
    symbols (★☆！～) are KEPT because this id doubles as the window-focus keyword and
    must stay a contiguous substring of the live title:
    "创作彼女的恋爱方程式-Galgamer@..." -> "创作彼女的恋爱方程式";
    "次元错位恋人!!【claude-4.6-opus】..." -> "次元错位恋人";
    "绽放★青春全力向前冲！" -> "绽放★青春全力向前冲". Returns "" only when that segment
    has no CJK/kana/digit (e.g. a leading "【汉化】" bracket) -- the caller must
    then supply an explicit game_id. This is only a DEFAULT; the caller's game_id
    always wins, and a CJK id is namespace-safe (conversation_id splits on "::",
    the per-game reaction lexicon file is an optional overlay)."""
    if not title:
        return ""
    stripped = title.strip()
    latin = re.match(r"[0-9a-z]+", stripped.lower())
    if latin:
        return latin.group(0)
    # CJK-titled game: cut at the first metadata boundary -- a latin letter
    # (translator/model tags: "Galgamer", "claude-4.6-opus", "Made by") or a
    # bracket / pipe / slash / @ -- then trim only the EDGE punctuation/whitespace.
    # In-title symbols (★☆！～) are KEPT in the middle on purpose: this id doubles
    # as the window-focus keyword (companion start -> title_keywords=[id]; ocr_loop
    # verifies focus by `keyword in live_title`), so it MUST stay a CONTIGUOUS
    # substring of the live title -- dropping a middle symbol would break focus
    # matching (WINDOW_NOT_FOCUSED). "绽放★青春全力向前冲！" -> "绽放★青春全力向前冲"
    # (★ kept, trailing ！ trimmed). Empty -> "" (e.g. a leading "【汉化】" bracket).
    head = re.split(r"[A-Za-z\[\](){}<>|/\\@【】「」『』（）［］]", stripped, maxsplit=1)[0]
    return re.sub(r"^[^一-鿿぀-ゟ゠-ヿｦ-ﾟ0-9]+|[^一-鿿぀-ゟ゠-ヿｦ-ﾟ0-9]+$", "", head)


class GalgameCompanionController:
    def __init__(
        self,
        game_memory: Any,
        capture: Any,
        locator: Any,
        ocr: Any,
        *,
        summarizer: Any = None,
        emit: CompanionEventSink | None = None,
        record_history: Callable[[str, str], None] | None = None,
        character_id: str = "spica",
        user_id: str = "麦",
        summary_trigger_chars: int = 2000,
        interval_seconds: float = 0.3,
        play_history_card_max_chars: int = 220,
        binding_sink: Any | None = None,
    ) -> None:
        self._game_memory = game_memory
        self._capture = capture
        self._locator = locator
        self._ocr = ocr
        self._summarizer = summarizer
        self._emit = emit or noop_companion_sink
        # Play-history bridge (B 方案, FINDINGS #15): same shape as the emit sink --
        # injected callable, default None. The controller only PRODUCES the card
        # text (game_id, card); write authority stays with the host's closure
        # (铁律 #8: galgame 对角色记忆只读).
        self._record_history = record_history
        # Phase 8-c1 (设计裁决 1/6): optional ActiveDomainRouter-shaped sink
        # (duck-typed: publish/retract). BEST-EFFORT by contract -- a sink
        # failure is logged and swallowed so it can never half-start a play or
        # break stop(); the local published snapshots below stay the galgame
        # domain's own truth (galgame-only closures keep reading THEM).
        self._binding_sink = binding_sink
        self._character_id = character_id
        self._user_id = user_id
        self._summary_trigger_chars = summary_trigger_chars
        self._interval_seconds = interval_seconds
        self._play_history_card_max_chars = play_history_card_max_chars
        self._lock = threading.RLock()
        self._session: GalgameCompanionSession | None = None
        self._runner: OcrStreamRunner | None = None
        # Published companion-turn binding (stage 2): written under the lock at
        # start/stop, read LOCK-FREE by current_game_context() (see its docstring).
        self._published_binding: GameTurnBinding | None = None
        # Published watch target (Phase 9): (game_id, window_id) for the
        # watch_game_screen tool. Same snapshot discipline as the binding --
        # published LAST on start, cleared FIRST on stop, lock-free read.
        self._published_watch_target: tuple[str, str] | None = None
        self.game_id: str | None = None  # set on start; kept after stop for later stages

    @property
    def is_active(self) -> bool:
        return self._session is not None

    @property
    def session(self) -> GalgameCompanionSession | None:
        return self._session

    def has_calibrated_dialog_region(self, game_id: str) -> bool:
        """True when the game's persisted ``GameProfile.ocr_profile`` carries a
        usable dialog region (stage 3, debt #8): the UI branches on this --
        silently reuse the calibration, or auto-open the calibration flow."""
        return self._profile_region_ratios(game_id)[0] is not None

    def _profile_region_ratios(self, game_id: str) -> tuple[Ratios | None, Ratios | None]:
        """(dialog_ratios, speaker_ratios) from the persisted GameProfile.ocr_profile;
        (None, None) when the game was never calibrated."""
        profile = self._game_memory.get_game_profile(game_id)
        if profile is None or not profile.ocr_profile:
            return None, None
        ocr_profile = OCRProfile.from_dict(profile.ocr_profile)
        return _region_ratios(ocr_profile.dialog_text_region), _region_ratios(ocr_profile.speaker_name_region)

    def current_game_context(self) -> GameTurnBinding | None:
        """The published companion-turn binding; ``None`` when not playing.

        LOCK-FREE on purpose: ``stop()`` holds the controller lock across
        ``session.end()`` -- whose final summary is an LLM call, i.e. seconds -- so
        a locked read here would stall the dialogue thread for that long. The
        binding is an immutable snapshot written under the lock (start publishes
        LAST, stop clears FIRST), and a single reference read is atomic, so a
        reader sees either a consistent binding or ``None`` -- both self-consistent
        (the gated stage reads the committed DB, never the live session).
        """
        return self._published_binding

    def current_watch_target(self) -> tuple[str, str] | None:
        """(game_id, window_id) of the live play, or ``None`` when not playing --
        what the watch_game_screen tool captures (Phase 9). Lock-free for the same
        reason as current_game_context: the tool runs on the turn's tool round and
        must never wait out a stop() holding the lock across the final summary."""
        return self._published_watch_target

    def start(
        self,
        window_id: str,
        *,
        game_id: str | None = None,
        window_title: str | None = None,
        dialog_ratios: Ratios | None = None,
        speaker_ratios: Ratios | None = None,
        match_rule: WindowMatchRule | None = None,
        interval_seconds: float | None = None,
        overlay_window_id: str | None = None,
    ) -> str:
        """Bind the game + start OCR loop. game_id from the caller (else guessed from
        window_title). ``dialog_ratios``/``speaker_ratios`` omitted -> read from the
        persisted GameProfile.ocr_profile (stage 3, debt #8: the calibration closes
        the loop); explicit args always win. ``interval_seconds`` overrides the OCR
        sampling interval for this play (None -> the controller's construct-time
        default). On ANY mid-start failure the half-built session is finalized (no
        dangling) and the controller stays unstarted, so it can be re-started."""
        with self._lock:
            if self._session is not None:
                raise GalgameCompanionError("companion already started; call stop() first")
            resolved = game_id or guess_game_id_from_title(window_title)
            if not resolved:
                raise GalgameCompanionError("game_id required (pass game_id, or a window_title to guess from)")
            if dialog_ratios is None:
                # Read the calibrated regions BEFORE building the session: an
                # uncalibrated game fails EARLY with nothing to clean up (no
                # PlaySession exists yet -> no dangling).
                profile_dialog, profile_speaker = self._profile_region_ratios(resolved)
                if profile_dialog is None:
                    raise GalgameCompanionError(
                        f"game {resolved!r} has no calibrated dialog region; "
                        "pass dialog_ratios or calibrate first (GameProfile.ocr_profile)"
                    )
                dialog_ratios = profile_dialog
                if speaker_ratios is None:
                    speaker_ratios = profile_speaker

            session = GalgameCompanionSession(
                self._game_memory,
                emit=self._emit,
                character_id=self._character_id,
                user_id=self._user_id,
                jobs=ThreadJobRunner(),
                summarizer=self._summarizer,
                summary_trigger_chars=self._summary_trigger_chars,
            )
            runner: OcrStreamRunner | None = None
            try:
                session.bind_game(resolved)  # idle -> game_launched
                session.start()  # game_launched -> playing (+ add_play_session, real DB)
                runner = OcrStreamRunner(
                    session, self._capture, self._locator, self._ocr,
                    interval_seconds=interval_seconds if interval_seconds is not None else self._interval_seconds,
                )
                runner.start(
                    window_id,
                    dialog_ratios=dialog_ratios,
                    match_rule=match_rule or WindowMatchRule(title_keywords=[resolved]),
                    speaker_ratios=speaker_ratios,
                    overlay_window_id=overlay_window_id,
                )
            except Exception:
                self._cleanup_failed_start(session, runner)  # finalize -> no dangling
                raise  # propagate; controller stays unstarted (self._session is None)

            self._session = session
            self._runner = runner
            self.game_id = resolved
            # Publish the companion-turn binding LAST (everything is up). v1 plays
            # the "default" playthrough; the dialogue thread reads this lock-free.
            self._published_binding = GameTurnBinding(
                conversation_id=game_conversation_id(resolved, "default"),
                game_context_request=GameContextRequest(
                    mode="active", game_id=resolved, playthrough_id="default",
                    # B1: scope the turn's [CURRENT_LINE] read to this live session
                    # (session.start() above has assigned session_id).
                    session_id=session.session_id,
                ),
            )
            self._published_watch_target = (resolved, window_id)  # Phase 9: watch tool target
            # Phase 8-c1: mirror the publish-LAST point into the domain router
            # (best-effort AFTER the local snapshots -- an exploding sink leaves
            # the play fully started and the local publish discipline intact).
            if self._binding_sink is not None:
                try:
                    self._binding_sink.publish("galgame", self._published_binding, priority=0)
                except Exception as exc:  # noqa: BLE001 -- sink must never break start
                    logger.warning("binding sink publish failed (ignored): %s", exc, exc_info=True)
            return resolved

    def stop(self) -> None:
        """Stop OCR + finalize the session (end() commits pending + final summary +
        marks ended). Safe + idempotent: no-op if never started / already stopped;
        never raises (a failing OCR-stop or end is logged, not propagated)."""
        with self._lock:
            # Clear the published binding FIRST: new turns revert to plain chat
            # immediately and never observe the session being finalized below.
            self._published_binding = None
            self._published_watch_target = None  # watch tool reverts to NO_ACTIVE_COMPANION
            # Phase 8-c1: mirror the clear-FIRST point into the domain router
            # (best-effort AFTER the local clears -- an exploding sink cannot
            # resurrect the binding or block the stop below).
            if self._binding_sink is not None:
                try:
                    self._binding_sink.retract("galgame")
                except Exception as exc:  # noqa: BLE001 -- sink must never break stop
                    logger.warning("binding sink retract failed (ignored): %s", exc, exc_info=True)
            runner, session = self._runner, self._session
            self._runner = None
            self._session = None
            if runner is not None:
                try:
                    runner.stop()  # stop OCR daemon + join (interrupts the wait)
                except Exception as exc:  # noqa: BLE001 -- stop must not crash mid-play
                    logger.warning("companion runner stop failed: %s", exc, exc_info=True)
            if session is not None and session.state in _ENDABLE:
                try:
                    session.end()  # drain summary jobs + commit pending + final summary + ended
                except Exception as exc:  # noqa: BLE001
                    logger.warning("companion session end failed: %s", exc, exc_info=True)
                else:
                    # B 方案 (FINDINGS #15): after a NORMAL end (summary + finalize
                    # landed), hand the play-history card to the injected recorder.
                    # Best-effort: a failure logs and never blocks stop.
                    self._record_play_history_safe(self.game_id)

    def _record_play_history_safe(self, game_id: str | None) -> None:
        if self._record_history is None or not game_id:
            return
        try:
            card = compose_play_history(
                self._game_memory, game_id, user_name=self._user_id,
                max_chars=self._play_history_card_max_chars,
            )
            if card:
                self._record_history(game_id, card)
        except Exception as exc:  # noqa: BLE001 -- best-effort: never block stop
            logger.warning("play history record failed for %s: %s", game_id, exc, exc_info=True)

    def _cleanup_failed_start(self, session: GalgameCompanionSession, runner: OcrStreamRunner | None) -> None:
        if runner is not None:
            try:
                runner.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("companion cleanup runner stop failed: %s", exc, exc_info=True)
        # If session.start() already created a live PlaySession, finalize it so it is
        # not left dangling (active with no ended_at). game_launched/idle have none.
        if session.state in _ENDABLE:
            try:
                session.end()
            except Exception as exc:  # noqa: BLE001
                logger.warning("companion cleanup session end failed: %s", exc, exc_info=True)
