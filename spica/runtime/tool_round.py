"""Tool probe / followup for the streaming runtime (Phase 6C).

Moved verbatim from agent/streaming_pipeline.py. Before streaming the final
answer, optionally run one Responses tool round (probe -> run local tools ->
build a followup prompt). DeepSeek-compatible clients skip the probe. Qt-free.
"""

from __future__ import annotations

import json
from typing import Any

from agent.nodes import _compact_tool_history_for_prompt, record_screen_tool_result
from common.timing import elapsed_ms, log_timing, now_ms
from spica.runtime.llm_stream import get_attr, llm_adapter, record_usage
from spica.runtime.tools import LegacyFunctionToolSet


def prepare_prompt_for_streaming(
    state: Any,
    services: Any,
    put_status: Any,
    deps: Any = None,
) -> tuple[str, str | None]:
    if services.llm_client is None:
        raise RuntimeError("LLM client 未配置。")

    # C3a: tools go through the injected ToolSet (deps.tools). Direct
    # stream_voice_events callers (tests) get an equivalent Legacy wrapper.
    tools = deps.tools if deps is not None else LegacyFunctionToolSet.from_services(services)
    model = str(services.config.get("model") or "gpt-4.1-mini")
    active_tool_schemas = (
        []
        if state.screen_attachment or state.screen_observation
        else tools.schemas_for_user_text(state.user_input)
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

    if llm_adapter(services).prefers_chat_completions():
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
    record_usage(state, response)
    log_timing(
        "agent_response",
        response_ms,
        phase="tool_probe",
        model=model,
        use_tools=True,
        round=1,
    )

    function_calls = [
        item for item in list(get_attr(response, "output", []) or [])
        if get_attr(item, "type") == "function_call"
    ]
    if not function_calls:
        state.response_id = str(get_attr(response, "id", "") or "") or None
        return str(state.prompt_input or ""), str(get_attr(response, "output_text", "") or "")

    tool_history: list[dict[str, Any]] = []
    for item in function_calls:
        state.timing["agent_function_calls"] += 1
        tool_start_ms = now_ms()
        tool_name = str(get_attr(item, "name", ""))
        arguments = str(get_attr(item, "arguments", "") or "{}")
        if tool_name == "inspect_screen":
            put_status("tools", "inspecting_screen")
        else:
            put_status("tools", f"tool:{tool_name}")
        tool_result = tools.run(tool_name, arguments)
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
    return build_tool_followup_prompt(state.prompt_input, tool_history), None


def build_tool_followup_prompt(prompt_input: Any, tool_history: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        [
            str(prompt_input),
            "[TOOL_RESULTS]",
            json.dumps(_compact_tool_history_for_prompt(tool_history), ensure_ascii=False),
            "[NEXT_STEP]",
            "请只根据以上工具结果输出最终 JSON，不要 Markdown，不要解释工具链。",
        ]
    )
