"""Host-side model/endpoint role router (OO migration Phase 6b, 方案 A-ii).

The ONE home for the host's endpoint/model decisions: which model name each
role runs on (``summary`` / ``judge`` / ``dialogue``) and the reaction judge's
independent-endpoint fallback tree (key / base_url / reasoning). Consumers
receive a ``BoundModel`` via ``for_role`` and never resolve endpoints or model
names themselves.

FACADE CONTRACT (Phase 4/6b, pinned by tests/test_reaction_judge.py +
tests/test_model_router.py): ``for_role("judge")`` takes its adapter THROUGH
``host._judge_llm_adapter()`` -- never by calling ``judge_adapter()`` directly
-- so ``patch.object(AppHost, "_judge_llm_adapter", ...)`` keeps intercepting
real construction (the moondream cutover 15-patch depends on the same seam).
The call chain is one-way: ``for_role("judge") -> host._judge_llm_adapter()
-> router.judge_adapter()``.

``host`` stays duck-typed ``Any``: this module must not import ``AppHost``
(app_host imports us -- the reverse edge would be a cycle; the assemblies
precedent). The constructor is INERT -- it stores the host reference and reads
NO config/services and does NO I/O; every decision resolves per call
(live-read, the ReactionScoringPolicy precedent).
"""

from __future__ import annotations

import logging
from typing import Any

from spica.host.agent_assembly import build_llm_client
from spica.ports.model import BoundModel

logger = logging.getLogger(__name__)


class ModelRouter:
    def __init__(self, host: Any) -> None:
        self._host = host

    def role_model(self, role: str) -> str:
        """The model name for a role. summary/judge fall back to the dialogue
        model INDEPENDENTLY -- byte-for-byte the historical per-site
        expressions (_new_summarizer / assemblies.new_reaction_judge)."""
        config = self._host.config
        if role == "summary":
            return config.galgame.summary_model or config.llm.model
        if role == "judge":
            return config.galgame.reaction_judge_model or config.llm.model
        return config.llm.model

    def for_role(self, role: str) -> BoundModel:
        """The role's BoundModel, resolved per call.

        judge: the adapter comes THROUGH ``host._judge_llm_adapter()`` (facade
        contract -- see module docstring); summary/dialogue ride the main
        resolved adapter."""
        if role == "judge":
            return BoundModel(self._host._judge_llm_adapter(), self.role_model("judge"))
        return BoundModel(self._host.services.llm_adapter, self.role_model(role))

    def judge_adapter(self) -> Any:
        """LLM adapter for the reaction judge -- its own endpoint (key +
        base_url), so the judge's load never saturates the main chat/summary
        endpoint (the deepseek-timeout-under-load root cause). Moved VERBATIM
        from ``assemblies/reaction.judge_llm_adapter`` (Phase 6b); reached only
        through the ``AppHost._judge_llm_adapter`` delegate. Vendor-neutral:
        any OpenAI-compatible provider (deepseek/OpenAI/...; NOT
        Claude/Anthropic, which needs a separate messages-API adapter). Each
        knob falls back to the main LLM independently:
          key      = secrets.judge_api_key  (unset -> share the main adapter, zero change)
          base_url = galgame.reaction_judge_base_url or config.llm.base_url
          (model is resolved by ``role_model("judge")``)
        Construction is network-free, so a bad key/url cannot break startup --
        it surfaces on the first judge call, which the scoring policy catches
        and degrades to the lexicon gate."""
        host = self._host
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
