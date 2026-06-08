"""Synchronous stage chain (C4: moved from agent/runtime.py).

Drives the stages in order for the non-streaming path. C5: builds the typed deps
once -- with a per-turn observer wrapping ctx.timing -- and threads it through
every stage so they share one timing sink (done.timing / response_payload). Used
by the golden/smoke/memory tests; the production sync path is ChatEngine.run_voice
(run_turn + fold). Qt-free.
"""

from __future__ import annotations

from dataclasses import replace

from spica.runtime.context import TurnContext
from spica.runtime.deps import TurnDeps
from spica.runtime.memory_commit import save_stream_memory
from spica.runtime.observer import DefaultTurnObserver
from spica.runtime.services import AgentServices
from spica.runtime.stages import (
    analyze_screen_attachment_node,
    build_prompt_node,
    build_response_node,
    build_visual_node,
    call_llm_node,
    load_recent_context_node,
    parse_reply_node,
    retrieve_long_term_memory_node,
    synthesize_tts_node,
    validate_input_node,
)


def run_voice_pipeline(ctx: TurnContext, services: AgentServices) -> TurnContext:
    deps = TurnDeps.from_legacy_services(services)
    deps = replace(deps, observer=DefaultTurnObserver(ctx.timing, logger=services.logger))
    ctx = validate_input_node(ctx, services, deps)
    ctx = load_recent_context_node(ctx, services, deps)
    ctx = retrieve_long_term_memory_node(ctx, services, deps)
    ctx = analyze_screen_attachment_node(ctx, services, deps)
    ctx = build_prompt_node(ctx, services, deps)
    ctx = call_llm_node(ctx, services, deps)
    ctx = parse_reply_node(ctx, services, deps)
    # Unified with the streaming path (Phase 6D): one memory-commit component,
    # not a separate save_recent + extract pair. Skipped on error (e.g. empty input).
    if not ctx.error:
        save_stream_memory(ctx, services, deps)
    ctx = build_visual_node(ctx, services, deps)
    ctx = synthesize_tts_node(ctx, services, deps)
    ctx = build_response_node(ctx, services, deps)
    return ctx
