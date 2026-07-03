"""Reaction domain assembly (OO migration Phase 4).

The WIRING half of the P5 reaction system, moved verbatim out of
``AppHost``: judge construction (incl. the independent judge endpoint fallback
tree) and engine assembly. The DECISION half lives in
``spica/galgame/reaction_scoring.py``; the write-authority closures (speak /
beat writer / dedupe reader) stay on the host.

FACADE CONTRACT (patch-validity, pinned by tests/test_reaction_judge.py):
``install(host)`` builds through ``host._new_reaction_judge()`` and
``host._build_reaction_engine()``; ``new_reaction_judge(host)`` takes its
adapter through ``host._judge_llm_adapter()``. The AppHost thin delegates are
the ONLY build path -- ``patch.object(AppHost, ...)`` must always intercept
(the ``test_moondream_default_cutover`` 15-patch depends on it). The delegates
are scheduled for deletion in Phase 5-c2, which migrates those patch targets
here in the same commit.

``host`` stays duck-typed ``Any``: this module must not import ``AppHost``
(app_host imports us -- the reverse edge would be a cycle).
"""

from __future__ import annotations

import logging
from typing import Any

from spica.galgame.reaction import ReactionEngine, ReactionModeParams, merge_mode_table
from spica.galgame.reaction_judge import GalgameReactionJudge
from spica.host.agent_assembly import build_llm_client

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
    is wired (so a half-config or a test never builds it). Model =
    reaction_judge_model, else the dialogue model -- same resolved adapter as
    the summarizer (mirrors _new_summarizer)."""
    if not host.config.galgame.reaction_judge_enabled:
        return None
    if host.services is None or host.services.llm_adapter is None:
        return None
    model = host.config.galgame.reaction_judge_model or host.config.llm.model
    # Facade contract: the adapter comes through the host delegate, never a
    # direct judge_llm_adapter(host) call (patch-validity pin).
    return GalgameReactionJudge(host._judge_llm_adapter(), model)


def judge_llm_adapter(host: Any) -> Any:
    """LLM adapter for the reaction judge -- its own endpoint (key + base_url),
    so the judge's load never saturates the main chat/summary endpoint (the
    deepseek-timeout-under-load root cause). Vendor-neutral: any OpenAI-compatible
    provider (deepseek/OpenAI/...; NOT Claude/Anthropic, which needs a separate
    messages-API adapter). Each knob falls back to the main LLM independently:
      key      = secrets.judge_api_key  (unset -> share the main adapter, zero change)
      base_url = galgame.reaction_judge_base_url or config.llm.base_url
      (model is resolved by the caller: reaction_judge_model or config.llm.model)
    Construction is network-free, so a bad key/url cannot break startup -- it
    surfaces on the first judge call, which the scoring policy catches and
    degrades to the lexicon gate."""
    judge_key = host.secrets.judge_api_key if host.secrets else None
    if not judge_key:
        logger.info("reaction judge: no JUDGE_API_KEY -> sharing the main LLM key/endpoint")
        return host.services.llm_adapter
    base_url = host.config.galgame.reaction_judge_base_url or host.config.llm.base_url
    client = build_llm_client(judge_key, base_url)
    logger.info("reaction judge: separate endpoint (JUDGE_API_KEY, base_url=%s)", base_url)
    return host.registry.resolve_llm(
        host.config.llm.provider, client=client,
        reasoning_effort=host.config.galgame.reaction_judge_reasoning_effort,
    )


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
