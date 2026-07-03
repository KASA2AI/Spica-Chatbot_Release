"""Reaction scoring policy (OO migration Phase 4).

The DECISION half of the P5 reaction system, moved verbatim out of
``spica/host/app_host.py``: judge invocation, the judge cooldown state, the
per-game lexicon mtime hot-reload cache, and the judge-failure degradation to
lexicon scoring. The WIRING half (engine/judge construction) lives in
``spica/host/assemblies/reaction.py``; the write-authority closures (beat
writer / dedupe reader / speak handoff) STAY on the host (铁律 #9).

Dependency shape: everything the policy needs from the host arrives as a
PROVIDER callable resolved AT CALL TIME (live-read) -- never a captured value or
bound method. Tests (and a future settings panel) replace host attributes like
``_reaction_judge`` / ``_reaction_game_scope`` after construction, and the
policy must see the replacement. ``clock`` is injectable so the cooldown window
is testable without patching ``time`` module-wide (the Phase 4 patch-validity
requirement).

This module must not import ``spica.host`` (the policy knows nothing about the
host -- that direction would invert the assembly seam), ``spica.runtime.*``,
``spica.core.events``, or Qt. Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from common.timing import log_timing
from spica.galgame.reaction import (
    REACTION_MODE_TABLE,
    ReactionLexicon,
    ScoreResult,
    lexicon_source_mtime,
    load_reaction_lexicon,
    score_beat,
)
from spica.galgame.reaction_judge import ReactionJudgeError

logger = logging.getLogger(__name__)

REACTION_JUDGE_COOLDOWN_SECONDS = 15.0
REACTION_JUDGE_WINDOW_LINES = 24
_LEXICON_FALLBACK_PASS_SCORE = 1000


class ReactionScoringPolicy:
    """Scores reaction beats behind the engine's ``(beat) -> ScoreResult`` seam.

    The host wires ``score`` in via its ``_reaction_scorer`` thin delegate, so
    the engine (untouched, forbidden file) and its injection point are
    byte-identical to pre-Phase-4.
    """

    def __init__(
        self,
        *,
        config_provider: Callable[[], Any],
        game_scope_provider: Callable[[], Any],
        game_memory_provider: Callable[[], Any],
        character_scope_provider: Callable[[], Any],
        judge_provider: Callable[[], Any],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config_provider
        self._game_scope = game_scope_provider
        self._game_memory = game_memory_provider
        self._character_scope = character_scope_provider
        self._judge = judge_provider
        self._clock = clock
        # Judge-call throttle + per-game lexicon caches (moved host state).
        self._judge_last_at: float | None = None
        self._lexicons: dict[str | None, ReactionLexicon] = {}
        self._lexicon_mtimes: dict[str | None, float] = {}

    def lexicon_for(self, game_id: str | None) -> ReactionLexicon:
        """mtime-cached per-game lexicon (step 4-B hot-reload, mirrors
        VisualDiffService): a default.yaml / <game_id>.yaml edit is picked up on
        the next beat without a restart. Shared by the lexicon scorer (judge off)
        and the judge's failure fallback so both see the same hot-reloaded words.

        PUBLIC on purpose: this is the deterministic-lexicon seam tests override
        (instance-level ``policy.lexicon_for = lambda gid: ...``)."""
        mtime = lexicon_source_mtime(game_id)
        if (
            game_id not in self._lexicons
            or self._lexicon_mtimes.get(game_id) != mtime
        ):
            self._lexicons[game_id] = load_reaction_lexicon(game_id)
            self._lexicon_mtimes[game_id] = mtime
        return self._lexicons[game_id]

    def score(self, beat: Any) -> ScoreResult:
        """The scorer behind the engine's ``self._scorer(beat)`` seam (reaction.py
        L592). ENGINE UNTOUCHED: the signature is ``(beat) -> ScoreResult`` and the
        L396 injection are both unchanged.

        - Judge OFF (default) -> lexicon ``score_beat`` (byte-identical to pre-judge,
          zero diff).
        - Judge ON -> LLM worth via the judge, reading a scene WINDOW + arc from
          game_memory (the same data the prompt context injection reads), so it
          no longer misses wordless drama.
        - judge-cooldown -> a worth-0 sentinel (drops below any worth threshold)
          without an LLM call, throttling the rate.
        - ANY judge failure DEGRADES HERE, not in the engine: the engine's worker
          loop swallows scorer exceptions into a silent drop, so catching here is
          the only place a failure becomes lexicon scoring rather than silence."""
        scope = self._game_scope()
        game_id = scope[0] if scope else None
        lexicon = self.lexicon_for(game_id)
        judge = self._judge()
        if judge is None:
            return score_beat(beat, lexicon)  # judge off: zero-diff lexicon path

        now = self._clock()
        if (
            self._judge_last_at is not None
            and now - self._judge_last_at < REACTION_JUDGE_COOLDOWN_SECONDS
        ):
            return ScoreResult(0, ("judge_cooldown",))
        gm = self._game_memory()
        if scope is None or gm is None:
            return self._lexicon_fallback(beat, lexicon)  # no live scope -> lexicon scale

        game_id, playthrough_id, _ = scope
        config = self._config()
        identity = self._character_scope()
        character_id = identity.character_id
        user_id = identity.user_id
        judge_model = config.galgame.reaction_judge_model or config.llm.model
        judge_started = self._clock()
        try:
            window = gm.unsummarized_committed_story_lines(game_id, playthrough_id)
            verdict = judge.judge(
                beat_lines=list(beat.lines),
                window_lines=window[-REACTION_JUDGE_WINDOW_LINES:],
                recent_summaries=gm.recent_summaries(game_id, playthrough_id, limit=2),
                progress=gm.get_progress_state(game_id, playthrough_id),
                recent_beats=gm.recent_companion_beats_for_prompt(
                    game_id, user_id, character_id,
                    limit=config.galgame.prompt_context_recent_limit,
                ),
            )
        except ReactionJudgeError:
            # Telemetry: one line per ACTUAL judge call (degraded path). 不改行为.
            log_timing("reaction_judge", (self._clock() - judge_started) * 1000.0,
                       model=judge_model, degraded=True, lines=len(beat.lines))
            logger.warning("reaction judge failed -> lexicon fallback", exc_info=True)
            return self._lexicon_fallback(beat, lexicon)
        # Telemetry: judge latency + worth (correlate with the engine's downstream
        # "reaction decision: spoke|below_threshold" log to see if worth was selected).
        log_timing("reaction_judge", (self._clock() - judge_started) * 1000.0,
                   model=judge_model, worth=verdict.worth, angle=verdict.angle,
                   degraded=False, lines=len(beat.lines))
        # Only an ACTUAL judge call stamps the cooldown (cooldown returns / fallback
        # do not), so the throttle measures spacing between real LLM calls.
        self._judge_last_at = now
        return ScoreResult(
            score=verdict.worth,
            reasons=(f"worth:{verdict.worth}", f"moment:{verdict.moment}", f"angle:{verdict.angle}"),
        )

    def _lexicon_fallback(self, beat: Any, lexicon: ReactionLexicon) -> ScoreResult:
        """叉口②-b: judge unavailable -> lexicon scoring on the LEXICON scale.
        Decide pass/fail against the CODE ``REACTION_MODE_TABLE`` (lexicon weight
        scale) -- NOT the worth-scale ``reaction_table`` the engine gates the judge
        with -- then return a pass/fail-encoded score so the engine's worth
        threshold can never silence a lexicon-passing beat (两套阈, 不沉默不崩)."""
        lex = score_beat(beat, lexicon)
        tier = REACTION_MODE_TABLE.get(self._config().galgame.reaction_mode)
        passed = tier is not None and lex.score >= tier.min_score
        return ScoreResult(
            score=(_LEXICON_FALLBACK_PASS_SCORE if passed else 0),
            reasons=("lexicon_fallback",) + lex.reasons,
        )
