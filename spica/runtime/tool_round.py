"""Tool probe / followup for the streaming runtime (Phase 6C).

Moved verbatim from agent/streaming_pipeline.py. Before streaming the final
answer, optionally run one tool round (probe -> run local tools -> build a
followup prompt). The probe goes through the Responses API or -- for clients
that prefer Chat Completions (DeepSeek) -- through a chat tool probe; both feed
the same local tool execution / followup chain. Qt-free.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterator

from spica.runtime.stages import _compact_tool_history_for_prompt, record_screen_tool_result
from common.timing import elapsed_ms, now_ms
from spica.runtime.context import TurnContext, is_turn_cancelled
from spica.runtime.llm_stream import get_attr, record_usage

logger = logging.getLogger(__name__)

# Sentinel yielded by the streaming chat-tool generator BETWEEN the probe phase and
# the followup phase (tool turns only): tells the orchestrator to discard the raw
# accumulated so far (the plain, unplayed tool preamble) so only the followup answer
# reaches the final parse / memory. The no-tool path never yields it.
STREAM_RESET = object()


def prepare_prompt_for_streaming(
    ctx: TurnContext,
    services: Any,
    put_status: Any,
    deps: Any = None,
) -> tuple[str, str | None]:
    if services.llm_client is None:
        raise RuntimeError("LLM client 未配置。")

    # Tools (C3a) and config / LLM port (C3b) come from the injected deps; the
    # orchestrator always bridges dict-config callers before reaching here.
    tools = deps.tools
    model = deps.config.llm.model
    prompt_input = ctx.prompt.prompt_input if ctx.prompt else None
    # P3: a SYSTEM-initiated turn (proactive speech) gets NO tools, hard-off --
    # its directive may mention 唱/看屏 etc. and the supply wordlist would
    # otherwise offer tools right back to her (self-excitation: a "just finished
    # singing" report re-triggering sing_song). Typed gate; default turns
    # byte-identical.
    active_tool_schemas = (
        []
        if (
            ctx.request.screen_attachment
            or ctx.screen_observation
            or ctx.request.interaction_mode == "system"
        )
        else tools.schemas_for_user_text(ctx.user_input)
    )
    use_tools = bool(active_tool_schemas)
    obs = deps.observer
    ctx.metadata["use_tools"] = use_tools
    ctx.metadata["available_tool_schema_count"] = len(services.tool_schemas)
    ctx.metadata["selected_tool_schema_count"] = len(active_tool_schemas)
    obs.mark("agent_tool_local_ms", 0.0)
    obs.mark("agent_function_calls", 0)
    obs.mark("agent_rounds", 0)

    if not use_tools or not active_tool_schemas:
        return str(prompt_input or ""), None

    # Supply diagnostic on the REAL streaming path (the previous triage logged
    # only on the sync path and never fired). DEBUG: raise the level when
    # triaging tool supply. Empty-schemas turns return above, so normal
    # dialogue stays byte-identical (no log, no code).
    logger.debug(
        "stream turn tools offered: %s",
        [s.get("name") or (s.get("function") or {}).get("name") for s in active_tool_schemas],
    )

    compact_lookup = getattr(tools, "compact_output", None)

    # No status during the probe (either endpoint): "deciding whether to use a
    # tool" is not "processing tools" -- the UI keeps showing the plain pending
    # dots until a tool ACTUALLY executes (_run_tool_calls emits per-tool status).
    if deps.llm.prefers_chat_completions():
        # Chat Completions tool probe (DeepSeek etc.). Until this branch existed
        # the probe was skipped entirely and tools never reached the request --
        # the watch_game_screen zero-trigger root cause (FINDINGS #18).
        def _probe_chat(prompt_text: str, round_number: int) -> tuple[list[dict[str, str]], str]:
            probe_start_ms = now_ms()
            calls, text = deps.llm.create_chat_with_tools(
                model=model,
                prompt=prompt_text,
                tools=active_tool_schemas,
                state=ctx,
            )
            probe_ms = elapsed_ms(probe_start_ms)
            obs.mark("agent_rounds", round_number)
            if round_number == 1:
                obs.mark("agent_response_initial_ms", probe_ms)
            else:
                obs.bump("agent_followup_response_ms", probe_ms)
            obs.event(
                "agent_response",
                probe_ms,
                phase="tool_probe",
                endpoint="chat_completions",
                model=model,
                use_tools=True,
                round=round_number,
            )
            return calls, text

        # Round 1 STREAMS: the no-tool JSON answer plays as it generates (the latency
        # win) instead of waiting for the whole non-streamed reply. The whole flow
        # (probe stream -> [tools -> followup stream]) is ONE generator the orchestrator
        # consumes through its existing delta loop. A plain-text tool preamble carries
        # no "answer" field -> the orchestrator's JsonAnswerExtractor drops it (nothing
        # played); STREAM_RESET then clears it from raw before the followup answer.
        def _chat_tool_stream() -> Iterator[Any]:
            calls_sink: list[dict[str, str]] = []
            probe_start_ms = now_ms()
            for delta in deps.llm.iter_chat_with_tools(
                model=model, prompt=str(prompt_input or ""),
                tools=active_tool_schemas, state=ctx, tool_calls_sink=calls_sink,
            ):
                yield delta
            probe_ms = elapsed_ms(probe_start_ms)
            obs.mark("agent_rounds", 1)
            obs.mark("agent_response_initial_ms", probe_ms)
            obs.event(
                "agent_response", probe_ms, phase="tool_probe",
                endpoint="chat_completions", model=model, use_tools=True, round=1,
            )
            if not calls_sink:
                return  # no tool -> the JSON answer already streamed (the win)
            if is_turn_cancelled(ctx.request):
                return  # cancelled during the (unplayed) preamble -> never run tools
            yield STREAM_RESET  # drop the plain preamble; keep only the followup answer
            tool_history = _run_tool_calls(ctx, obs, tools, put_status, calls_sink)
            if not _any_chainable(tools, calls_sink):
                # Single-shot tools (watch/note/inspect): one followup, streamed.
                put_status("thinking", "thinking")
                followup = build_tool_followup_prompt(prompt_input, tool_history, compact_lookup)
                for delta in deps.llm.iter_response_text({"model": model, "input": followup}, ctx):
                    if is_turn_cancelled(ctx.request):
                        break
                    yield delta
                return
            # Chainable tools are dormant today (all single-shot); the chain re-probes
            # NON-streaming (round 2+) then its final answer is emitted as one delta.
            chain_prompt, chain_text = _run_chain_rounds(
                ctx, deps, obs, tools, put_status, prompt_input, tool_history, _probe_chat
            )
            if chain_text is not None:
                yield chain_text
                return
            for delta in deps.llm.iter_response_text({"model": model, "input": chain_prompt}, ctx):
                if is_turn_cancelled(ctx.request):
                    break
                yield delta

        return str(prompt_input or ""), _chat_tool_stream()

    def _probe_responses(prompt_text: str, round_number: int) -> tuple[list[dict[str, str]], str]:
        request = {
            "model": model,
            "input": prompt_text,
            "tools": active_tool_schemas,
        }
        response_start_ms = now_ms()
        response = deps.llm.create_responses(**request)
        response_ms = elapsed_ms(response_start_ms)
        obs.mark("agent_rounds", round_number)
        if round_number == 1:
            obs.mark("agent_response_initial_ms", response_ms)
        else:
            obs.bump("agent_followup_response_ms", response_ms)
        record_usage(obs, response)
        obs.event(
            "agent_response",
            response_ms,
            phase="tool_probe",
            model=model,
            use_tools=True,
            round=round_number,
        )
        function_calls = [
            item for item in list(get_attr(response, "output", []) or [])
            if get_attr(item, "type") == "function_call"
        ]
        if not function_calls:
            ctx.response_id = str(get_attr(response, "id", "") or "") or None
        normalized = [
            {
                "name": str(get_attr(item, "name", "")),
                "arguments": str(get_attr(item, "arguments", "") or "{}"),
            }
            for item in function_calls
        ]
        return normalized, str(get_attr(response, "output_text", "") or "")

    normalized_calls, probe_text = _probe_responses(str(prompt_input or ""), 1)
    if not normalized_calls:
        return str(prompt_input or ""), probe_text

    tool_history = _run_tool_calls(ctx, obs, tools, put_status, normalized_calls)
    if not _any_chainable(tools, normalized_calls):
        # Single-shot tools only: today's single-round path, byte for byte.
        put_status("thinking", "thinking")
        return build_tool_followup_prompt(prompt_input, tool_history, compact_lookup), None
    return _run_chain_rounds(
        ctx, deps, obs, tools, put_status, prompt_input, tool_history, _probe_responses
    )


def _any_chainable(tools: Any, calls: list[dict[str, str]]) -> bool:
    """P1 gate: only a tool that DECLARED chainable=True pulls the turn into the
    multi-round loop. Toolsets without the query (legacy fakes) -> False, so the
    single-round path stays the default everywhere."""
    query = getattr(tools, "chainable", None)
    if not callable(query):
        return False
    return any(query(call["name"]) for call in calls)


def _run_chain_rounds(
    ctx: TurnContext,
    deps: Any,
    obs: Any,
    tools: Any,
    put_status: Any,
    prompt_input: Any,
    tool_history: list[dict[str, Any]],
    probe: Any,
) -> tuple[str, str | None]:
    """P1 chain rounds (round 2..max_tool_rounds) for chainable tools.

    Each round re-probes WITH tools (non-streaming, same endpoint helper as
    round 1); no calls -> that probe's text IS the final answer (the prefetched
    channel the round-1 decline path already uses -- accepted cost: a chained
    flow's final answer pops instead of streaming). On exceeding the budget the
    STREAMING chain forces a graceful final: one streamed followup WITHOUT
    tools, prompt-noted to stop calling tools -- she always answers; triage
    reads the warning + observer event. (The frozen sync chain keeps its
    historical LLM_TOOL_LOOP_EXCEEDED error instead -- see stages.call_llm_node.)
    """
    compact_lookup = getattr(tools, "compact_output", None)
    max_rounds = max(1, int(deps.config.max_tool_rounds))
    followup = build_tool_followup_prompt(prompt_input, tool_history, compact_lookup)
    for round_number in range(2, max_rounds + 1):
        calls, text = probe(followup, round_number)
        if not calls:
            return followup, text
        _run_tool_calls(ctx, obs, tools, put_status, calls, history=tool_history)
        followup = build_tool_followup_prompt(prompt_input, tool_history, compact_lookup)
    logger.warning(
        "tool loop exceeded max_tool_rounds=%d; forcing a final answer without tools",
        max_rounds,
    )
    obs.event("tool_loop_exceeded", 0.0, rounds=max_rounds)
    put_status("thinking", "thinking")
    return (
        build_tool_followup_prompt(prompt_input, tool_history, compact_lookup, force_final=True),
        None,
    )


def _run_tool_calls(
    ctx: TurnContext,
    obs: Any,
    tools: Any,
    put_status: Any,
    calls: list[dict[str, str]],
    history: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Execute normalized tool calls (``{"name", "arguments"}``) locally.

    The single execution chain shared by the Responses probe and the Chat
    Completions probe -- status, observer events and screen-result recording
    are identical regardless of which endpoint produced the calls. Chain rounds
    pass ``history`` so executions accumulate into the same list."""
    tool_history: list[dict[str, Any]] = history if history is not None else []
    for call in calls:
        # #1 checkpoint ①: a cancelled turn stops BEFORE executing any (further)
        # tool. This is what blocks ghost sing_song -- its SongRequestEvent rides
        # the companion_sink bridge, bypassing the stream token, so once tools.run
        # fires nothing else can stop it singing. Returns the history accumulated
        # so far. Deadline: cancelled None/unset -> is_turn_cancelled False -> never
        # taken, every tool runs exactly as before.
        if is_turn_cancelled(ctx.request):
            break
        obs.bump("agent_function_calls", 1)
        tool_start_ms = now_ms()
        tool_name = call["name"]
        arguments = call["arguments"]
        if tool_name == "inspect_screen":
            put_status("tools", "inspecting_screen")
        else:
            put_status("tools", f"tool:{tool_name}")
        tool_result = tools.run(tool_name, arguments)
        record_screen_tool_result(ctx, obs, tool_name, tool_result)
        tool_duration = elapsed_ms(tool_start_ms)
        obs.bump("agent_tool_local_ms", tool_duration)
        tool_history.append({"name": tool_name, "arguments": arguments, "output": tool_result})
        obs.event(
            "agent_tool_local",
            tool_duration,
            name=tool_name,
            arguments_chars=len(arguments),
            output_chars=len(tool_result),
        )
    return tool_history


def build_tool_followup_prompt(
    prompt_input: Any,
    tool_history: list[dict[str, Any]],
    compact_lookup: Any = None,
    force_final: bool = False,
) -> str:
    sections = [
        str(prompt_input),
        "[TOOL_RESULTS]",
        json.dumps(
            _compact_tool_history_for_prompt(tool_history, compact_lookup), ensure_ascii=False
        ),
        "[NEXT_STEP]",
        "请只根据以上工具结果输出最终 JSON，不要 Markdown，不要解释工具链。",
    ]
    if force_final:
        # Loop-budget exceeded (P1): the graceful forced final -- streamed, no
        # tools offered, and the prompt says so explicitly.
        sections.append("不要再调用工具，基于已有结果回答。")
    return "\n\n".join(sections)
