from __future__ import annotations

import json
from functools import wraps
from typing import Any, Callable

from agent.character_loader import DEFAULT_CHARACTER_NAME, DEFAULT_INTERLOCUTOR_NAME
from agent.prompt_builder import DEFAULT_CHARACTER_PROFILE, build_spica_prompt
from agent.reply_parser import EMOTION_LABELS, normalize_emotion, parse_model_reply
from agent.state import AgentServices
from agent.text_normalizer import normalize_square_brackets_for_speech
from agent.time_context import build_local_time_context
from common.timing import elapsed_ms, log_timing, now_ms
from agent_tools.function_tools import run_local_tool, tool_schemas_for_user_text
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
from spica.adapters.llm import OpenAICompatibleAdapter
from spica.adapters.memory import SqliteMemoryAdapter
from spica.ports.memory import MemoryScope
from spica.runtime.context import (
    PromptBundle,
    RetrievedContext,
    StreamedAnswer,
    TurnContext,
    TurnError,
    turn_error_to_legacy_dict,
)


DEFAULT_SCREEN_ATTACHMENT_QUESTION = "请查看这张截图并概括内容。"


# C3c: nodes run on TurnContext (typed per-stage sub-objects) instead of the
# AgentState blackboard. They still take ``services`` as the dependency/config
# carrier -- transitional debt: C4 flips services -> deps when agent/ moves to
# spica/runtime/stages and the residual ``services.config.get`` reads migrate to
# AppConfig. C3c adds no new ``services.config.get`` and no new client/adapter
# fallback; it only re-routes blackboard fields onto ctx sub-objects.
def node_timer(func: Callable[[TurnContext, AgentServices], TurnContext]):
    @wraps(func)
    def wrapper(ctx: TurnContext, services: AgentServices) -> TurnContext:
        start_ms = now_ms()
        try:
            return func(ctx, services)
        except Exception as exc:
            if ctx.error is None:
                ctx.error = TurnError("NODE_FAILED", f"{func.__name__}: {exc}")
            return ctx
        finally:
            duration = elapsed_ms(start_ms)
            ctx.timing[f"{func.__name__}_ms"] = duration
            _log_timing(services, func.__name__, duration, conversation_id=ctx.request.conversation_id)

    return wrapper


def _log_timing(services: AgentServices, step: str, duration_ms: float, **fields: Any) -> None:
    logger = services.logger or log_timing
    logger(step, duration_ms, **fields)


def _skip_if_error(ctx: TurnContext) -> bool:
    return ctx.error is not None


def _get_attr(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _llm_adapter(services: AgentServices) -> OpenAICompatibleAdapter:
    return services.llm_adapter or OpenAICompatibleAdapter(services.llm_client)


def _memory_adapter(services: AgentServices) -> Any:
    # Legacy node-path resolver (C3b moved it off spica/runtime/memory_commit so
    # that hot path runs on deps.memory). Retired with agent/ in C4.
    return services.memory_adapter or SqliteMemoryAdapter(services.memory_store, services.recent_memory)


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
def validate_input_node(ctx: TurnContext, services: AgentServices) -> TurnContext:
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
def load_recent_context_node(ctx: TurnContext, services: AgentServices) -> TurnContext:
    if _skip_if_error(ctx):
        return ctx
    recent = services.recent_memory.get_recent(
        ctx.request.conversation_id,
        limit=int(services.config.get("recent_context_limit", 3)),
    )
    if ctx.recent is None:
        ctx.recent = RetrievedContext()
    ctx.recent.recent_context = recent
    ctx.metadata["recent_context_count"] = len(recent)
    return ctx


@node_timer
def retrieve_long_term_memory_node(ctx: TurnContext, services: AgentServices) -> TurnContext:
    if _skip_if_error(ctx):
        return ctx
    # Read through MemoryPort.retrieve so the read key matches commit_turn's
    # character-namespaced write key (Phase 5/7). A bare conversation_id here
    # silently misses every auto-extracted memory. Reuse the same adapter
    # resolution as the write path -- no second fallback.
    scope = MemoryScope(
        character_id=str(services.config.get("character_id") or "spica"),
        user_id=str(services.config.get("interlocutor_name") or DEFAULT_INTERLOCUTOR_NAME),
        conversation_id=ctx.request.conversation_id,
    )
    items = _memory_adapter(services).retrieve(
        scope,
        ctx.user_input,
        limit=int(services.config.get("long_term_memory_limit", 5)),
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
def analyze_screen_attachment_node(ctx: TurnContext, services: AgentServices) -> TurnContext:
    if _skip_if_error(ctx):
        return ctx
    if not ctx.request.screen_attachment:
        return ctx

    started_ms = now_ms()
    clear_last_screen_analysis_metadata()
    try:
        observation = analyze_screen_attachment(
            attachment=ctx.request.screen_attachment,
            user_question=ctx.user_input,
        )
        ctx.screen_observation = observation
        duration = elapsed_ms(started_ms)
        ctx.timing["screen_analysis_ms"] = duration
        ctx.metadata["screen_observation_used"] = True
        ctx.metadata["screen_observation_schema"] = observation.get("schema_version")
        ctx.metadata["screen_observation_target"] = (observation.get("request") or {}).get("target")
        _record_screen_analysis_metadata(ctx)
        ctx.tools.append(
            {
                "name": "screen_analyzer",
                "required": True,
                "ok": True,
                "target": (observation.get("request") or {}).get("target"),
                "source": (observation.get("capture") or {}).get("source"),
            }
        )
        _log_timing(
            services,
            "screen_attachment_analysis",
            duration,
            target=(observation.get("request") or {}).get("target"),
            source=(observation.get("capture") or {}).get("source"),
            stream_enabled=ctx.timing.get("screen_analysis_stream_enabled"),
            stream_fallback_used=ctx.timing.get("screen_analysis_stream_fallback_used"),
            first_delta_ms=ctx.timing.get("screen_analysis_first_delta_ms"),
        )
    except ScreenToolError as exc:
        duration = elapsed_ms(started_ms)
        ctx.timing["screen_analysis_ms"] = duration
        _record_screen_analysis_metadata(ctx)
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
        ctx.timing["screen_analysis_ms"] = duration
        _record_screen_analysis_metadata(ctx)
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
def build_prompt_node(ctx: TurnContext, services: AgentServices) -> TurnContext:
    if _skip_if_error(ctx):
        return ctx
    recent_context = ctx.recent.recent_context if ctx.recent else []
    long_term_memories = ctx.recent.long_term_memories if ctx.recent else []
    prompt_input = build_spica_prompt(
        user_input=ctx.user_input,
        recent_context=recent_context,
        long_term_memories=long_term_memories,
        character_profile=str(services.config.get("character_profile") or DEFAULT_CHARACTER_PROFILE),
        memory_limit=int(services.config.get("long_term_memory_limit", 5)),
        memory_budget_chars=int(services.config.get("long_term_memory_budget_chars", 1200)),
        recent_turn_char_limit=int(services.config.get("recent_turn_char_limit", 360)),
        interlocutor_name=str(services.config.get("interlocutor_name") or DEFAULT_INTERLOCUTOR_NAME),
        character_name=str(services.config.get("character_name") or DEFAULT_CHARACTER_NAME),
        user_local_time=ctx.user_local_time if ctx.request.include_user_time_context else None,
    )
    if ctx.screen_observation:
        prompt_input = _inject_screen_observation(prompt_input, ctx.screen_observation)
    ctx.prompt = PromptBundle(prompt_input=prompt_input)
    ctx.metadata["prompt_input_chars"] = len(str(prompt_input))
    return ctx


@node_timer
def call_llm_node(ctx: TurnContext, services: AgentServices) -> TurnContext:
    if _skip_if_error(ctx):
        return ctx
    if services.llm_client is None:
        ctx.error = TurnError("LLM_CLIENT_NOT_CONFIGURED", "LLM client 未配置。")
        return ctx

    model = str(services.config.get("model") or "gpt-4.1-mini")
    max_rounds = max(1, int(services.config.get("max_tool_rounds", 3)))
    prompt_input = ctx.prompt.prompt_input if ctx.prompt else None
    active_tool_schemas = (
        []
        if ctx.request.screen_attachment or ctx.screen_observation
        else tool_schemas_for_user_text(ctx.user_input, services.tool_schemas)
    )
    use_tools = bool(active_tool_schemas)
    ctx.metadata["use_tools"] = use_tools
    ctx.metadata["available_tool_schema_count"] = len(services.tool_schemas)
    ctx.metadata["selected_tool_schema_count"] = len(active_tool_schemas)
    ctx.timing["agent_tool_local_ms"] = 0.0
    ctx.timing["agent_followup_response_ms"] = 0.0
    ctx.timing["agent_function_calls"] = 0
    ctx.timing["agent_rounds"] = 0
    ctx.timing["agent_model"] = model
    ctx.timing["prompt_input_chars"] = len(str(prompt_input or ""))

    _log_timing(services, "tool_schema_gate", 0.0, use_tools=use_tools, user_chars=len(ctx.user_input))

    prompt_for_round = str(prompt_input or "")
    tool_history: list[dict[str, Any]] = []
    response = None

    answer = StreamedAnswer()
    ctx.answer = answer

    adapter = _llm_adapter(services)
    if adapter.prefers_chat_completions():
        ctx.timing["agent_rounds"] = 1
        if use_tools and active_tool_schemas:
            ctx.timing["agent_tool_probe_skipped"] = True
            ctx.timing["agent_tool_probe_skip_reason"] = "chat_completions_compatible_client"
        response_start_ms = now_ms()
        answer.raw_model_output = adapter.complete_chat(model, prompt_for_round, ctx)
        response_duration = elapsed_ms(response_start_ms)
        ctx.timing["agent_response_initial_ms"] = response_duration
        ctx.timing["raw_answer_chars"] = len(answer.raw_model_output or "")
        _log_timing(
            services,
            "agent_chat_completion",
            response_duration,
            phase="initial",
            model=model,
            use_tools=False,
        )
        return ctx

    for round_index in range(max_rounds):
        ctx.timing["agent_rounds"] = round_index + 1
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
            ctx.timing["agent_response_initial_ms"] = response_duration
            phase = "initial"
        else:
            ctx.timing["agent_followup_response_ms"] = round(
                float(ctx.timing.get("agent_followup_response_ms") or 0) + response_duration,
                2,
            )
            phase = "followup"

        _record_usage(ctx, response)
        _log_timing(
            services,
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
            ctx.timing["raw_answer_chars"] = len(answer.raw_model_output or "")
            return ctx

        for item in function_calls:
            ctx.timing["agent_function_calls"] += 1
            tool_start_ms = now_ms()
            tool_name = str(_get_attr(item, "name", ""))
            arguments = str(_get_attr(item, "arguments", "") or "{}")
            tool_result = run_local_tool(services.tool_functions, tool_name, arguments)
            record_screen_tool_result(ctx, tool_name, tool_result)
            tool_duration = elapsed_ms(tool_start_ms)
            ctx.timing["agent_tool_local_ms"] = round(
                float(ctx.timing.get("agent_tool_local_ms") or 0) + tool_duration,
                2,
            )
            tool_history.append(
                {
                    "name": tool_name,
                    "arguments": arguments,
                    "output": tool_result,
                }
            )
            _log_timing(
                services,
                "agent_tool_local",
                tool_duration,
                name=tool_name,
                arguments_chars=len(arguments),
                output_chars=len(tool_result),
            )

        prompt_for_round = _build_tool_followup_prompt(prompt_input, tool_history)

    ctx.error = TurnError("LLM_TOOL_LOOP_EXCEEDED", "工具调用轮数超过限制。")
    if response is not None:
        answer.raw_model_output = str(_get_attr(response, "output_text", "") or "")
        ctx.response_id = str(_get_attr(response, "id", "") or "") or None
    return ctx


@node_timer
def parse_reply_node(ctx: TurnContext, services: AgentServices) -> TurnContext:
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
def build_visual_node(ctx: TurnContext, services: AgentServices) -> TurnContext:
    if _skip_if_error(ctx):
        return ctx
    if services.visual_tool is None:
        return ctx
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
            ctx.timing["visual_classifier_ms"] = classifier_meta["duration_ms"]
        if isinstance(classifier_meta.get("segments"), int):
            ctx.timing["visual_segments"] = classifier_meta["segments"]
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
def synthesize_tts_node(ctx: TurnContext, services: AgentServices) -> TurnContext:
    if _skip_if_error(ctx):
        return ctx
    provider = _tts_adapter_name(services)
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
        if result.timing:
            ctx.timing.update(result.timing)
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
def build_response_node(ctx: TurnContext, services: AgentServices) -> TurnContext:
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


def record_screen_tool_result(ctx: TurnContext, tool_name: str, tool_result: str) -> None:
    if tool_name != "inspect_screen":
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
    _record_screen_analysis_metadata(ctx)


def _record_screen_analysis_metadata(ctx: TurnContext) -> None:
    metadata = get_last_screen_analysis_metadata()
    for key in ("screen_analysis_stream_enabled", "screen_analysis_stream_fallback_used"):
        if key in metadata:
            ctx.timing[key] = metadata[key]
            ctx.metadata[key] = metadata[key]
    if metadata.get("screen_analysis_first_delta_ms") is not None:
        ctx.timing["screen_analysis_first_delta_ms"] = metadata["screen_analysis_first_delta_ms"]
    for key in ("screen_analysis_engine", "screen_analysis_model", "screen_analysis_revision", "screen_analysis_local"):
        if key in metadata:
            ctx.metadata[key] = metadata[key]
    for key in ("screen_analysis_moondream_ms", "screen_analysis_total_ms"):
        if key in metadata:
            ctx.timing[key] = metadata[key]


def _compact_tool_history_for_prompt(tool_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact_history: list[dict[str, Any]] = []
    for item in tool_history:
        compact_item = dict(item)
        if compact_item.get("name") == "inspect_screen":
            compact_item["output"] = _compact_screen_tool_output(str(compact_item.get("output") or ""))
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


def _record_usage(ctx: TurnContext, response: Any) -> None:
    usage = _get_attr(response, "usage")
    if not usage:
        return
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = _get_attr(usage, key)
        if value is not None:
            ctx.timing[key] = value


_DEEPSEEK_BRANCH_MOVED = "agent.nodes DeepSeek/OpenAI branch moved to spica.adapters.llm (Phase 5)"
