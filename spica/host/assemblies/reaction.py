"""Reaction domain assembly (OO migration Phase 4).

The WIRING half of the P5 reaction system, moved verbatim out of
``AppHost``: judge construction and engine assembly. The DECISION half lives
in ``spica/galgame/reaction_scoring.py``; the judge's endpoint/model decisions
(incl. the independent endpoint fallback tree) live in
``spica/host/model_router.py`` (Phase 6b); the write-authority closures
(speak / beat writer / dedupe reader) stay on the host.

FACADE CONTRACT (patch-validity, pinned by tests/test_reaction_judge.py):
``install(host)`` builds through ``host._new_reaction_judge()`` and
``host._build_reaction_engine()``; ``new_reaction_judge(host)`` takes its
adapter through ``host._judge_llm_adapter()`` (via
``host.model_router.for_role("judge")``, Phase 6b). The AppHost thin delegates are
the ONLY build path -- ``patch.object(AppHost, ...)`` must always intercept
(the ``test_moondream_default_cutover`` 15-patch depends on it). The delegates
are LONG-LIVED facades (D4 stop-clock, amendment 521f882) -- deletion is not
scheduled.

``host`` stays duck-typed ``Any``: this module must not import ``AppHost``
(app_host imports us -- the reverse edge would be a cycle).
"""

from __future__ import annotations

import logging
from typing import Any

from spica.galgame.reaction import ReactionEngine, ReactionModeParams, merge_mode_table
from spica.galgame.reaction_judge import GalgameReactionJudge

logger = logging.getLogger(__name__)


def install(host: Any) -> None:
    """Wire the reaction domain onto the host (called once from initialize()).

    Judge BEFORE engine so the scorer policy's live judge_provider sees it on
    the first beat -- same ordering the inline initialize() code had.
    """
    host._reaction_judge = host._new_reaction_judge()
    host.reaction_engine = host._build_reaction_engine()


def new_reaction_judge(host: Any) -> GalgameReactionJudge | None:
    """The P5 v2 reaction judge. None unless reaction_judge_enabled AND an LLM
    is wired (so a half-config or a test never builds it)."""
    if not host.config.galgame.reaction_judge_enabled:
        return None
    if host.services is None or host.services.llm_adapter is None:
        return None
    # Facade contract (Phase 6b, 方案 A-ii): for_role("judge") takes the adapter
    # THROUGH host._judge_llm_adapter() -- never a direct router.judge_adapter()
    # call -- so patch.object(AppHost, "_judge_llm_adapter", ...) keeps
    # intercepting real construction (patch-validity pin). Model resolution
    # (reaction_judge_model or the dialogue model) lives in the router too.
    return GalgameReactionJudge(host.model_router.for_role("judge"))


def build_reaction_engine(host: Any) -> ReactionEngine | None:
    """Assemble + start the reaction engine, or None when off. The mode is
    the typed ``galgame.reaction_mode`` (step 4-A), resolve-once (D-P5-4:
    restart-effective; the params lambda is the holder seam a future
    settings panel swaps without touching this assembly)."""
    mode = host.config.galgame.reaction_mode
    override_raw = host.config.galgame.reaction_table
    override = (
        {
            name: ReactionModeParams(
                min_score=tier.min_score,
                max_per_window=tier.max_per_window,
                cooldown_seconds=tier.cooldown_seconds,
            )
            for name, tier in override_raw.items()
        }
        if override_raw
        else None
    )
    params = merge_mode_table(override).get(mode)
    if params is None:
        # "off" by contract; anything else is schema-impossible (Literal),
        # kept as a defensive guard for hand-built configs in tests.
        if mode != "off":
            logger.warning("unknown galgame.reaction_mode %r -- reaction engine stays off", mode)
        return None
    engine = ReactionEngine(
        speak=host._reaction_speak,
        params_provider=lambda: params,
        scorer=host._reaction_scorer,
        beat_writer=host._write_reaction_beat,
        recent_for_dedupe=host._recent_reaction_beats,
        budget_window_seconds=host.config.galgame.reaction_budget_window_seconds,
    )
    engine.start()
    logger.info("reaction engine on (mode=%s)", mode)
    return engine
