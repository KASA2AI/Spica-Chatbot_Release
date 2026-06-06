from __future__ import annotations

import concurrent.futures
import json
import os
import queue
import re
import threading
from typing import Any, Iterator

from agent.character_loader import DEFAULT_INTERLOCUTOR_NAME
from spica.adapters.memory import SqliteMemoryAdapter
from spica.ports.memory import MemoryScope
from agent.nodes import (
    analyze_screen_attachment_node,
    build_prompt_node,
    load_recent_context_node,
    record_screen_tool_result,
    retrieve_long_term_memory_node,
    validate_input_node,
    _compact_tool_history_for_prompt,
)
from agent.reply_parser import EMOTION_LABELS, guess_emotion, normalize_emotion, parse_model_reply
from agent.state import AgentServices, AgentState
from agent.text_normalizer import build_tts_text, normalize_square_brackets_for_speech
from agent.time_context import format_local_time_for_prompt
from common.timing import elapsed_ms, log_timing, now_ms
from agent_tools.function_tools import run_local_tool, tool_schemas_for_user_text
from agent_tools.function_tools.screen.schema import screen_observation_context_for_next_turn
from agent_tools.tts.schemas import TTSRequest
from spica.adapters.llm import OpenAICompatibleAdapter
from spica.runtime.play_unit_splitter import JsonAnswerExtractor, PlayUnitSplitter


_SENTINEL = object()
_FIRST_UNIT_WARNING_MS = 3000.0


def _llm_adapter(services: AgentServices) -> OpenAICompatibleAdapter:
    return services.llm_adapter or OpenAICompatibleAdapter(services.llm_client)


def _memory_adapter(services: AgentServices):
    return services.memory_adapter or SqliteMemoryAdapter(services.memory_store, services.recent_memory)


def stream_voice_events(state: AgentState, services: AgentServices) -> Iterator[dict[str, Any]]:
    request_start_ms = now_ms()
    yield {"event": "status", "data": {"state": "thinking", "message": "thinking"}}

    output_queue: queue.Queue[Any] = queue.Queue()
    producer = threading.Thread(
        target=_produce_stream_events,
        args=(state, services, request_start_ms, output_queue),
        daemon=True,
    )
    producer.start()

    while True:
        item = output_queue.get()
        if item is _SENTINEL:
            break
        yield item
    producer.join(timeout=1)


def _produce_stream_events(
    state: AgentState,
    services: AgentServices,
    request_start_ms: float,
    output_queue: queue.Queue[Any],
) -> None:
    timing_lock = threading.Lock()
    ready_lock = threading.Lock()
    ready_units: dict[int, dict[str, Any]] = {}
    next_emit = {"index": 0}
    unit_timings: list[dict[str, Any]] = []
    created_units: list[str] = []
    first_unit_event = threading.Event()

    visual_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, int(os.getenv("VISUAL_STREAM_WORKERS") or 2))
    )
    tts_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    ready_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    ready_futures: list[concurrent.futures.Future[Any]] = []

    def relative_ms() -> float:
        return round(now_ms() - request_start_ms, 2)

    def set_timing_once(key: str, value: float) -> None:
        with timing_lock:
            state.timing.setdefault(key, value)

    def mark_first_unit_warning(reason: str) -> None:
        if first_unit_event.is_set():
            return
        warning_ms = relative_ms()
        with timing_lock:
            if state.timing.get("first_unit_warning_ms") is not None:
                return
            state.timing["first_unit_warning_ms"] = warning_ms
            state.timing["first_unit_warning"] = (
                f"first playable unit was not created within {int(_FIRST_UNIT_WARNING_MS)}ms"
            )
            state.timing["first_unit_warning_reason"] = reason
        log_timing(
            "chat_stream_first_unit_warning",
            warning_ms,
            conversation_id=state.conversation_id,
            reason=reason,
        )

    def put_status(state_name: str, message: str) -> None:
        output_queue.put({"event": "status", "data": {"state": state_name, "message": message}})

    def put_unit_event(event_name: str, event_data: dict[str, Any]) -> None:
        output_queue.put({"event": event_name, "data": event_data})

    def put_ready(index: int, event_data: dict[str, Any]) -> None:
        with ready_lock:
            ready_units[index] = event_data
            while next_emit["index"] in ready_units:
                ready_event = ready_units.pop(next_emit["index"])
                output_queue.put({"event": "unit_ready", "data": ready_event})
                next_emit["index"] += 1

    def submit_unit(display_text: str) -> None:
        display_text = normalize_square_brackets_for_speech((display_text or "").strip())
        if not display_text:
            return

        index = len(created_units)
        previous_units = list(created_units)
        full_answer_so_far = "".join(previous_units + [display_text])
        emotion = normalize_emotion(state.emotion_override or guess_emotion(full_answer_so_far))
        unit_timing: dict[str, Any] = {
            "unit_index": index,
            "unit_text_chars": len(display_text),
            "unit_created_ms": relative_ms(),
        }
        unit_timings.append(unit_timing)
        if index == 0:
            set_timing_once("first_unit_created_ms", unit_timing["unit_created_ms"])
            set_timing_once("first_sentence_ms", unit_timing["unit_created_ms"])
            if unit_timing["unit_created_ms"] > _FIRST_UNIT_WARNING_MS:
                mark_first_unit_warning("first_unit_created_after_threshold")
            first_unit_event.set()

        tts_text = build_tts_text(display_text)
        unit = {
            "index": index,
            "display_text": display_text,
            "tts_text": tts_text,
            "emotion": emotion,
            "previous_units": previous_units,
            "full_answer_so_far": full_answer_so_far,
            "timing": unit_timing,
        }
        put_unit_event(
            "unit_text_ready",
            {
                "index": index,
                "display_text": display_text,
                "tts_text": tts_text,
                "emotion": emotion,
                "timing": {
                    "unit_created_ms": unit_timing["unit_created_ms"],
                },
            },
        )
        visual_future = visual_executor.submit(
            _build_unit_visual_and_emit,
            services,
            state,
            unit,
            request_start_ms,
            set_timing_once,
            put_unit_event,
        )
        tts_future = tts_executor.submit(
            _synthesize_unit_audio,
            services,
            state,
            unit,
            request_start_ms,
            set_timing_once,
            put_unit_event,
        )
        ready_futures.append(
            ready_executor.submit(
                _finalize_unit,
                unit,
                visual_future,
                tts_future,
                request_start_ms,
                set_timing_once,
                put_ready,
            )
        )
        created_units.append(display_text)

    first_unit_timer = threading.Timer(
        _FIRST_UNIT_WARNING_MS / 1000.0,
        mark_first_unit_warning,
        args=("timer_threshold",),
    )
    first_unit_timer.daemon = True
    first_unit_timer.start()

    try:
        state = validate_input_node(state, services)
        if state.error:
            output_queue.put({"event": "error", "data": {"message": state.error.get("message") or "请求无效。"}})
            return

        state = load_recent_context_node(state, services)
        state = retrieve_long_term_memory_node(state, services)
        if state.screen_attachment:
            put_status("tools", "inspecting_screen")
        state = analyze_screen_attachment_node(state, services)
        if state.error:
            output_queue.put({"event": "error", "data": {"message": state.error.get("message") or "截图分析失败。"}})
            return
        state = build_prompt_node(state, services)
        if state.error:
            output_queue.put({"event": "error", "data": {"message": state.error.get("message") or "请求失败。"}})
            return

        visual_context = None
        if services.visual_tool is not None and hasattr(services.visual_tool, "prepare_stream_context"):
            visual_context = services.visual_tool.prepare_stream_context(
                requested_costume=state.visual_overrides.get("costume_set"),
                requested_mode=state.visual_overrides.get("costume_mode"),
            )
        state.metadata["stream_visual_context"] = visual_context

        model = str(services.config.get("model") or "gpt-4.1-mini")
        state.timing["agent_model"] = model
        state.timing["prompt_input_chars"] = len(str(state.prompt_input or ""))

        prompt_for_stream, prefetched_raw = _prepare_prompt_for_streaming(state, services, put_status)
        splitter = PlayUnitSplitter(
            min_chars=int(os.getenv("PLAY_UNIT_MIN_CHARS") or services.config.get("play_unit_min_chars") or 18),
            max_chars=int(os.getenv("PLAY_UNIT_MAX_CHARS") or services.config.get("play_unit_max_chars") or 96),
        )
        extractor = JsonAnswerExtractor()
        raw_model_parts: list[str] = []

        def handle_raw_delta(delta: str) -> None:
            if not delta:
                return
            set_timing_once("first_llm_delta_ms", relative_ms())
            with timing_lock:
                state.timing["llm_delta_events"] = int(state.timing.get("llm_delta_events") or 0) + 1
            raw_model_parts.append(delta)
            state.raw_model_output = "".join(raw_model_parts)
            answer_delta = extractor.feed(state.raw_model_output)
            if not answer_delta:
                return
            previous_sentence_count = splitter.completed_sentence_count
            units = splitter.feed(answer_delta)
            if splitter.completed_sentence_count > previous_sentence_count:
                set_timing_once("first_sentence_ms", relative_ms())
            for unit_text in units:
                submit_unit(unit_text)

        if prefetched_raw is not None:
            handle_raw_delta(prefetched_raw)
        else:
            request = {"model": model, "input": prompt_for_stream}
            for delta in _llm_adapter(services).iter_response_text(request, state):
                handle_raw_delta(delta)

        for unit_text in splitter.flush():
            submit_unit(unit_text)

        state.raw_model_output = "".join(raw_model_parts)
        state.parsed_reply = parse_model_reply(state.raw_model_output or "")
        state.answer = normalize_square_brackets_for_speech(state.parsed_reply["answer"])
        state.parsed_reply["answer"] = state.answer
        state.emotion = normalize_emotion(state.emotion_override or state.parsed_reply["emotion"])
        if not created_units and state.answer:
            fallback_splitter = PlayUnitSplitter(
                min_chars=splitter.min_chars,
                max_chars=splitter.max_chars,
            )
            for unit_text in fallback_splitter.feed(state.answer) + fallback_splitter.flush():
                submit_unit(unit_text)

        _save_stream_memory(state, services)

        for future in ready_futures:
            future.result()

        state.timing["done_ms"] = relative_ms()
        state.timing["units_count"] = len(created_units)
        state.timing["units"] = unit_timings
        log_timing(
            "chat_stream_done",
            state.timing["done_ms"],
            conversation_id=state.conversation_id,
            units_count=len(created_units),
            first_llm_delta_ms=state.timing.get("first_llm_delta_ms"),
            first_sentence_ms=state.timing.get("first_sentence_ms"),
            first_unit_created_ms=state.timing.get("first_unit_created_ms"),
            first_tts_start_ms=state.timing.get("first_tts_start_ms"),
            first_tts_done_ms=state.timing.get("first_tts_done_ms"),
            first_unit_ready_ms=state.timing.get("first_unit_ready_ms"),
            first_visual_ready_ms=state.timing.get("first_visual_ready_ms"),
            first_audio_ready_ms=state.timing.get("first_audio_ready_ms"),
            llm_stream_create_ms=state.timing.get("llm_stream_create_ms"),
            llm_stream_fallback_used=state.timing.get("llm_stream_fallback_used"),
        )
        output_queue.put(
            {
                "event": "done",
                "data": {
                    "answer": state.answer or "",
                    "emotion": state.emotion or "happy",
                    "emotion_label": EMOTION_LABELS[normalize_emotion(state.emotion or "happy")],
                    "emotion_reason": state.parsed_reply.get("emotion_reason") if state.parsed_reply else "",
                    "units_count": len(created_units),
                    "timing": state.timing,
                },
            }
        )
    except Exception as exc:
        output_queue.put({"event": "error", "data": {"message": str(exc)}})
        log_timing("chat_stream_error", relative_ms(), conversation_id=state.conversation_id, error=str(exc))
    finally:
        first_unit_timer.cancel()
        visual_executor.shutdown(wait=False, cancel_futures=False)
        tts_executor.shutdown(wait=False, cancel_futures=False)
        ready_executor.shutdown(wait=False, cancel_futures=False)
        output_queue.put(_SENTINEL)


def _prepare_prompt_for_streaming(
    state: AgentState,
    services: AgentServices,
    put_status: Any,
) -> tuple[str, str | None]:
    if services.llm_client is None:
        raise RuntimeError("LLM client 未配置。")

    model = str(services.config.get("model") or "gpt-4.1-mini")
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
    state.timing["agent_function_calls"] = 0
    state.timing["agent_rounds"] = 0

    if not use_tools or not active_tool_schemas:
        return str(state.prompt_input or ""), None

    if _llm_adapter(services).prefers_chat_completions():
        state.timing["agent_tool_probe_skipped"] = True
        state.timing["agent_tool_probe_skip_reason"] = "chat_completions_compatible_client"
        return str(state.prompt_input or ""), None

    put_status("tools", "processing_tools")
    request = {
        "model": model,
        "input": str(state.prompt_input or ""),
        "tools": active_tool_schemas,
    }
    response_start_ms = now_ms()
    response = services.llm_client.responses.create(**request)
    response_ms = elapsed_ms(response_start_ms)
    state.timing["agent_rounds"] = 1
    state.timing["agent_response_initial_ms"] = response_ms
    _record_usage(state, response)
    log_timing(
        "agent_response",
        response_ms,
        phase="tool_probe",
        model=model,
        use_tools=True,
        round=1,
    )

    function_calls = [
        item for item in list(_get_attr(response, "output", []) or [])
        if _get_attr(item, "type") == "function_call"
    ]
    if not function_calls:
        state.response_id = str(_get_attr(response, "id", "") or "") or None
        return str(state.prompt_input or ""), str(_get_attr(response, "output_text", "") or "")

    tool_history: list[dict[str, Any]] = []
    for item in function_calls:
        state.timing["agent_function_calls"] += 1
        tool_start_ms = now_ms()
        tool_name = str(_get_attr(item, "name", ""))
        arguments = str(_get_attr(item, "arguments", "") or "{}")
        if tool_name == "inspect_screen":
            put_status("tools", "inspecting_screen")
        else:
            put_status("tools", f"tool:{tool_name}")
        tool_result = run_local_tool(services.tool_functions, tool_name, arguments)
        record_screen_tool_result(state, tool_name, tool_result)
        tool_duration = elapsed_ms(tool_start_ms)
        state.timing["agent_tool_local_ms"] = round(
            float(state.timing.get("agent_tool_local_ms") or 0) + tool_duration,
            2,
        )
        tool_history.append({"name": tool_name, "arguments": arguments, "output": tool_result})
        log_timing(
            "agent_tool_local",
            tool_duration,
            name=tool_name,
            arguments_chars=len(arguments),
            output_chars=len(tool_result),
        )

    put_status("thinking", "thinking")
    return _build_tool_followup_prompt(state.prompt_input, tool_history), None


# LLM client I/O + the OpenAI-Responses vs Chat-Completions (DeepSeek) branch and
# streaming fallbacks were moved to spica/adapters/llm/openai_compatible.py (Phase 5).
# The pipeline now calls _llm_adapter(services).iter_response_text(...) /
# .prefers_chat_completions() instead of these in-pipeline functions.


def _build_unit_visual(
    services: AgentServices,
    state: AgentState,
    unit: dict[str, Any],
    request_start_ms: float,
    set_timing_once: Any,
) -> dict[str, Any]:
    unit_timing = unit["timing"]
    unit_index = int(unit["index"])
    classifier_start_abs = now_ms()
    unit_timing["visual_classifier_start_ms"] = round(classifier_start_abs - request_start_ms, 2)
    try:
        if services.visual_tool is None:
            raise RuntimeError("visual tool is not configured")
        payload = services.visual_tool.build_unit_visual_payload(
            current_unit_text=unit["display_text"],
            emotion=unit["emotion"],
            unit_index=unit_index,
            previous_units=unit["previous_units"],
            full_answer_so_far=unit["full_answer_so_far"],
            runtime_context=state.metadata.get("stream_visual_context"),
            requested_costume=state.visual_overrides.get("costume_set"),
            requested_mode=state.visual_overrides.get("costume_mode"),
        )
        classifier = payload.get("classifier") if isinstance(payload.get("classifier"), dict) else {}
        duration_ms = classifier.get("duration_ms")
        if not isinstance(duration_ms, (int, float)):
            duration_ms = elapsed_ms(classifier_start_abs)
        cue = payload.get("cue") if isinstance(payload.get("cue"), dict) else {}
        visual = {
            "expression_id": cue.get("expression_id"),
            "hand_pose": cue.get("hand_pose"),
            "image_url": cue.get("image_url"),
            "image_path": cue.get("image_path"),
            "reason": cue.get("reason"),
            "selection_source": payload.get("selection_source") or "local_vote_classifier",
            "classifier_version": payload.get("classifier_version"),
            "duration_ms": duration_ms,
            "confidence": classifier.get("confidence"),
            "signals": classifier.get("signals", []),
            "selection_error": payload.get("selection_error"),
            "costume": payload.get("costume"),
            "costume_mode": payload.get("costume_mode"),
            "background_url": payload.get("background_url"),
            "dialog": payload.get("dialog"),
            "character": payload.get("character"),
            "cue": cue,
            "cues": payload.get("cues") if isinstance(payload.get("cues"), list) else [cue],
        }
        unit_timing["visual_classifier_duration_ms"] = duration_ms
        unit_timing["visual_classifier_version"] = visual["classifier_version"]
        unit_timing["visual_selection_source"] = visual["selection_source"]
        unit_timing["visual_selection_error"] = visual["selection_error"]
        log_timing(
            "visual_classifier_unit",
            duration_ms,
            unit_index=unit_index,
            chars=len(unit["display_text"]),
            version=visual["classifier_version"],
            source=visual["selection_source"],
            error=visual["selection_error"],
        )
        return visual
    except Exception as exc:
        duration_ms = elapsed_ms(classifier_start_abs)
        unit_timing["visual_classifier_duration_ms"] = duration_ms
        unit_timing["visual_classifier_version"] = None
        unit_timing["visual_selection_source"] = "visual_error"
        unit_timing["visual_selection_error"] = str(exc)
        return {
            "expression_id": None,
            "hand_pose": None,
            "image_url": None,
            "reason": "visual classifier failed",
            "selection_source": "visual_error",
            "classifier_version": None,
            "duration_ms": duration_ms,
            "selection_error": str(exc),
        }
    finally:
        if unit_index == 0:
            set_timing_once("first_visual_ready_ms", round(now_ms() - request_start_ms, 2))


def _cue_from_visual_payload(visual: dict[str, Any]) -> dict[str, Any]:
    cue = visual.get("cue") if isinstance(visual.get("cue"), dict) else {}
    if cue:
        return cue
    cues = visual.get("cues") if isinstance(visual.get("cues"), list) else []
    if cues and isinstance(cues[0], dict):
        return cues[0]
    return {}


def _build_unit_visual_and_emit(
    services: AgentServices,
    state: AgentState,
    unit: dict[str, Any],
    request_start_ms: float,
    set_timing_once: Any,
    put_unit_event: Any,
) -> dict[str, Any]:
    visual = _build_unit_visual(services, state, unit, request_start_ms, set_timing_once)
    unit_timing = unit["timing"]
    unit_index = int(unit["index"])
    visual_ready_ms = round(now_ms() - request_start_ms, 2)
    unit_timing["visual_ready_ms"] = visual_ready_ms
    put_unit_event(
        "unit_visual_ready",
        {
            "index": unit_index,
            "visual": visual,
            "cue": _cue_from_visual_payload(visual),
            "visual_error": visual.get("selection_error"),
            "timing": {
                "visual_ms": unit_timing.get("visual_classifier_duration_ms"),
                "visual_ready_ms": visual_ready_ms,
            },
        },
    )
    return visual


def _synthesize_unit_audio(
    services: AgentServices,
    state: AgentState,
    unit: dict[str, Any],
    request_start_ms: float,
    set_timing_once: Any,
    put_unit_event: Any,
) -> dict[str, Any]:
    unit_timing = unit["timing"]
    unit_index = int(unit["index"])
    tts_start_ms = now_ms()
    tts_start_relative_ms = round(tts_start_ms - request_start_ms, 2)
    unit_timing["tts_start_ms"] = tts_start_relative_ms
    if unit_index == 0:
        set_timing_once("first_tts_start_ms", tts_start_relative_ms)
    put_unit_event(
        "unit_audio_started",
        {
            "index": unit_index,
            "tts_text": unit["tts_text"],
            "emotion": unit["emotion"],
            "timing": {
                "tts_start_ms": tts_start_relative_ms,
            },
        },
    )
    audio_payload: dict[str, Any] = {
        "audio_url": None,
        "audio_path": None,
        "audio_error": None,
        "tts_result": None,
        "duration_ms": None,
    }
    try:
        if services.tts_adapter is None:
            raise RuntimeError("TTS adapter is not configured")
        result = services.tts_adapter.synthesize(
            TTSRequest(
                text=unit["tts_text"],
                emotion=unit["emotion"],
                extra={"tts_param_overrides": state.tts_param_overrides or {}},
            )
        )
        if not result.ok:
            raise RuntimeError(result.error or "TTS synthesis failed")
        duration_ms = result.duration_ms
        if not isinstance(duration_ms, (int, float)):
            duration_ms = result.timing.get("tts_total_ms")
        if not isinstance(duration_ms, (int, float)):
            duration_ms = elapsed_ms(tts_start_ms)
        unit_timing["tts_duration_ms"] = duration_ms
        audio_payload = {
            "audio_url": result.audio_url,
            "audio_path": result.audio_path,
            "audio_error": None,
            "tts_result": result,
            "duration_ms": duration_ms,
        }
    except Exception as exc:
        duration_ms = elapsed_ms(tts_start_ms)
        unit_timing["tts_duration_ms"] = duration_ms
        unit_timing["tts_error"] = str(exc)
        audio_payload = {
            "audio_url": None,
            "audio_path": None,
            "audio_error": str(exc),
            "tts_result": None,
            "duration_ms": duration_ms,
        }
    finally:
        tts_done_relative_ms = round(now_ms() - request_start_ms, 2)
        unit_timing["tts_done_ms"] = tts_done_relative_ms
        if unit_index == 0:
            set_timing_once("first_tts_done_ms", tts_done_relative_ms)
            set_timing_once("first_audio_ready_ms", tts_done_relative_ms)
        put_unit_event(
            "unit_audio_ready",
            {
                "index": unit_index,
                "audio_url": audio_payload.get("audio_url"),
                "audio_path": audio_payload.get("audio_path"),
                "audio_error": audio_payload.get("audio_error"),
                "timing": {
                    "tts_ms": unit_timing.get("tts_duration_ms"),
                    "tts_start_ms": unit_timing.get("tts_start_ms"),
                    "tts_done_ms": unit_timing.get("tts_done_ms"),
                },
            },
        )
    return audio_payload


def _finalize_unit(
    unit: dict[str, Any],
    visual_future: concurrent.futures.Future[dict[str, Any]],
    tts_future: concurrent.futures.Future[dict[str, Any]],
    request_start_ms: float,
    set_timing_once: Any,
    put_ready: Any,
) -> None:
    visual = visual_future.result()
    audio = tts_future.result()
    unit_timing = unit["timing"]
    unit_ready_ms = round(now_ms() - request_start_ms, 2)
    unit_timing["unit_ready_ms"] = unit_ready_ms
    if int(unit["index"]) == 0:
        set_timing_once("first_unit_ready_ms", unit_ready_ms)
    log_timing(
        "chat_stream_unit_ready",
        unit_ready_ms,
        unit_index=unit["index"],
        visual_ms=unit_timing.get("visual_classifier_duration_ms"),
        tts_ms=unit_timing.get("tts_duration_ms"),
        audio_error=audio.get("audio_error"),
    )
    data = {
        "index": unit["index"],
        "display_text": unit["display_text"],
        "tts_text": unit["tts_text"],
        "emotion": unit["emotion"],
        "visual": visual,
        "audio_url": audio.get("audio_url"),
        "audio_path": audio.get("audio_path"),
        "timing": {
            "visual_ms": unit_timing.get("visual_classifier_duration_ms"),
            "tts_ms": unit_timing.get("tts_duration_ms"),
            "unit_ready_ms": unit_ready_ms,
        },
    }
    if audio.get("audio_error"):
        data["audio_error"] = audio["audio_error"]
    put_ready(int(unit["index"]), data)


def _save_stream_memory(state: AgentState, services: AgentServices) -> None:
    try:
        services.recent_memory.append_turn(
            state.conversation_id,
            state.user_input,
            state.answer or "",
            user_local_time=(
                format_local_time_for_prompt(state.user_local_time)
                if state.include_user_time_context
                else None
            ),
            interaction_mode=state.interaction_mode,
            screen_observation_context=screen_observation_context_for_next_turn(state.screen_observation),
        )
        interlocutor = str(services.config.get("interlocutor_name") or DEFAULT_INTERLOCUTOR_NAME)
        result = _memory_adapter(services).commit_turn(
            MemoryScope(
                character_id=str(services.config.get("character_id") or "spica"),
                user_id=interlocutor,
                conversation_id=state.conversation_id,
            ),
            state.user_input,
            state.answer or "",
            meta={
                "interlocutor_name": interlocutor,
                "max_active_memories": int(services.config.get("max_long_term_memories", 200)),
            },
        )
        state.metadata.update(result)
    except Exception as exc:
        state.metadata["memory_error"] = str(exc)


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


def _record_usage(state: AgentState, response: Any) -> None:
    usage = _get_attr(response, "usage")
    if not usage:
        return
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = _get_attr(usage, key)
        if value is not None:
            state.timing[key] = value


def _get_attr(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)
