from __future__ import annotations

import json
from functools import wraps
from typing import Any, Callable

from agent.character_loader import DEFAULT_CHARACTER_NAME, DEFAULT_INTERLOCUTOR_NAME
from agent.prompt_builder import DEFAULT_CHARACTER_PROFILE, build_spica_prompt
from agent.reply_parser import EMOTION_LABELS, normalize_emotion, parse_model_reply
from agent.state import AgentServices, AgentState
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


DEFAULT_SCREEN_ATTACHMENT_QUESTION = "请查看这张截图并概括内容。"


def node_timer(func: Callable[[AgentState, AgentServices], AgentState]):
    @wraps(func)
    def wrapper(state: AgentState, services: AgentServices) -> AgentState:
        start_ms = now_ms()
        try:
            return func(state, services)
        except Exception as exc:
            if state.error is None:
                state.error = {"code": "NODE_FAILED", "message": f"{func.__name__}: {exc}"}
            return state
        finally:
            duration = elapsed_ms(start_ms)
            state.timing[f"{func.__name__}_ms"] = duration
            _log_timing(services, func.__name__, duration, conversation_id=state.conversation_id)

    return wrapper


def _log_timing(services: AgentServices, step: str, duration_ms: float, **fields: Any) -> None:
    logger = services.logger or log_timing
    logger(step, duration_ms, **fields)


def _skip_if_error(state: AgentState) -> bool:
    return state.error is not None


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


def _build_tts_request(state: AgentState, text: str, emotion: str) -> TTSRequest:
    return TTSRequest(
        text=text,
        emotion=emotion,
        extra={"tts_param_overrides": state.tts_param_overrides or {}},
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
def validate_input_node(state: AgentState, services: AgentServices) -> AgentState:
    state.user_input = (state.user_input or "").strip()
    state.visual_overrides = state.visual_overrides or {}
    if not state.user_input and state.screen_attachment:
        state.user_input = DEFAULT_SCREEN_ATTACHMENT_QUESTION
    if state.include_user_time_context and not state.user_local_time:
        state.user_local_time = build_local_time_context()
    state.metadata["user_local_time"] = state.user_local_time if state.include_user_time_context else None
    state.metadata["interaction_mode"] = state.interaction_mode
    state.metadata["has_screen_attachment"] = bool(state.screen_attachment)
    if not state.user_input:
        state.answer = "メッセージを入力してください。"
        state.emotion = "surprised"
        state.error = {"code": "EMPTY_MESSAGE", "message": "message 不能为空。"}
    return state


@node_timer
def load_recent_context_node(state: AgentState, services: AgentServices) -> AgentState:
    if _skip_if_error(state):
        return state
    state.recent_context = services.recent_memory.get_recent(
        state.conversation_id,
        limit=int(services.config.get("recent_context_limit", 3)),
    )
    state.metadata["recent_context_count"] = len(state.recent_context)
    return state


@node_timer
def retrieve_long_term_memory_node(state: AgentState, services: AgentServices) -> AgentState:
    if _skip_if_error(state):
        return state
    # Read through MemoryPort.retrieve so the read key matches commit_turn's
    # character-namespaced write key (Phase 5/7). A bare conversation_id here
    # silently misses every auto-extracted memory. Reuse the same adapter
    # resolution as the write path -- no second fallback.
    scope = MemoryScope(
        character_id=str(services.config.get("character_id") or "spica"),
        user_id=str(services.config.get("interlocutor_name") or DEFAULT_INTERLOCUTOR_NAME),
        conversation_id=state.conversation_id,
    )
    items = _memory_adapter(services).retrieve(
        scope,
        state.user_input,
        limit=int(services.config.get("long_term_memory_limit", 5)),
    )
    # build_spica_prompt / _format_memories consume dicts (scope / content /
    # memory_type); map MemoryItem back so the prompt's scope label survives.
    state.long_term_memories = [
        {
            "scope": item.scope,
            "content": item.text,
            "memory_type": item.type,
            "importance": item.importance,
            "score": item.score,
        }
        for item in items
    ]
    state.metadata["long_term_memory_count"] = len(state.long_term_memories)
    return state


@node_timer
def analyze_screen_attachment_node(state: AgentState, services: AgentServices) -> AgentState:
    if _skip_if_error(state):
        return state
    if not state.screen_attachment:
        return state

    started_ms = now_ms()
    clear_last_screen_analysis_metadata()
    try:
        observation = analyze_screen_attachment(
            attachment=state.screen_attachment,
            user_question=state.user_input,
        )
        state.screen_observation = observation
        duration = elapsed_ms(started_ms)
        state.timing["screen_analysis_ms"] = duration
        state.metadata["screen_observation_used"] = True
        state.metadata["screen_observation_schema"] = observation.get("schema_version")
        state.metadata["screen_observation_target"] = (observation.get("request") or {}).get("target")
        _record_screen_analysis_metadata(state)
        state.tools.append(
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
            stream_enabled=state.timing.get("screen_analysis_stream_enabled"),
            stream_fallback_used=state.timing.get("screen_analysis_stream_fallback_used"),
            first_delta_ms=state.timing.get("screen_analysis_first_delta_ms"),
        )
    except ScreenToolError as exc:
        duration = elapsed_ms(started_ms)
        state.timing["screen_analysis_ms"] = duration
        _record_screen_analysis_metadata(state)
        state.tools.append(
            {
                "name": "screen_analyzer",
                "required": True,
                "ok": False,
                "error": {"code": exc.code, "message": exc.message},
            }
        )
        state.error = {"code": exc.code, "message": exc.message}
    except Exception as exc:
        duration = elapsed_ms(started_ms)
        state.timing["screen_analysis_ms"] = duration
        _record_screen_analysis_metadata(state)
        state.tools.append(
            {
                "name": "screen_analyzer",
                "required": True,
                "ok": False,
                "error": {"code": "SCREEN_ANALYSIS_FAILED", "message": str(exc)},
            }
        )
        state.error = {"code": "SCREEN_ANALYSIS_FAILED", "message": str(exc)}
    return state


@node_timer
def build_prompt_node(state: AgentState, services: AgentServices) -> AgentState:
    if _skip_if_error(state):
        return state
    state.prompt_input = build_spica_prompt(
        user_input=state.user_input,
        recent_context=state.recent_context,
        long_term_memories=state.long_term_memories,
        character_profile=str(services.config.get("character_profile") or DEFAULT_CHARACTER_PROFILE),
        memory_limit=int(services.config.get("long_term_memory_limit", 5)),
        memory_budget_chars=int(services.config.get("long_term_memory_budget_chars", 1200)),
        recent_turn_char_limit=int(services.config.get("recent_turn_char_limit", 360)),
        interlocutor_name=str(services.config.get("interlocutor_name") or DEFAULT_INTERLOCUTOR_NAME),
        character_name=str(services.config.get("character_name") or DEFAULT_CHARACTER_NAME),
        user_local_time=state.user_local_time if state.include_user_time_context else None,
    )
    if state.screen_observation:
        state.prompt_input = _inject_screen_observation(state.prompt_input, state.screen_observation)
    state.metadata["prompt_input_chars"] = len(str(state.prompt_input))
    return state


@node_timer
def call_llm_node(state: AgentState, services: AgentServices) -> AgentState:
    if _skip_if_error(state):
        return state
    if services.llm_client is None:
        state.error = {"code": "LLM_CLIENT_NOT_CONFIGURED", "message": "LLM client 未配置。"}
        return state

    model = str(services.config.get("model") or "gpt-4.1-mini")
    max_rounds = max(1, int(services.config.get("max_tool_rounds", 3)))
    active_tool_schemas = (
        []
        if state.screen_attachment or state.screen_observation
        else tool_schemas_for_user_text(state.user_input, services.tool_schemas)
    )
    use_tools = bool(active_tool_schemas)
    state.metadata["use_tools"] = use_tools
    state.metadata["available_tool_schema_count"] = len(services.tool_schemas)
    state.metadata["selected_tool_schema_count"] = len(active_tool_schemas)
    state.timing["agent_tool_local_ms"] = 0.0
    state.timing["agent_followup_response_ms"] = 0.0
    state.timing["agent_function_calls"] = 0
    state.timing["agent_rounds"] = 0
    state.timing["agent_model"] = model
    state.timing["prompt_input_chars"] = len(str(state.prompt_input or ""))

    _log_timing(services, "tool_schema_gate", 0.0, use_tools=use_tools, user_chars=len(state.user_input))

    prompt_for_round = str(state.prompt_input or "")
    tool_history: list[dict[str, Any]] = []
    response = None

    adapter = _llm_adapter(services)
    if adapter.prefers_chat_completions():
        state.timing["agent_rounds"] = 1
        if use_tools and active_tool_schemas:
            state.timing["agent_tool_probe_skipped"] = True
            state.timing["agent_tool_probe_skip_reason"] = "chat_completions_compatible_client"
        response_start_ms = now_ms()
        state.raw_model_output = adapter.complete_chat(model, prompt_for_round, state)
        response_duration = elapsed_ms(response_start_ms)
        state.timing["agent_response_initial_ms"] = response_duration
        state.timing["raw_answer_chars"] = len(state.raw_model_output or "")
        _log_timing(
            services,
            "agent_chat_completion",
            response_duration,
            phase="initial",
            model=model,
            use_tools=False,
        )
        return state

    for round_index in range(max_rounds):
        state.timing["agent_rounds"] = round_index + 1
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
            state.timing["agent_response_initial_ms"] = response_duration
            phase = "initial"
        else:
            state.timing["agent_followup_response_ms"] = round(
                float(state.timing.get("agent_followup_response_ms") or 0) + response_duration,
                2,
            )
            phase = "followup"

        _record_usage(state, response)
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
            state.raw_model_output = str(_get_attr(response, "output_text", "") or "")
            state.response_id = str(_get_attr(response, "id", "") or "") or None
            state.timing["raw_answer_chars"] = len(state.raw_model_output or "")
            return state

        for item in function_calls:
            state.timing["agent_function_calls"] += 1
            tool_start_ms = now_ms()
            tool_name = str(_get_attr(item, "name", ""))
            arguments = str(_get_attr(item, "arguments", "") or "{}")
            tool_result = run_local_tool(services.tool_functions, tool_name, arguments)
            record_screen_tool_result(state, tool_name, tool_result)
            tool_duration = elapsed_ms(tool_start_ms)
            state.timing["agent_tool_local_ms"] = round(
                float(state.timing.get("agent_tool_local_ms") or 0) + tool_duration,
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

        prompt_for_round = _build_tool_followup_prompt(state.prompt_input, tool_history)

    state.error = {"code": "LLM_TOOL_LOOP_EXCEEDED", "message": "工具调用轮数超过限制。"}
    if response is not None:
        state.raw_model_output = str(_get_attr(response, "output_text", "") or "")
        state.response_id = str(_get_attr(response, "id", "") or "") or None
    return state


@node_timer
def parse_reply_node(state: AgentState, services: AgentServices) -> AgentState:
    if _skip_if_error(state):
        return state
    state.parsed_reply = parse_model_reply(state.raw_model_output or "")
    state.answer = normalize_square_brackets_for_speech(state.parsed_reply["answer"])
    state.parsed_reply["answer"] = state.answer
    state.emotion = normalize_emotion(state.emotion_override or state.parsed_reply["emotion"])
    return state


# save_recent_context_node + extract_memory_node were unified with the streaming
# path into spica/runtime/memory_commit.save_stream_memory (Phase 6D).


@node_timer
def build_visual_node(state: AgentState, services: AgentServices) -> AgentState:
    if _skip_if_error(state):
        return state
    if services.visual_tool is None:
        return state
    try:
        state.visual = services.visual_tool.build_visual_payload(
            answer=state.answer or "",
            emotion=state.emotion or "happy",
            requested_costume=state.visual_overrides.get("costume_set"),
            requested_mode=state.visual_overrides.get("costume_mode"),
        )
        classifier_meta = state.visual.get("classifier") if isinstance(state.visual.get("classifier"), dict) else {}
        if isinstance(classifier_meta.get("duration_ms"), (int, float)):
            state.timing["visual_classifier_ms"] = classifier_meta["duration_ms"]
        if isinstance(classifier_meta.get("segments"), int):
            state.timing["visual_segments"] = classifier_meta["segments"]
        state.tools.append(
            {
                "name": "spica_visual_diff",
                "required": False,
                "ok": True,
                "costume": state.visual.get("costume"),
                "classifier_version": state.visual.get("classifier_version"),
                "selection_source": state.visual.get("selection_source"),
                "selection_error": state.visual.get("selection_error"),
            }
        )
    except Exception as exc:
        state.tools.append(
            {
                "name": "spica_visual_diff",
                "required": False,
                "ok": False,
                "error": str(exc),
            }
        )
    return state


@node_timer
def synthesize_tts_node(state: AgentState, services: AgentServices) -> AgentState:
    if _skip_if_error(state):
        return state
    provider = _tts_adapter_name(services)
    if services.tts_adapter is None:
        state.tools.append(
            {
                "name": provider,
                "required": True,
                "ok": False,
                "error": "TTS adapter is not configured.",
            }
        )
        state.error = {"code": "TTS_TOOL_NOT_CONFIGURED", "message": "TTS adapter 未初始化。"}
        return state
    try:
        state.tts_result = services.tts_adapter.synthesize(
            _build_tts_request(
                state,
                text=state.answer or "",
                emotion=state.emotion or "happy",
            )
        )
        if state.tts_result.timing:
            state.timing.update(state.tts_result.timing)
        if not state.tts_result.ok:
            state.tools.append(
                {
                    "name": state.tts_result.provider,
                    "required": True,
                    "ok": False,
                    "error": state.tts_result.error,
                }
            )
            state.error = {"code": "TTS_FAILED", "message": state.tts_result.error or "TTS synthesis failed."}
            return state
        state.tools.append(
            {
                "name": state.tts_result.provider,
                "required": True,
                "ok": True,
                "audio_url": state.tts_result.audio_url,
            }
        )
    except Exception as exc:
        state.tools.append(
            {
                "name": provider,
                "required": True,
                "ok": False,
                "error": str(exc),
            }
        )
        state.error = {"code": "TTS_FAILED", "message": str(exc)}
    return state


@node_timer
def build_response_node(state: AgentState, services: AgentServices) -> AgentState:
    emotion = normalize_emotion(state.emotion or "surprised")
    emotion_reason = "用户输入为空。" if state.error and state.error.get("code") == "EMPTY_MESSAGE" else "模型按回复语气选择。"
    if state.parsed_reply:
        emotion_reason = state.parsed_reply.get("emotion_reason") or emotion_reason

    payload = {
        "answer": state.answer or "メッセージを入力してください。",
        "conversation_id": state.conversation_id,
        "emotion": {
            "name": emotion,
            "label": EMOTION_LABELS[emotion],
            "reason": emotion_reason,
        },
        "audio_url": None,
        "audio_path": None,
        "tts_params": None,
        "visual": state.visual,
        "tools": state.tools,
        "timing": state.timing,
    }

    if state.tts_result:
        payload["audio_url"] = state.tts_result.audio_url
        payload["audio_path"] = state.tts_result.audio_path
        payload["tts_chunks"] = _legacy_tts_chunks(state.tts_result)
        payload["tts_chunk_audio"] = _legacy_tts_chunk_audio(state.tts_result)
        if state.tts_result.error:
            payload["tts_error"] = state.tts_result.error

    if state.error:
        payload["error"] = state.error

    state.response_payload = payload
    return state


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


def record_screen_tool_result(state: AgentState, tool_name: str, tool_result: str) -> None:
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
    state.screen_observation = data
    state.metadata["screen_observation_used"] = True
    state.metadata["screen_observation_schema"] = data.get("schema_version")
    state.metadata["screen_observation_target"] = (data.get("request") or {}).get("target")
    state.metadata["screen_observation_source"] = (data.get("capture") or {}).get("source")
    _record_screen_analysis_metadata(state)


def _record_screen_analysis_metadata(state: AgentState) -> None:
    metadata = get_last_screen_analysis_metadata()
    for key in ("screen_analysis_stream_enabled", "screen_analysis_stream_fallback_used"):
        if key in metadata:
            state.timing[key] = metadata[key]
            state.metadata[key] = metadata[key]
    if metadata.get("screen_analysis_first_delta_ms") is not None:
        state.timing["screen_analysis_first_delta_ms"] = metadata["screen_analysis_first_delta_ms"]
    for key in ("screen_analysis_engine", "screen_analysis_model", "screen_analysis_revision", "screen_analysis_local"):
        if key in metadata:
            state.metadata[key] = metadata[key]
    for key in ("screen_analysis_moondream_ms", "screen_analysis_total_ms"):
        if key in metadata:
            state.timing[key] = metadata[key]


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


def _record_usage(state: AgentState, response: Any) -> None:
    usage = _get_attr(response, "usage")
    if not usage:
        return
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = _get_attr(usage, key)
        if value is not None:
            state.timing[key] = value


_DEEPSEEK_BRANCH_MOVED = "agent.nodes DeepSeek/OpenAI branch moved to spica.adapters.llm (Phase 5)"
