from __future__ import annotations

import json
from functools import wraps
from typing import Any, Callable

from agent.character_loader import DEFAULT_INTERLOCUTOR_NAME
from memory.control import save_extracted_memories
from agent.prompt_builder import DEFAULT_CHARACTER_PROFILE, build_spica_prompt
from agent.reply_parser import EMOTION_LABELS, normalize_emotion, parse_model_reply
from agent.state import AgentServices, AgentState
from common.timing import elapsed_ms, log_timing, now_ms
from agent_tools.router import run_local_tool, should_use_tools


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


@node_timer
def validate_input_node(state: AgentState, services: AgentServices) -> AgentState:
    state.user_input = (state.user_input or "").strip()
    state.visual_overrides = state.visual_overrides or {}
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
    state.long_term_memories = services.memory_store.search_memories(
        state.conversation_id,
        state.user_input,
        limit=int(services.config.get("long_term_memory_limit", 5)),
    )
    state.metadata["long_term_memory_count"] = len(state.long_term_memories)
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
    )
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
    use_tools = should_use_tools(state.user_input)
    state.metadata["use_tools"] = use_tools
    state.timing["agent_tool_local_ms"] = 0.0
    state.timing["agent_followup_response_ms"] = 0.0
    state.timing["agent_function_calls"] = 0
    state.timing["agent_rounds"] = 0
    state.timing["agent_model"] = model
    state.timing["prompt_input_chars"] = len(str(state.prompt_input or ""))

    _log_timing(services, "tool_router", 0.0, use_tools=use_tools, user_chars=len(state.user_input))

    prompt_for_round = str(state.prompt_input or "")
    tool_history: list[dict[str, Any]] = []
    response = None

    if _prefers_chat_completions(services.llm_client):
        state.timing["agent_rounds"] = 1
        if use_tools and services.tool_schemas:
            state.timing["agent_tool_probe_skipped"] = True
            state.timing["agent_tool_probe_skip_reason"] = "chat_completions_compatible_client"
        response_start_ms = now_ms()
        response = services.llm_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt_for_round}],
        )
        response_duration = elapsed_ms(response_start_ms)
        state.timing["agent_response_initial_ms"] = response_duration
        _record_usage(state, response)
        choices = list(_get_attr(response, "choices", []) or [])
        if choices:
            message = _get_attr(choices[0], "message")
            state.raw_model_output = str(_get_attr(message, "content", "") or "")
        else:
            state.raw_model_output = ""
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
        if use_tools and services.tool_schemas:
            request["tools"] = services.tool_schemas

        response_start_ms = now_ms()
        response = services.llm_client.responses.create(**request)
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
    state.answer = state.parsed_reply["answer"]
    state.emotion = normalize_emotion(state.emotion_override or state.parsed_reply["emotion"])
    return state


@node_timer
def save_recent_context_node(state: AgentState, services: AgentServices) -> AgentState:
    if _skip_if_error(state):
        return state
    services.recent_memory.append_turn(state.conversation_id, state.user_input, state.answer or "")
    return state


@node_timer
def extract_memory_node(state: AgentState, services: AgentServices) -> AgentState:
    if _skip_if_error(state):
        return state
    result = save_extracted_memories(
        memory_store=services.memory_store,
        conversation_id=state.conversation_id,
        user_input=state.user_input,
        assistant_answer=state.answer or "",
        max_active_memories=int(services.config.get("max_long_term_memories", 200)),
        interlocutor_name=str(services.config.get("interlocutor_name") or DEFAULT_INTERLOCUTOR_NAME),
    )
    state.metadata.update(result)
    return state


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
    if services.tts_tool is None:
        state.tools.append(
            {
                "name": "gptsovits_tts",
                "required": True,
                "ok": False,
                "error": "TTS tool is not configured.",
            }
        )
        state.error = {"code": "TTS_TOOL_NOT_CONFIGURED", "message": "GPT-SoVITS tool 未初始化。"}
        return state
    try:
        state.tts_result = services.tts_tool.synthesize(
            text=state.answer or "",
            emotion=state.emotion or "happy",
            tts_param_overrides=state.tts_param_overrides,
        )
        if state.tts_result.get("timing"):
            state.timing.update(state.tts_result["timing"])
        state.tools.append(
            {
                "name": "gptsovits_tts",
                "required": True,
                "ok": True,
                "audio_url": state.tts_result["audio_url"],
            }
        )
    except Exception as exc:
        state.tools.append(
            {
                "name": "gptsovits_tts",
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
        payload["audio_url"] = state.tts_result.get("audio_url")
        payload["audio_path"] = state.tts_result.get("audio_path")
        payload["tts_params"] = state.tts_result.get("tts_params")
        payload["tts_chunks"] = state.tts_result.get("tts_chunks")
        payload["tts_chunk_audio"] = state.tts_result.get("tts_chunk_audio")
        payload["reference"] = state.tts_result.get("reference")

    if state.error:
        payload["error"] = state.error

    state.response_payload = payload
    return state


def _build_tool_followup_prompt(prompt_input: Any, tool_history: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        [
            str(prompt_input),
            "[TOOL_RESULTS]",
            json.dumps(tool_history, ensure_ascii=False),
            "[NEXT_STEP]",
            "请只根据以上工具结果输出最终 JSON，不要 Markdown，不要解释工具链。",
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


def _prefers_chat_completions(client: Any) -> bool:
    base_url = str(_get_attr(client, "base_url", "") or "").lower()
    return "deepseek" in base_url and _has_chat_completions(client)


def _has_chat_completions(client: Any) -> bool:
    chat = _get_attr(client, "chat")
    completions = _get_attr(chat, "completions") if chat is not None else None
    return completions is not None and hasattr(completions, "create")
