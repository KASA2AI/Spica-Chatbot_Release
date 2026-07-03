"""Turn stages (C4: moved from agent/nodes.py; C5: timing via TurnObserver).

Each stage is ``(ctx, services, deps) -> ctx``: it reads/writes its own TurnContext
sub-object (C3c) and never emits events. Tunables + the LLM/memory ports come from
the typed ``deps`` (config/llm/memory); ``services`` is a *transitional* dependency
carrier for the bits not yet on deps -- recent_memory and the tool schema/function
registry (TODO C6/C7).

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
from functools import wraps
from typing import Any, Callable

from spica.conversation.character_loader import DEFAULT_CHARACTER_NAME, DEFAULT_INTERLOCUTOR_NAME
from spica.conversation.prompt_builder import DEFAULT_CHARACTER_PROFILE, build_spica_prompt
from spica.conversation.reply_parser import EMOTION_LABELS, normalize_emotion, parse_model_reply
from spica.conversation.text_normalizer import normalize_square_brackets_for_speech
from spica.conversation.time_context import build_local_time_context
# OO migration Phase 1: the galgame DISPLAY half (section formatting) lives in the
# domain package; the gate + node stay here. First runtime -> galgame import edge;
# acyclic because spica/galgame/__init__ re-exports pure models only and
# prompt_sections imports nothing from spica.runtime (test_layering guards it).
from spica.galgame.prompt_sections import _build_game_context_sections
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
from spica.ports.memory import MemoryScope
from spica.runtime.context import (
    PromptBundle,
    RetrievedContext,
    StreamedAnswer,
    TurnContext,
    TurnError,
    turn_error_to_legacy_dict,
)
from spica.runtime.deps import TurnDeps
from spica.runtime.observer import NoopTurnObserver


logger = logging.getLogger(__name__)

DEFAULT_SCREEN_ATTACHMENT_QUESTION = "请查看这张截图并概括内容。"

# -- galgame gated context injection (Phase 3) -------------------------------
_GALGAME_CONVERSATION_PREFIX = "galgame::"
_OFFLINE_COMMAND_INTENTS = frozenset(
    {"ask_last_progress", "ask_game_progress", "ask_character_relation"}
)


# Stages take (ctx, services, deps). deps may be None for direct (dict-config)
# callers (the compat sync chain / tests); stages that need config or a port
# bridge it here -- the same N3-clean pattern memory_commit uses. node_timer
# threads deps through and times the node via the injected observer (C5).
def node_timer(func: Callable[..., TurnContext]):
    @wraps(func)
    def wrapper(ctx: TurnContext, services: AgentServices, deps: Any = None) -> TurnContext:
        observer = deps.observer if deps is not None else NoopTurnObserver()
        with observer.span(func.__name__, conversation_id=ctx.request.conversation_id):
            try:
                return func(ctx, services, deps)
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


def _tts_adapter_name(services: AgentServices) -> str:
    return str(getattr(services.tts_adapter, "name", None) or "tts")


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
    return ctx


@node_timer
def load_recent_context_node(ctx: TurnContext, services: AgentServices, deps: Any = None) -> TurnContext:
    if _skip_if_error(ctx):
        return ctx
    deps = deps or TurnDeps.from_legacy_services(services)
    # TODO(C6): the recent-turn buffer becomes a deps-resolved capability;
    # services.recent_memory is a transitional carrier (N3 allows it).
    recent = services.recent_memory.get_recent(
        ctx.request.conversation_id,
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
    scope = MemoryScope(
        character_id=str(deps.config.character.character_id or "spica"),
        user_id=str(deps.config.character.interlocutor_name or DEFAULT_INTERLOCUTOR_NAME),
        conversation_id=ctx.request.effective_memory_conversation_id,
    )
    items = deps.memory.retrieve(
        scope,
        ctx.user_input,
        limit=deps.config.memory.long_term_memory_limit,
    )
    # build_spica_prompt / _format_memories consume dicts (scope / content /
    # memory_type); map MemoryItem back so the prompt's scope label survives.
    memories = [
        {
            "scope": item.scope,
            "content": item.text,
            "memory_type": item.type,
            "importance": item.importance,
            "score": item.score,
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


@node_timer
def build_prompt_node(ctx: TurnContext, services: AgentServices, deps: Any = None) -> TurnContext:
    if _skip_if_error(ctx):
        return ctx
    deps = deps or TurnDeps.from_legacy_services(services)
    recent_context = ctx.recent.recent_context if ctx.recent else []
    long_term_memories = ctx.recent.long_term_memories if ctx.recent else []
    prompt_input = build_spica_prompt(
        user_input=ctx.user_input,
        recent_context=recent_context,
        long_term_memories=long_term_memories,
        character_profile=str(deps.config.character.character_profile or DEFAULT_CHARACTER_PROFILE),
        memory_limit=deps.config.memory.long_term_memory_limit,
        memory_budget_chars=deps.config.memory.long_term_memory_budget_chars,
        recent_turn_char_limit=deps.config.memory.recent_turn_char_limit,
        interlocutor_name=str(deps.config.character.interlocutor_name or DEFAULT_INTERLOCUTOR_NAME),
        character_name=str(deps.config.character.character_name or DEFAULT_CHARACTER_NAME),
        user_local_time=ctx.user_local_time if ctx.request.include_user_time_context else None,
    )
    if ctx.screen_observation:
        prompt_input = _inject_screen_observation(prompt_input, ctx.screen_observation)
    ctx.prompt = PromptBundle(prompt_input=prompt_input)
    ctx.metadata["prompt_input_chars"] = len(str(prompt_input))
    return ctx


# B3: gated stage inserted AFTER build_prompt, BEFORE the LLM call. The gate is
# pure request-field logic (NEVER an LLM call -- CLAUDE.md #1.3). The `none` branch
# is a byte-level no-op (it never opens an observer span, so it does not even touch
# ctx.timing) -- that is what keeps a plain chat turn's prompt + ctx identical.
# Injection mirrors _inject_screen_observation: append sections to the already-built
# prompt string; build_prompt_node / prompt_builder are untouched.


def _game_context_mode(request: Any) -> str:
    gcr = getattr(request, "game_context_request", None)
    conversation_id = getattr(request, "conversation_id", "") or ""
    if (
        getattr(request, "interaction_mode", "chat") == "galgame"
        or conversation_id.startswith(_GALGAME_CONVERSATION_PREFIX)
        or (gcr is not None and getattr(gcr, "mode", None) == "active")
    ):
        return "active"
    if getattr(request, "command_intent", None) in _OFFLINE_COMMAND_INTENTS or (
        gcr is not None and getattr(gcr, "mode", None) == "offline"
    ):
        return "offline"
    return "none"


def _parse_game_id_from_conversation(conversation_id: str) -> str | None:
    if not conversation_id.startswith(_GALGAME_CONVERSATION_PREFIX):
        return None
    parts = conversation_id.split("::")
    return parts[1] if len(parts) >= 2 and parts[1] else None


def _parse_playthrough_from_conversation(conversation_id: str) -> str | None:
    parts = conversation_id.split("::")
    if len(parts) >= 4 and parts[2] == "playthrough" and parts[3]:
        return parts[3]
    return None


def _resolve_game_target(
    request: Any, game_memory: Any, mode: str
) -> tuple[str | None, str, str | None]:
    gcr = getattr(request, "game_context_request", None)
    conversation_id = getattr(request, "conversation_id", "") or ""
    game_id = getattr(gcr, "game_id", None) if gcr is not None else None
    if not game_id:
        game_id = _parse_game_id_from_conversation(conversation_id)
    playthrough_id = getattr(gcr, "playthrough_id", None) if gcr is not None else None
    if not playthrough_id:
        playthrough_id = _parse_playthrough_from_conversation(conversation_id) or "default"
    # B1: the live session id rides on the typed gate request (the companion
    # controller stamps it into the published binding). Absent on the manual/debug
    # conversation-id path -> CURRENT_LINE is simply not read for that turn.
    session_id = getattr(gcr, "session_id", None) if gcr is not None else None
    if not game_id and mode == "offline":
        last = game_memory.last_played_game()
        if last is not None:
            game_id = last.game_id
            playthrough_id = last.active_playthrough_id or "default"
    return game_id, playthrough_id, session_id


def retrieve_game_context_node(ctx: TurnContext, services: AgentServices, deps: Any = None) -> TurnContext:
    """Gated galgame context injection (B3).

    NOT decorated with @node_timer on purpose: the ``none`` branch must be a
    byte-level no-op, and opening an observer span would write ``ctx.timing`` --
    so the gate is checked BEFORE any span. The gate is pure request-field logic;
    it never runs an LLM (CLAUDE.md #1.3). Injection is best-effort: a game_memory
    read failure logs a warning and injects nothing, never failing the turn.
    """
    mode = _game_context_mode(ctx.request)
    if mode == "none" or ctx.error is not None:
        # Byte-level no-op: no span opened, no ctx mutation, prompt untouched.
        return ctx
    deps = deps or TurnDeps.from_legacy_services(services)
    observer = deps.observer if deps is not None else NoopTurnObserver()
    with observer.span("retrieve_game_context_node", conversation_id=ctx.request.conversation_id):
        game_memory = getattr(deps, "game_memory", None)
        if game_memory is None or ctx.prompt is None:
            return ctx
        try:
            game_id, playthrough_id, session_id = _resolve_game_target(ctx.request, game_memory, mode)
            if not game_id:
                return ctx
            sections = _build_game_context_sections(
                mode, game_memory, ctx.request, deps, game_id, playthrough_id, session_id
            )
        except Exception as exc:  # noqa: BLE001 -- best-effort: never fail the turn
            # Hard rule (Phase 3): do NOT silently swallow. Surface the failure on
            # the logging layer so a broken game_memory is diagnosable, not just
            # "Spica can't answer progress" with no trace.
            logger.warning(
                "retrieve_game_context_node: game context read failed "
                "(mode=%s, conversation_id=%s): %s",
                mode,
                ctx.request.conversation_id,
                exc,
                exc_info=True,
            )
            return ctx
        if sections:
            base = str(ctx.prompt.prompt_input or "")
            ctx.prompt = PromptBundle(prompt_input="\n\n".join([base] + sections))
    return ctx


@node_timer
def call_llm_node(ctx: TurnContext, services: AgentServices, deps: Any = None) -> TurnContext:
    if _skip_if_error(ctx):
        return ctx
    if services.llm_client is None:
        ctx.error = TurnError("LLM_CLIENT_NOT_CONFIGURED", "LLM client 未配置。")
        return ctx
    deps = deps or TurnDeps.from_legacy_services(services)
    obs = deps.observer

    model = deps.config.llm.model
    max_rounds = max(1, int(deps.config.max_tool_rounds))
    prompt_input = ctx.prompt.prompt_input if ctx.prompt else None
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
    ctx.metadata["available_tool_schema_count"] = len(services.tool_schemas)
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

    prompt_for_round = str(prompt_input or "")
    tool_history: list[dict[str, Any]] = []
    response = None

    answer = StreamedAnswer()
    ctx.answer = answer

    adapter = deps.llm
    if adapter.prefers_chat_completions():
        logger.debug("llm path: chat_completions (tools this turn: %s)", use_tools)
        if use_tools and active_tool_schemas:
            # Chat Completions tool loop -- mirrors the Responses loop below
            # (probe with tools each round; no calls -> the probe text IS the
            # answer). Same fix vintage as the streaming branch (FINDINGS #18).
            probe_text = ""
            for round_index in range(max_rounds):
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
                    obs.bump("agent_function_calls", 1)
                    tool_start_ms = now_ms()
                    tool_result = deps.tools.run(call["name"], call["arguments"])
                    record_screen_tool_result(ctx, obs, call["name"], tool_result)
                    tool_duration = elapsed_ms(tool_start_ms)
                    obs.bump("agent_tool_local_ms", tool_duration)
                    tool_history.append(
                        {
                            "name": call["name"],
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
                prompt_for_round = _build_tool_followup_prompt(prompt_input, tool_history)
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
        obs.mark("agent_rounds", round_index + 1)
        request = {
            "model": model,
            "input": prompt_for_round,
        }
        if use_tools and active_tool_schemas:
            request["tools"] = active_tool_schemas

        response_start_ms = now_ms()
        response = adapter.create_responses(**request)
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
            obs.bump("agent_function_calls", 1)
            tool_start_ms = now_ms()
            tool_name = str(_get_attr(item, "name", ""))
            arguments = str(_get_attr(item, "arguments", "") or "{}")
            tool_result = deps.tools.run(tool_name, arguments)
            record_screen_tool_result(ctx, obs, tool_name, tool_result)
            tool_duration = elapsed_ms(tool_start_ms)
            obs.bump("agent_tool_local_ms", tool_duration)
            tool_history.append(
                {
                    "name": tool_name,
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

        prompt_for_round = _build_tool_followup_prompt(prompt_input, tool_history)

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
    # TODO(C7): visual is a resolved port (deps.visual); services.visual_tool transitional.
    if services.visual_tool is None:
        return ctx
    deps = deps or TurnDeps.from_legacy_services(services)
    obs = deps.observer
    answer = ctx.answer if ctx.answer is not None else StreamedAnswer()
    ctx.answer = answer
    try:
        visual = services.visual_tool.build_visual_payload(
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
    # TODO(C7): TTS is a resolved port (deps.tts); services.tts_adapter transitional.
    provider = _tts_adapter_name(services)
    deps = deps or TurnDeps.from_legacy_services(services)
    obs = deps.observer
    answer = ctx.answer if ctx.answer is not None else StreamedAnswer()
    ctx.answer = answer
    if services.tts_adapter is None:
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
        result = services.tts_adapter.synthesize(
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


def _build_tool_followup_prompt(prompt_input: Any, tool_history: list[dict[str, Any]]) -> str:
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
        compact_item["output"] = _cap_tool_output(output)
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


def _inject_screen_observation(prompt_input: Any, observation: dict[str, Any]) -> str:
    safe_observation = compact_screen_observation_for_prompt(observation)
    return "\n\n".join(
        [
            str(prompt_input),
            "[SCREEN_OBSERVATION]",
            json.dumps(safe_observation, ensure_ascii=False),
            "[SCREEN_OBSERVATION_INSTRUCTIONS]",
            (
                "这张截图已经由本地 screen analyzer 分析完成。请只根据 screen_observation.v1 的内容回答，"
                "不要要求再次截图，不要声称可以实时观察，不要提及内部工具链。"
                "如果 observation 表示不确定、低置信度或有 ambiguity，请明确说明不确定，不要编造确定答案。"
                "如果是任务栏、标签页或数量统计类问题，请说明这是基于截图的估计，并带上限制。"
            ),
        ]
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
