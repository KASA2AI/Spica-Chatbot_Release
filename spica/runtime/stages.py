"""Turn stages (C4: moved from agent/nodes.py; C5: timing via TurnObserver).

Each stage is ``(ctx, services, deps) -> ctx``: it reads/writes its own TurnContext
sub-object (C3c) and never emits events. Tunables, ports and per-turn state all
come from the typed ``deps`` (Phase 5 single-track: config / llm / memory /
recent / llm_ready / available_tool_schema_count / visual / tts); ``services``
stays only as the positional signature slot (guard-banned for attribute reads
here -- test_no_dict_config).

Timing + structured logging go through ``deps.observer`` (C5): ``span`` for a timed
node, ``mark`` / ``mark_once`` / ``bump`` for stored values, ``event`` for a
diagnostic log line. No stage calls ``log_timing`` or touches ``ctx.timing``
directly (N4-observe). The observer's sink IS ``ctx.timing``, so ``done.timing`` is
unchanged. N3-config only bans ``services.config`` / ``services.llm_adapter`` /
``services.memory_adapter`` here, which this module no longer touches.

Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from functools import wraps
from typing import Any, Callable
from concurrent.futures import CancelledError

from spica.conversation.character_loader import DEFAULT_CHARACTER_NAME, DEFAULT_INTERLOCUTOR_NAME
from spica.conversation.prompt_builder import (
    DEFAULT_CHARACTER_PROFILE,
    TOOL_EXECUTION_INSTRUCTIONS,
    append_prompt_context_sections,
    build_spica_prompt,
)
from spica.conversation.reply_parser import EMOTION_LABELS, normalize_emotion, parse_model_reply
from spica.conversation.text_normalizer import normalize_square_brackets_for_speech
from spica.conversation.time_context import build_local_time_context
from spica.runtime.services import AgentServices
from common.timing import elapsed_ms, now_ms
from agent_tools.function_tools.screen.analyzer import (
    analyze_screen_attachment,
    clear_last_screen_analysis_metadata,
    get_last_screen_analysis_metadata,
)
from agent_tools.function_tools.screen.schema import (
    ScreenToolError,
    compact_screen_observation_for_prompt,
)
from agent_tools.tts.schemas import TTSRequest, TTSResult
from spica.runtime.context import (
    PromptBundle,
    RetrievedContext,
    StreamedAnswer,
    TurnContext,
    TurnError,
    is_turn_cancelled,
    turn_error_to_legacy_dict,
)
from spica.runtime.deps import TurnDeps
from spica.runtime.observer import NoopTurnObserver
from spica.runtime.scope import MemoryScopeStrategy


logger = logging.getLogger(__name__)

DEFAULT_SCREEN_ATTACHMENT_QUESTION = "请查看这张截图并概括内容。"

# Stages take (ctx, services, deps). deps may be None for direct (dict-config)
# callers (the compat sync chain / tests); stages that need config or a port
# bridge it here -- the same N3-clean pattern memory_commit uses. node_timer
# threads deps through and times the node via the injected observer (C5).
def node_timer(func: Callable[..., TurnContext]):
    @wraps(func)
    def wrapper(ctx: TurnContext, services: AgentServices, deps: Any = None, **kwargs: Any) -> TurnContext:
        observer = deps.observer if deps is not None else NoopTurnObserver()
        with observer.span(func.__name__, conversation_id=ctx.request.conversation_id):
            try:
                return func(ctx, services, deps, **kwargs)
            except CancelledError:
                raise
            except Exception as exc:
                if ctx.error is None:
                    ctx.error = TurnError("NODE_FAILED", f"{func.__name__}: {exc}")
                return ctx

    return wrapper


def _skip_if_error(ctx: TurnContext) -> bool:
    return ctx.error is not None


def _get_attr(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _tts_adapter_name(deps: Any) -> str:
    return str(getattr(deps.tts, "name", None) or "tts")


def _build_tts_request(ctx: TurnContext, text: str, emotion: str) -> TTSRequest:
    return TTSRequest(
        text=text,
        emotion=emotion,
        extra={"tts_param_overrides": ctx.request.tts_param_overrides or {}},
    )


def _legacy_tts_chunks(result: TTSResult) -> list[str]:
    return [
        str(chunk.get("text") or "")
        for chunk in result.chunks
        if isinstance(chunk, dict) and chunk.get("text")
    ]


def _legacy_tts_chunk_audio(result: TTSResult) -> list[dict[str, Any]]:
    return [
        dict(chunk)
        for chunk in result.chunks
        if isinstance(chunk, dict) and (chunk.get("audio_path") or chunk.get("audio_url"))
    ]


@node_timer
def validate_input_node(ctx: TurnContext, services: AgentServices, deps: Any = None) -> TurnContext:
    ctx.user_input = (ctx.request.user_input or "").strip()
    if not ctx.user_input and ctx.request.screen_attachment:
        ctx.user_input = DEFAULT_SCREEN_ATTACHMENT_QUESTION
    if ctx.request.include_user_time_context and not ctx.user_local_time:
        ctx.user_local_time = build_local_time_context()
    ctx.metadata["user_local_time"] = ctx.user_local_time if ctx.request.include_user_time_context else None
    ctx.metadata["interaction_mode"] = ctx.request.interaction_mode
    ctx.metadata["has_screen_attachment"] = bool(ctx.request.screen_attachment)
    if not ctx.user_input:
        # Empty input sets ONLY the error: build_response owns the answer/emotion
        # fallback (byte-identical strings), so no intermediate stage reads a
        # validate-written answer (C3c guardrail 3).
        ctx.error = TurnError("EMPTY_MESSAGE", "message 不能为空。")
    else:
        from spica.runtime.memory_commit import record_turn_input
        deps = deps or TurnDeps.from_legacy_services(services)
        record_turn_input(ctx, deps)
        from spica.runtime.context import is_domain_conversation
        note_reply = getattr(deps, "note_media_reply", None)
        if (ctx.error is None and callable(note_reply)
                and ctx.request.interaction_mode == "chat" and not is_domain_conversation(ctx.request.conversation_id)):
            result = note_reply(ctx.user_input)
            if result is not None:
                from spica.runtime.memory_commit import record_turn_result
                record_turn_result(ctx, deps, kind="tool", content=json.dumps(result, ensure_ascii=False),
                                   event_suffix="media-reply", source="anime_playback_reply", metadata={"actor": "business"})
                ctx.metadata["media_reply_result"] = result
    return ctx


@node_timer
def load_recent_context_node(ctx: TurnContext, services: AgentServices, deps: Any = None) -> TurnContext:
    if _skip_if_error(ctx):
        return ctx
    deps = deps or TurnDeps.from_legacy_services(services)
    # Phase 5: the recent-turn buffer is a deps-resolved capability (deps.recent,
    # bridge-filled from the host bundle) -- the old C6 TODO is settled.
    #
    # Phase 2: the bucket key is character-scoped ({character_id}::{conversation_id})
    # via MemoryScopeStrategy -- symmetric with memory_commit's append key, so two
    # characters sharing a conversation_id can no longer read each other's recent
    # context. memory/recent.py stays a dumb store; ALL key derivation lives in the
    # strategy.
    working_context = getattr(deps.memory, "working_context", None)
    if callable(working_context):
        scope = MemoryScopeStrategy(deps.config).evidence_scope(ctx.request, deps.personal_owner_id)
        context = working_context(scope, ctx.request.material_query, exclude_turn_id=ctx.request.evidence_turn_id,
            token_budget=900 if ctx.request.material_hint is not None and ctx.request.interaction_mode == 'system' else 2400,
            **({'cancelled': ctx.request.cancelled} if ctx.request.cancelled is not None else {}),
            **({'event_binding': ctx.request.event_binding} if ctx.request.event_binding is not None else {}))
        ctx.recent = RetrievedContext(working_messages=context["messages"])
        ctx.metadata["working_context_status"] = context["status"]
        ctx.metadata["context_evidence_ids"] = sorted(set(context.get("evidence_ids", []))
                                                      | set(context.get("summary_source_ids", [])))
        ctx.metadata["context_evidence_revisions"] = context.get("evidence_revisions", {})
        ctx.metadata["recent_context_count"] = len(context["messages"])
        return ctx
    recent = deps.recent.get_recent(
        MemoryScopeStrategy(deps.config).recent_key(ctx.request),
        limit=deps.config.memory.recent_context_limit,
    )
    if ctx.recent is None:
        ctx.recent = RetrievedContext()
    ctx.recent.recent_context = recent
    ctx.metadata["recent_context_count"] = len(recent)
    return ctx


@node_timer
def retrieve_long_term_memory_node(ctx: TurnContext, services: AgentServices, deps: Any = None) -> TurnContext:
    if _skip_if_error(ctx):
        return ctx
    deps = deps or TurnDeps.from_legacy_services(services)
    # Read through MemoryPort.retrieve so the read key matches commit_turn's
    # character-namespaced write key (Phase 5/7). A bare conversation_id here
    # silently misses every auto-extracted memory.
    #
    # §27①: use effective_memory_conversation_id, NOT the raw conversation_id, so a
    # galgame turn (conversation_id = galgame::<game>::...) can keep
    # memory_conversation_id = "default" and still read Spica's long-term character
    # memory. For a plain chat turn memory_conversation_id is unset -> this falls
    # back to conversation_id -> byte-identical behaviour (golden unchanged).
    # Phase 2: the scope triple comes from MemoryScopeStrategy (same values as the
    # old hand-built MemoryScope; write-side symmetry with memory_commit is now
    # structural, not a comment).
    scope = MemoryScopeStrategy(deps.config).ltm_scope(ctx.request, deps.personal_owner_id)
    items = deps.memory.retrieve(
        scope,
        ctx.request.material_query,
        limit=deps.config.memory.long_term_memory_limit,
    )
    ctx.metadata["memory_retrieval_status"] = getattr(items, "status", "matched" if items else "no_match")
    # build_spica_prompt / _format_memories consume dicts (scope / content /
    # memory_type); map MemoryItem back so the prompt's scope label survives.
    memories = [
        {
            "scope": item.scope,
            "content": item.text,
            "memory_type": item.type,
            "importance": item.importance,
            "score": item.score,
            "id": getattr(item, "id", None),
            "source_ids": getattr(item, "source_ids", ()),
            "source_quotes": getattr(item, "source_quotes", ()),
            "source_times": getattr(item, "source_times", ()),
            "revision": getattr(item, "revision", 1),
            "revision_reason": getattr(item, "revision_reason", None),
            "status": getattr(item, "status", "active"),
            "valid_from": getattr(item, "valid_from", None),
            "valid_until": getattr(item, "valid_until", None),
        }
        for item in items
    ]
    if ctx.recent is None:
        ctx.recent = RetrievedContext()
    ctx.recent.long_term_memories = memories
    ctx.metadata["long_term_memory_count"] = len(memories)
    return ctx


@node_timer
def analyze_screen_attachment_node(ctx: TurnContext, services: AgentServices, deps: Any = None) -> TurnContext:
    if _skip_if_error(ctx):
        return ctx
    if not ctx.request.screen_attachment:
        return ctx
    deps = deps or TurnDeps.from_legacy_services(services)
    obs = deps.observer

    started_ms = now_ms()
    clear_last_screen_analysis_metadata()
    try:
        observation = analyze_screen_attachment(
            attachment=ctx.request.screen_attachment,
            user_question=ctx.user_input,
        )
        ctx.screen_observation = observation
        from agent_tools.function_tools.screen.schema import screen_observation_context_for_next_turn
        from spica.runtime.memory_commit import record_turn_result
        screen_context = screen_observation_context_for_next_turn(observation)
        if screen_context:
            record_turn_result(ctx, deps, kind="environment", content=screen_context,
                               event_suffix="screen-observation", source="past_screen_observation")
        duration = elapsed_ms(started_ms)
        obs.mark("screen_analysis_ms", duration)
        ctx.metadata["screen_observation_used"] = True
        ctx.metadata["screen_observation_schema"] = observation.get("schema_version")
        ctx.metadata["screen_observation_target"] = (observation.get("request") or {}).get("target")
        _record_screen_analysis_metadata(ctx, obs)
        ctx.tools.append(
            {
                "name": "screen_analyzer",
                "required": True,
                "ok": True,
                "target": (observation.get("request") or {}).get("target"),
                "source": (observation.get("capture") or {}).get("source"),
            }
        )
        obs.event(
            "screen_attachment_analysis",
            duration,
            target=(observation.get("request") or {}).get("target"),
            source=(observation.get("capture") or {}).get("source"),
            stream_enabled=obs.snapshot().get("screen_analysis_stream_enabled"),
            stream_fallback_used=obs.snapshot().get("screen_analysis_stream_fallback_used"),
            first_delta_ms=obs.snapshot().get("screen_analysis_first_delta_ms"),
        )
    except ScreenToolError as exc:
        duration = elapsed_ms(started_ms)
        obs.mark("screen_analysis_ms", duration)
        _record_screen_analysis_metadata(ctx, obs)
        # Tool-audit record (its own shape), not a TurnError serialization.
        ctx.tools.append(
            {
                "name": "screen_analyzer",
                "required": True,
                "ok": False,
                "error": {"code": exc.code, "message": exc.message},
            }
        )
        ctx.error = TurnError(exc.code, exc.message)
    except Exception as exc:
        duration = elapsed_ms(started_ms)
        obs.mark("screen_analysis_ms", duration)
        _record_screen_analysis_metadata(ctx, obs)
        ctx.tools.append(
            {
                "name": "screen_analyzer",
                "required": True,
                "ok": False,
                "error": {"code": "SCREEN_ANALYSIS_FAILED", "message": str(exc)},
            }
        )
        ctx.error = TurnError("SCREEN_ANALYSIS_FAILED", str(exc))
    return ctx


def estimated_context_tokens(value: Any) -> int:
    text = json.dumps(value, ensure_ascii=False)
    return (sum(4 if ord(char) > 127 else 1 for char in text) + 3) // 4


def check_context_sources(ctx: TurnContext) -> bool:
    from spica.ports.memory import EvidenceAdmissionError
    try:
        if ctx.before_model_request is not None:
            ctx.before_model_request(ctx)
        return True
    except EvidenceAdmissionError:
        return False  # the shared callback has already set the concrete TurnError


def check_context_budget(ctx: TurnContext, deps: Any, prompt: Any, schemas=()) -> bool:
    """Check each actual personal model request, including tool continuations."""
    if not check_context_sources(ctx):
        return False
    if ctx.recent is None or ctx.recent.working_messages is None:
        return True
    instructions = ctx.prompt.model_instructions if ctx.prompt else ''
    total = (estimated_context_tokens(prompt) + estimated_context_tokens(instructions)
             + estimated_context_tokens(schemas))
    ctx.metadata['prompt_estimated_tokens'] = total
    if total <= deps.config.memory.context_token_budget:
        return True
    ctx.error = TurnError('CONTEXT_BUDGET_EXCEEDED',
        '本轮内容超过上下文预算。已执行的工具结果已保留；请缩小查询范围或提高上下文预算，不要重复提交操作。')
    return False


@node_timer
def build_prompt_node(ctx: TurnContext, services: AgentServices, deps: Any = None, *, context_sections=()) -> TurnContext:
    if _skip_if_error(ctx):
        return ctx
    deps = deps or TurnDeps.from_legacy_services(services)
    recent_context = ctx.recent.recent_context if ctx.recent else []
    long_term_memories = ctx.recent.long_term_memories if ctx.recent else []
    selected_memory_ids = []
    prompt_options = dict(
        user_input=ctx.user_input,
        interaction_mode=ctx.request.interaction_mode,
        input_source=ctx.request.input_source,
        input_modality=ctx.request.input_modality,
        recent_context=recent_context,
        long_term_memories=long_term_memories,
        character_profile=str(deps.config.character.character_profile or DEFAULT_CHARACTER_PROFILE),
        memory_limit=deps.config.memory.long_term_memory_limit,
        memory_budget_chars=min(deps.config.memory.long_term_memory_budget_chars,
            400 if ctx.request.material_hint is not None and ctx.request.interaction_mode == 'system' else 700),
        recent_turn_char_limit=deps.config.memory.recent_turn_char_limit,
        interlocutor_name=str(deps.config.character.interlocutor_name or DEFAULT_INTERLOCUTOR_NAME),
        character_name=str(deps.config.character.character_name or DEFAULT_CHARACTER_NAME),
        user_local_time=ctx.user_local_time if ctx.request.include_user_time_context else None,
        dialog_display_language=str(deps.config.character.dialog_display_language or "ja"),
        personal_continuity=ctx.recent.personal_continuity if ctx.recent else (),
        memory_retrieval_status='unavailable' if ctx.metadata.get('working_context_status') == 'unavailable'
            else ctx.metadata.get("memory_retrieval_status", "no_match"),
        working_messages=ctx.recent.working_messages if ctx.recent else None,
        selected_memory_ids=selected_memory_ids,
        material_hint=ctx.request.material_hint,
    )
    def compose():
        prompt = build_spica_prompt(**prompt_options)
        if ctx.metadata.get("media_reply_result") is not None:
            # This is the current business owner's result. It must survive
            # history compaction and be counted with the required current input.
            prompt = append_prompt_context_sections(prompt, [
                "[CURRENT_BUSINESS_RESULT source=anime_playback_reply]\n"
                + json.dumps(ctx.metadata["media_reply_result"], ensure_ascii=False)
            ], dialog_display_language=str(deps.config.character.dialog_display_language or "ja"))
        if ctx.screen_observation:
            prompt = _inject_screen_observation(prompt, ctx.screen_observation,
                dialog_display_language=str(deps.config.character.dialog_display_language or "ja"))
        if context_sections:
            prompt = append_prompt_context_sections(prompt, context_sections,
                dialog_display_language=str(deps.config.character.dialog_display_language or "ja"))
        return prompt

    prompt_input = compose()
    instructions = ""
    if ctx.request.interaction_mode != "system":
        instructions = "\n\n".join(filter(None, (instructions, TOOL_EXECUTION_INSTRUCTIONS)))
    if ctx.recent is not None and ctx.recent.working_messages is not None:
        estimated_tokens = estimated_context_tokens
        schemas = [] if ctx.request.interaction_mode == "system" or ctx.screen_observation else deps.tools.schemas_for_user_text(ctx.user_input)
        overhead = estimated_tokens(instructions) + estimated_tokens(schemas)
        total = estimated_tokens(prompt_input) + overhead
        budget = deps.config.memory.context_token_budget
        if total > budget:
            # Optional author examples/background and expression hints yield
            # before real dialogue or tool receipts. Recompose whole blocks.
            prompt_options['character_material_budget'] = 0
            prompt_input = compose()
            total = estimated_tokens(prompt_input) + overhead
        if total > budget:
            # System/role/current input stay intact. Select whole historical
            # exchanges with the budget left by the actual assembled contract.
            prompt_options["working_messages"] = []
            base = compose()
            available = max(0, budget - overhead - estimated_tokens(base))
            if not available and long_term_memories:
                prompt_options["long_term_memories"] = []
                ctx.recent.long_term_memories = []
                base = compose()
                available = max(0, budget - overhead - estimated_tokens(base))
            scope = MemoryScopeStrategy(deps.config).evidence_scope(ctx.request, deps.personal_owner_id)
            selected = deps.memory.working_context(scope, ctx.request.material_query,
                exclude_turn_id=ctx.request.evidence_turn_id, token_budget=available,
                **({'cancelled': ctx.request.cancelled} if ctx.request.cancelled is not None else {}),
                **({'event_binding': ctx.request.event_binding} if ctx.request.event_binding is not None else {}))
            ctx.metadata['working_context_status'] = selected['status']
            if selected['status'] == 'unavailable':
                prompt_options['memory_retrieval_status'] = 'unavailable'
            ctx.recent.working_messages = selected["messages"]
            ctx.metadata["context_evidence_ids"] = sorted(set(selected.get("evidence_ids", [])) | set(selected.get("summary_source_ids", [])))
            ctx.metadata["context_evidence_revisions"] = selected.get("evidence_revisions", {})
            prompt_options["working_messages"] = selected["messages"]
            prompt_input = compose()
            total = estimated_tokens(prompt_input) + overhead
        ctx.metadata["prompt_estimated_tokens"] = total
        if total > budget:
            # The remaining role contract/current input cannot be cut into
            # fragments or silently submitted beyond the configured budget.
            ctx.error = TurnError("CONTEXT_BUDGET_EXCEEDED", "本轮必要内容超过上下文预算，精简背景和历史后仍无法容纳；请检查角色核心、工具说明或本次输入长度。")
            return ctx
    ctx.prompt = PromptBundle(prompt_input=prompt_input, model_instructions=instructions)
    ctx.metadata['memory_entry_ids'] = list(selected_memory_ids)
    validate_context = getattr(deps.memory, 'context_is_current', None)
    if callable(validate_context):
        from spica.ports.memory import EvidenceAdmissionError
        scope = MemoryScopeStrategy(deps.config).evidence_scope(ctx.request, deps.personal_owner_id)

        def before_model_request(ctx):
            dependencies = {
                'input_evidence_id': ctx.metadata.get('input_evidence_id'),
                'context_evidence_ids': sorted(set(ctx.metadata.get('context_evidence_ids', []))
                    | set(ctx.metadata.get('result_evidence_ids', []))),
                'context_evidence_revisions': ctx.metadata.get('context_evidence_revisions', {}),
                'memory_entry_ids': ctx.metadata.get('memory_entry_ids', []),
            }
            try:
                current = validate_context(scope, dependencies)
            except Exception as exc:
                ctx.error = TurnError('MEMORY_CONTEXT_UNAVAILABLE',
                    '暂时无法核实本轮记忆来源，已停止后续回复；已执行的操作不会重试。')
                raise EvidenceAdmissionError(ctx.error.message) from exc
            if not current:
                ctx.error = TurnError('MEMORY_CONTEXT_WITHDRAWN',
                    '本轮使用的资料已修正或删除，已停止后续回复；已执行的操作不会重试。')
                raise EvidenceAdmissionError(ctx.error.message)

        ctx.before_model_request = before_model_request
    ctx.metadata["prompt_input_chars"] = len(str(prompt_input))
    return ctx


# B3 / Phase 3: gated context injection inserted AFTER build_prompt, BEFORE the
# LLM call. The node is GENERIC (OO migration Phase 3): each registered
# PromptContextContributor's ``mode(request)`` gate is pure request-field logic
# (NEVER an LLM call -- CLAUDE.md #1.3). All-"none" turns are a byte-level no-op
# (no span opened, ctx.timing untouched) -- that is what keeps a plain chat
# turn's prompt + ctx identical. Injection mirrors _inject_screen_observation:
# append sections to the prompt; personal turns recompose with the same budget.
# The galgame gate/target logic lives in
# spica/galgame/context_contributor.py; the section builders in
# spica/galgame/prompt_sections.py.


def contribute_context_node(ctx: TurnContext, services: AgentServices, deps: Any = None) -> TurnContext:
    """Read active contributors once and assemble within the personal budget.

    All-"none" is a byte-level no-op, with gates before the existing timing span.
    Failed reads are logged and omitted; valid sections use the same prompt
    builder to reserve their space before selecting complete historical turns.
    """
    if ctx.error is not None:
        # Prior-error turns return untouched WITHOUT resolving deps/services --
        # the pre-Phase-3 node never bridged deps on this path (alias compat;
        # pinned by test_prompt_context_contributors.PriorErrorCompatTest).
        return ctx
    deps = deps or TurnDeps.from_legacy_services(services)
    active: list[tuple[Any, str]] = []
    for contributor in deps.context_contributors or ():
        try:
            mode = contributor.mode(ctx.request)
        except Exception:  # noqa: BLE001 -- a broken gate must not break plain chat
            logger.warning(
                "contribute_context_node: contributor %s mode() failed; treated as none",
                getattr(contributor, "name", repr(contributor)),
                exc_info=True,
            )
            continue
        if mode in ("active", "offline"):
            active.append((contributor, mode))
    if not active:
        # Byte-level no-op: no span opened, no ctx mutation, prompt untouched.
        return ctx
    observer = deps.observer
    with observer.span("retrieve_game_context_node", conversation_id=ctx.request.conversation_id):
        if ctx.prompt is None:
            return ctx
        sections: list[str] = []
        for contributor, mode in active:
            try:
                sections.extend(contributor.sections(ctx, deps, mode))
            except Exception as exc:  # noqa: BLE001 -- best-effort: never fail the turn
                # Do NOT silently swallow: surface the failure on the logging layer
                # so a broken domain read is diagnosable.
                logger.warning(
                    "contribute_context_node: context read failed "
                    "(contributor=%s, mode=%s, conversation_id=%s): %s",
                    getattr(contributor, "name", repr(contributor)),
                    mode,
                    ctx.request.conversation_id,
                    exc,
                    exc_info=True,
                )
        if sections and ctx.recent is not None and ctx.recent.working_messages is not None:
            return build_prompt_node(ctx, services, deps, context_sections=sections)
        if sections:
            base = ctx.prompt.prompt_input or ""
            ctx.prompt = replace(ctx.prompt,
                prompt_input=append_prompt_context_sections(
                    base,
                    sections,
                    dialog_display_language=str(
                        deps.config.character.dialog_display_language or "ja"
                    ),
                )
            )
    return ctx


# PERMANENT compatibility alias (D2): direct importers/callers -- orchestrator
# history, the frozen sync chain lineage and four test files -- keep working
# forever. Must stay a PURE ASSIGNMENT (guarded by AST test); never re-def.
retrieve_game_context_node = contribute_context_node


@node_timer
def call_llm_node(ctx: TurnContext, services: AgentServices, deps: Any = None) -> TurnContext:
    from spica.runtime.context import is_domain_conversation
    if _skip_if_error(ctx):
        return ctx
    deps = deps or TurnDeps.from_legacy_services(services)
    # Phase 5: readiness is the bridge-computed flag, NOT ``deps.llm is None``
    # (from_services wraps even a None client in an adapter, so deps.llm is
    # never None -- test_llm_client_not_configured pins this branch).
    if not deps.llm_ready:
        ctx.error = TurnError("LLM_CLIENT_NOT_CONFIGURED", "LLM client 未配置。")
        return ctx
    obs = deps.observer

    model = deps.config.llm.model
    max_rounds = max(1, int(deps.config.max_tool_rounds))
    prompt_input = ctx.prompt.prompt_input if ctx.prompt else None
    cap_output = (is_domain_conversation(ctx.request.conversation_id)
                  or ctx.recent is None or ctx.recent.working_messages is None)
    # C7: tools resolve through the registry-backed ToolSet (deps.tools); the intent
    # gate lives inside schemas_for_user_text. available_tool_schema_count stays the
    # injected legacy count (telemetry; equals the registry's built-in tool set).
    # P3 safety gate mirrored from tool_round (system turns get no tools); the
    # frozen sync chain never carries production system turns, but the two
    # branches must agree on the rule.
    active_tool_schemas = (
        []
        if (
            ctx.request.screen_attachment
            or ctx.screen_observation
            or ctx.request.interaction_mode == "system"
        )
        else deps.tools.schemas_for_user_text(ctx.user_input)
    )
    use_tools = bool(active_tool_schemas)
    ctx.metadata["use_tools"] = use_tools
    ctx.metadata["available_tool_schema_count"] = deps.available_tool_schema_count
    ctx.metadata["selected_tool_schema_count"] = len(active_tool_schemas)
    obs.mark("agent_tool_local_ms", 0.0)
    obs.mark("agent_followup_response_ms", 0.0)
    obs.mark("agent_function_calls", 0)
    obs.mark("agent_rounds", 0)
    obs.mark("agent_model", model)
    obs.mark("prompt_input_chars", len(str(prompt_input or "")))

    obs.event("tool_schema_gate", 0.0, use_tools=use_tools, user_chars=len(ctx.user_input))
    # Supply-chain diagnostic: the EXACT tool names this turn's request will
    # carry. DEBUG (sync-chain twin of the tool_round line; no production callers).
    logger.debug(
        "turn tools offered: %s",
        [s.get("name") or (s.get("function") or {}).get("name") for s in active_tool_schemas] or "[]",
    )

    prompt_for_round = prompt_input or ""
    tool_history: list[dict[str, Any]] = []
    response = None

    answer = StreamedAnswer()
    ctx.answer = answer

    adapter = deps.llm
    if not check_context_budget(ctx, deps, prompt_for_round, active_tool_schemas):
        return ctx
    if adapter.prefers_chat_completions():
        logger.debug("llm path: chat_completions (tools this turn: %s)", use_tools)
        if use_tools and active_tool_schemas:
            # Chat Completions tool loop -- mirrors the Responses loop below
            # (probe with tools each round; no calls -> the probe text IS the
            # answer). Same fix vintage as the streaming branch (FINDINGS #18).
            probe_text = ""
            for round_index in range(max_rounds):
                if not check_context_budget(ctx, deps, prompt_for_round, active_tool_schemas):
                    return ctx
                obs.mark("agent_rounds", round_index + 1)
                response_start_ms = now_ms()
                tool_calls, probe_text = adapter.create_chat_with_tools(
                    model=model,
                    prompt=prompt_for_round,
                    tools=active_tool_schemas,
                    state=ctx,
                )
                response_duration = elapsed_ms(response_start_ms)
                if round_index == 0:
                    obs.mark("agent_response_initial_ms", response_duration)
                    phase = "initial"
                else:
                    obs.bump("agent_followup_response_ms", response_duration)
                    phase = "followup"
                obs.event(
                    "agent_chat_completion",
                    response_duration,
                    phase=phase,
                    model=model,
                    use_tools=True,
                    round=round_index + 1,
                )
                if not tool_calls:
                    answer.raw_model_output = probe_text
                    obs.mark("raw_answer_chars", len(answer.raw_model_output or ""))
                    return ctx
                for call in tool_calls:
                    if ctx.request.interaction_mode == "system" or is_turn_cancelled(ctx.request):
                        return ctx
                    if not check_context_sources(ctx):
                        return ctx
                    obs.bump("agent_function_calls", 1)
                    tool_start_ms = now_ms()
                    tool_result = deps.tools.run(call["name"], call["arguments"])
                    from uuid import uuid4
                    from spica.runtime.memory_commit import record_turn_result
                    call_id = call.get("id") or "local_" + uuid4().hex
                    record_turn_result(ctx, deps, kind="tool", content=tool_result, event_suffix="tool:" + call_id,
                                       source=call["name"], metadata={"model_call_id": call_id,
                                       "arguments": call["arguments"], "batch": round_index + 1})
                    record_screen_tool_result(ctx, obs, call["name"], tool_result)
                    tool_duration = elapsed_ms(tool_start_ms)
                    obs.bump("agent_tool_local_ms", tool_duration)
                    tool_history.append(
                        {
                            **call,
                            "id": call_id,
                            "batch": round_index + 1,
                            "arguments": call["arguments"],
                            "output": tool_result,
                        }
                    )
                    obs.event(
                        "agent_tool_local",
                        tool_duration,
                        name=call["name"],
                        arguments_chars=len(call["arguments"]),
                        output_chars=len(tool_result),
                    )
                prompt_for_round = _build_tool_followup_prompt(prompt_input, tool_history,
                    cap_output=cap_output, tool_schemas=active_tool_schemas)
            # FROZEN divergence (P1): the sync chain keeps the historical error on
            # overflow (golden-pinned); the streaming chain forces a graceful
            # final answer instead (tool_round._run_chain_rounds).
            ctx.error = TurnError("LLM_TOOL_LOOP_EXCEEDED", "工具调用轮数超过限制。")
            answer.raw_model_output = probe_text
            return ctx
        obs.mark("agent_rounds", 1)
        response_start_ms = now_ms()
        answer.raw_model_output = adapter.complete_chat(model, prompt_for_round, ctx)
        response_duration = elapsed_ms(response_start_ms)
        obs.mark("agent_response_initial_ms", response_duration)
        obs.mark("raw_answer_chars", len(answer.raw_model_output or ""))
        obs.event(
            "agent_chat_completion",
            response_duration,
            phase="initial",
            model=model,
            use_tools=False,
        )
        return ctx

    for round_index in range(max_rounds):
        if not check_context_budget(ctx, deps, prompt_for_round, active_tool_schemas):
            return ctx
        obs.mark("agent_rounds", round_index + 1)
        request = {
            "model": model,
            "input": prompt_for_round,
        }
        if use_tools and active_tool_schemas:
            request["tools"] = active_tool_schemas

        response_start_ms = now_ms()
        response = adapter.create_responses(state=ctx, **request)
        response_duration = elapsed_ms(response_start_ms)
        if round_index == 0:
            obs.mark("agent_response_initial_ms", response_duration)
            phase = "initial"
        else:
            obs.bump("agent_followup_response_ms", response_duration)
            phase = "followup"

        _record_usage(obs, response)
        obs.event(
            "agent_response",
            response_duration,
            phase=phase,
            model=model,
            use_tools=use_tools,
            round=round_index + 1,
        )

        function_calls = [
            item for item in list(_get_attr(response, "output", []) or [])
            if _get_attr(item, "type") == "function_call"
        ]
        if not function_calls:
            answer.raw_model_output = str(_get_attr(response, "output_text", "") or "")
            ctx.response_id = str(_get_attr(response, "id", "") or "") or None
            obs.mark("raw_answer_chars", len(answer.raw_model_output or ""))
            return ctx

        for item in function_calls:
            if ctx.request.interaction_mode == "system" or is_turn_cancelled(ctx.request):
                return ctx
            if not check_context_sources(ctx):
                return ctx
            obs.bump("agent_function_calls", 1)
            tool_start_ms = now_ms()
            tool_name = str(_get_attr(item, "name", ""))
            arguments = str(_get_attr(item, "arguments", "") or "{}")
            tool_result = deps.tools.run(tool_name, arguments)
            from uuid import uuid4
            from spica.runtime.memory_commit import record_turn_result
            call_id = str(_get_attr(item, "call_id", "") or "local_" + uuid4().hex)
            record_turn_result(ctx, deps, kind="tool", content=tool_result, event_suffix="tool:" + call_id,
                               source=tool_name, metadata={"model_call_id": call_id,
                               "arguments": arguments, "batch": round_index + 1})
            record_screen_tool_result(ctx, obs, tool_name, tool_result)
            tool_duration = elapsed_ms(tool_start_ms)
            obs.bump("agent_tool_local_ms", tool_duration)
            tool_history.append(
                {
                    "name": tool_name,
                    "id": call_id,
                    "batch": round_index + 1,
                    "response_items": adapter.response_items(response),
                    "assistant_text": str(_get_attr(response, "output_text", "") or ""),
                    "arguments": arguments,
                    "output": tool_result,
                }
            )
            obs.event(
                "agent_tool_local",
                tool_duration,
                name=tool_name,
                arguments_chars=len(arguments),
                output_chars=len(tool_result),
            )

        prompt_for_round = _build_tool_followup_prompt(prompt_input, tool_history,
                    cap_output=cap_output, tool_schemas=active_tool_schemas)

    # FROZEN divergence (P1): see the chat branch note above -- error here,
    # graceful forced final on the streaming chain.
    ctx.error = TurnError("LLM_TOOL_LOOP_EXCEEDED", "工具调用轮数超过限制。")
    if response is not None:
        answer.raw_model_output = str(_get_attr(response, "output_text", "") or "")
        ctx.response_id = str(_get_attr(response, "id", "") or "") or None
    return ctx


@node_timer
def parse_reply_node(ctx: TurnContext, services: AgentServices, deps: Any = None) -> TurnContext:
    if _skip_if_error(ctx):
        return ctx
    answer = ctx.answer if ctx.answer is not None else StreamedAnswer()
    ctx.answer = answer
    answer.parsed_reply = parse_model_reply(answer.raw_model_output or "")
    answer.answer = normalize_square_brackets_for_speech(answer.parsed_reply["answer"])
    answer.parsed_reply["answer"] = answer.answer
    answer.emotion = normalize_emotion(ctx.request.emotion_override or answer.parsed_reply["emotion"])
    return ctx


# save_recent_context_node + extract_memory_node were unified with the streaming
# path into spica/runtime/memory_commit.save_stream_memory (Phase 6D).


@node_timer
def build_visual_node(ctx: TurnContext, services: AgentServices, deps: Any = None) -> TurnContext:
    if _skip_if_error(ctx):
        return ctx
    deps = deps or TurnDeps.from_legacy_services(services)
    if deps.visual is None:
        return ctx
    obs = deps.observer
    answer = ctx.answer if ctx.answer is not None else StreamedAnswer()
    ctx.answer = answer
    try:
        visual = deps.visual.build_visual_payload(
            answer=answer.answer or "",
            emotion=answer.emotion or "happy",
            requested_costume=ctx.request.visual_overrides.get("costume_set"),
            requested_mode=ctx.request.visual_overrides.get("costume_mode"),
        )
        answer.visual = visual
        classifier_meta = visual.get("classifier") if isinstance(visual.get("classifier"), dict) else {}
        if isinstance(classifier_meta.get("duration_ms"), (int, float)):
            obs.mark("visual_classifier_ms", classifier_meta["duration_ms"])
        if isinstance(classifier_meta.get("segments"), int):
            obs.mark("visual_segments", classifier_meta["segments"])
        ctx.tools.append(
            {
                "name": "spica_visual_diff",
                "required": False,
                "ok": True,
                "costume": visual.get("costume"),
                "classifier_version": visual.get("classifier_version"),
                "selection_source": visual.get("selection_source"),
                "selection_error": visual.get("selection_error"),
            }
        )
    except Exception as exc:
        ctx.tools.append(
            {
                "name": "spica_visual_diff",
                "required": False,
                "ok": False,
                "error": str(exc),
            }
        )
    return ctx


@node_timer
def synthesize_tts_node(ctx: TurnContext, services: AgentServices, deps: Any = None) -> TurnContext:
    if _skip_if_error(ctx):
        return ctx
    deps = deps or TurnDeps.from_legacy_services(services)
    provider = _tts_adapter_name(deps)
    obs = deps.observer
    answer = ctx.answer if ctx.answer is not None else StreamedAnswer()
    ctx.answer = answer
    if deps.tts is None:
        ctx.tools.append(
            {
                "name": provider,
                "required": True,
                "ok": False,
                "error": "TTS adapter is not configured.",
            }
        )
        ctx.error = TurnError("TTS_TOOL_NOT_CONFIGURED", "TTS adapter 未初始化。")
        return ctx
    try:
        result = deps.tts.synthesize(
            _build_tts_request(
                ctx,
                text=answer.answer or "",
                emotion=answer.emotion or "happy",
            )
        )
        answer.tts_result = result
        for key, value in (result.timing or {}).items():
            obs.mark(key, value)
        if not result.ok:
            ctx.tools.append(
                {
                    "name": result.provider,
                    "required": True,
                    "ok": False,
                    "error": result.error,
                }
            )
            ctx.error = TurnError("TTS_FAILED", result.error or "TTS synthesis failed.")
            return ctx
        ctx.tools.append(
            {
                "name": result.provider,
                "required": True,
                "ok": True,
                "audio_url": result.audio_url,
            }
        )
    except Exception as exc:
        ctx.tools.append(
            {
                "name": provider,
                "required": True,
                "ok": False,
                "error": str(exc),
            }
        )
        ctx.error = TurnError("TTS_FAILED", str(exc))
    return ctx


@node_timer
def build_response_node(ctx: TurnContext, services: AgentServices, deps: Any = None) -> TurnContext:
    answer = ctx.answer
    emotion = normalize_emotion((answer.emotion if answer else None) or "surprised")
    emotion_reason = "用户输入为空。" if ctx.error and ctx.error.code == "EMPTY_MESSAGE" else "模型按回复语气选择。"
    parsed_reply = answer.parsed_reply if answer else None
    if parsed_reply:
        emotion_reason = parsed_reply.get("emotion_reason") or emotion_reason

    # Empty/error turns leave ctx.answer None; the fallback strings here are
    # byte-identical to what validate used to pre-write (C3c guardrail 3).
    answer_text = (answer.answer if answer else None) or "メッセージを入力してください。"
    tts_result = answer.tts_result if answer else None

    payload = {
        "answer": answer_text,
        "conversation_id": ctx.request.conversation_id,
        "emotion": {
            "name": emotion,
            "label": EMOTION_LABELS[emotion],
            "reason": emotion_reason,
        },
        "audio_url": None,
        "audio_path": None,
        "tts_params": None,
        "visual": answer.visual if answer else None,
        "tools": ctx.tools,
        # ctx.timing is the observer's sink -- the accumulated turn timing.
        "timing": ctx.timing,
    }

    if tts_result:
        payload["audio_url"] = tts_result.audio_url
        payload["audio_path"] = tts_result.audio_path
        payload["tts_chunks"] = _legacy_tts_chunks(tts_result)
        payload["tts_chunk_audio"] = _legacy_tts_chunk_audio(tts_result)
        if tts_result.error:
            payload["tts_error"] = tts_result.error

    if ctx.error:
        # One of the two TurnError serialization boundaries (C3c guardrail 2).
        payload["error"] = turn_error_to_legacy_dict(ctx.error)

    ctx.response_payload = payload
    return ctx


def _build_tool_followup_prompt(prompt_input: Any, tool_history: list[dict[str, Any]], *, cap_output=True, tool_schemas=()) -> Any:
    if isinstance(prompt_input, list) or tool_schemas:
        from spica.runtime.tool_round import build_tool_followup_prompt
        return build_tool_followup_prompt(prompt_input, tool_history, cap_output=cap_output, tool_schemas=tool_schemas)
    return "\n\n".join(
        [
            str(prompt_input),
            "[TOOL_RESULTS]",
            json.dumps(_compact_tool_history_for_prompt(tool_history), ensure_ascii=False),
            "[NEXT_STEP]",
            "请只根据以上工具结果输出最终 JSON，不要 Markdown，不要解释工具链。",
        ]
    )


# Tools whose result is a screen_observation.v1 the turn should LIFT into ctx
# (-> next-turn "刚看过屏" context via memory_commit). Phase 9 adds the companion
# watch tool to the ROSTER only -- the record logic below is byte-identical for
# inspect_screen.
_SCREEN_OBSERVATION_TOOLS = frozenset({"inspect_screen", "watch_game_screen"})


def record_screen_tool_result(ctx: TurnContext, observer: Any, tool_name: str, tool_result: str) -> None:
    if tool_name not in _SCREEN_OBSERVATION_TOOLS:
        return
    try:
        parsed = json.loads(tool_result or "{}")
    except json.JSONDecodeError:
        return
    if not isinstance(parsed, dict) or not parsed.get("ok"):
        return
    data = parsed.get("data")
    if not isinstance(data, dict) or data.get("schema_version") != "screen_observation.v1":
        return
    ctx.screen_observation = data
    ctx.metadata["screen_observation_used"] = True
    ctx.metadata["screen_observation_schema"] = data.get("schema_version")
    ctx.metadata["screen_observation_target"] = (data.get("request") or {}).get("target")
    ctx.metadata["screen_observation_source"] = (data.get("capture") or {}).get("source")
    _record_screen_analysis_metadata(ctx, observer)


def _record_screen_analysis_metadata(ctx: TurnContext, observer: Any) -> None:
    metadata = get_last_screen_analysis_metadata()
    for key in ("screen_analysis_stream_enabled", "screen_analysis_stream_fallback_used"):
        if key in metadata:
            observer.mark(key, metadata[key])
            ctx.metadata[key] = metadata[key]
    if metadata.get("screen_analysis_first_delta_ms") is not None:
        observer.mark("screen_analysis_first_delta_ms", metadata["screen_analysis_first_delta_ms"])
    for key in ("screen_analysis_engine", "screen_analysis_model", "screen_analysis_revision", "screen_analysis_local"):
        if key in metadata:
            ctx.metadata[key] = metadata[key]
    for key in ("screen_analysis_moondream_ms", "screen_analysis_total_ms"):
        if key in metadata:
            observer.mark(key, metadata[key])


# P1 (F4): hard backstop for tool outputs entering a followup prompt. Chosen well
# above today's real outputs (a watch observation is ~1-2KB) so the existing tools
# NEVER hit it -- the target customer is a future chainable tool dumping a page.
_TOOL_OUTPUT_PROMPT_CAP = 8000


def _cap_tool_output(output: str) -> str:
    if len(output) <= _TOOL_OUTPUT_PROMPT_CAP:
        return output
    keep = _TOOL_OUTPUT_PROMPT_CAP // 2
    omitted = len(output) - 2 * keep
    return output[:keep] + f"...[truncated {omitted} chars]..." + output[-keep:]


def _compact_tool_history_for_prompt(
    tool_history: list[dict[str, Any]],
    compact_lookup: Any = None,
    *, cap_output: bool = True,
) -> list[dict[str, Any]]:
    """Two layers (P1, F4): a tool-declared compactor (``compact_lookup`` resolves
    the registry's ``compact_output``; the streaming chain passes it), then the
    global hard cap. The FROZEN sync chain passes no lookup and keeps the
    historical inspect_screen special case -- inspect registers the SAME function
    via the registry, so both paths compact identically byte for byte."""
    compact_history: list[dict[str, Any]] = []
    for item in tool_history:
        compact_item = dict(item)
        name = compact_item.get("name")
        output = str(compact_item.get("output") or "")
        compactor = compact_lookup(name) if callable(compact_lookup) else None
        if compactor is not None:
            output = compactor(output)
        elif name == "inspect_screen":
            output = _compact_screen_tool_output(output)
        compact_item["output"] = _cap_tool_output(output) if cap_output else output
        compact_history.append(compact_item)
    return compact_history


def _compact_screen_tool_output(output: str) -> str:
    try:
        parsed = json.loads(output or "{}")
    except json.JSONDecodeError:
        return output
    if not isinstance(parsed, dict) or not parsed.get("ok") or not isinstance(parsed.get("data"), dict):
        return output
    parsed = dict(parsed)
    parsed["data"] = compact_screen_observation_for_prompt(parsed["data"])
    return json.dumps(parsed, ensure_ascii=False)


def _inject_screen_observation(
    prompt_input: Any,
    observation: dict[str, Any],
    *,
    dialog_display_language: str = "ja",
) -> str:
    safe_observation = compact_screen_observation_for_prompt(observation)
    return append_prompt_context_sections(
        prompt_input,
        [
            "[SCREEN_OBSERVATION]",
            json.dumps(safe_observation, ensure_ascii=False),
            "[SCREEN_OBSERVATION_INSTRUCTIONS]",
            (
                "这张截图已经由本地 screen analyzer 分析完成。请只根据 screen_observation.v1 的内容回答，"
                "不要要求再次截图，不要声称可以实时观察，不要提及内部工具链。"
                "如果 observation 表示不确定、低置信度或有 ambiguity，请明确说明不确定，不要编造确定答案。"
                "如果是任务栏、标签页或数量统计类问题，请说明这是基于截图的估计，并带上限制。"
            ),
        ],
        dialog_display_language=dialog_display_language,
    )


def _record_usage(observer: Any, response: Any) -> None:
    usage = _get_attr(response, "usage")
    if not usage:
        return
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = _get_attr(usage, key)
        if value is not None:
            observer.mark(key, value)


_DEEPSEEK_BRANCH_MOVED = "spica.runtime.stages DeepSeek/OpenAI branch moved to spica.adapters.llm (Phase 5)"
