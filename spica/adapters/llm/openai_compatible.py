"""OpenAI-compatible LLM adapter (Phase 5; model port v2 since OO migration
Phase 6a/7).

Encapsulates the chat-model client I/O and the OpenAI-Responses vs
Chat-Completions (e.g. DeepSeek) branch + streaming fallbacks that previously
lived inline in ``agent/streaming_pipeline.py`` and ``agent/nodes.py``. The
adapter carries TWO surfaces over the same internals:

- the frozen v1 ``LLMPort`` method family -- kept verbatim for the sync museum
  chain (``stages.call_llm_node`` / ``sync_chain``) and the adapter-level
  tests; and
- the v2 ``TextModel`` / ``ToolCallingModel`` face (``complete`` / ``stream``
  / ``probe`` / ``probe_stream``), thin over the v1 paths so the endpoint and
  fallback logic keeps a single home. The production runtime enters ONLY here,
  through a ``BoundModel`` (``deps.model``) -- never through v1 methods
  (guarded by ``tests/test_no_v1_llm_in_runtime.py``).

``state`` is typed ``Any`` to avoid a spica -> agent import; its ``timing`` /
``response_id`` attributes are updated and ``request.interaction_mode`` is read
to select the configured System Turn reasoning lane.
"""

from __future__ import annotations

from copy import deepcopy
from concurrent.futures import CancelledError
from types import SimpleNamespace
from typing import Any, Iterator

from common.timing import elapsed_ms, log_timing, now_ms
from spica.adapters.llm.usage import record_usage as _record_usage, request as _measured_request
from spica.ports.model import ModelInput, ToolProbeResult, ToolProbeStream


def check_model_connection(*, base_url: str | None, model: str, api_key: str) -> dict[str, Any]:
    """Bounded connectivity check, with no conversation, tools or generation."""
    import httpx
    from openai import OpenAI, APIStatusError, APITimeoutError, APIConnectionError

    try:
        # Match the chat client's network policy: ignore environment proxies
        # and certificate overrides, so this checks the same connection.
        with httpx.Client(trust_env=False, timeout=8.0) as http_client:
            with OpenAI(api_key=api_key, base_url=base_url or "https://api.openai.com/v1",
                        http_client=http_client, timeout=8.0, max_retries=0) as client:
                models = client.models.list()
                found = any(item.id == model for item in models.data)
    except APIStatusError as exc:
        if exc.status_code in {401, 403}:
            raise ValueError("连接被拒绝，请检查密钥与服务权限。") from None
        if exc.status_code in {404, 405}:
            raise ValueError("服务未提供模型列表接口，请确认 API 地址；本次无法验证连接。") from None
        raise ValueError(f"连接测试失败（HTTP {exc.status_code}），请检查服务状态。") from None
    except APITimeoutError:
        raise ValueError("连接超时，请检查 API 地址和网络。") from None
    except APIConnectionError:
        raise ValueError("无法连接，请检查 API 地址、网络和证书。") from None
    except Exception:
        # SDK/remote exceptions can contain credentials or request bodies.
        raise ValueError("无法读取模型列表，请检查服务是否兼容。") from None
    return {"message": ("连接成功，模型列表包含所填模型。生成效果请在重启后通过聊天验证。" if found
                        else "连接成功，但模型列表未包含所填模型，请向服务商确认模型 ID。")}


def to_chat_completions_tools(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert tool schemas to the Chat Completions nested format.

    The registry stores Responses-flat schemas (top-level ``name`` /
    ``description`` / ``parameters``); Chat Completions (DeepSeek etc.) requires
    ``{"type": "function", "function": {...}}``. Already-nested schemas pass
    through unchanged. Pure function -- no client, no I/O."""
    converted: list[dict[str, Any]] = []
    for schema in schemas:
        if isinstance(schema.get("function"), dict):
            converted.append(schema)
            continue
        function = {
            key: schema[key]
            for key in ("name", "description", "parameters", "strict")
            if key in schema
        }
        converted.append({"type": "function", "function": function})
    return converted


_GPT_EFFORTS = ("none", "low", "medium", "high")


def _model_is_deepseek(model: str | None) -> bool:
    return "deepseek" in (model or "").lower()


def _model_is_gpt(model: str | None) -> bool:
    return (model or "").lower().startswith("gpt")


def _reasoning_chat_kwargs(model: str | None, effort: str) -> dict[str, Any]:
    """Reasoning/thinking kwargs for a chat.completions request. {} for
    'default'/unknown (send NOTHING -> the provider's own default, zero-diff).
    DeepSeek V4 supports low/high/max effort (medium maps to high); 'none'
    disables thinking. Older DeepSeek models retain their provider default.
    GPT uses reasoning_effort = none/low/medium/high."""
    if not effort or effort == "default":
        return {}
    if _model_is_deepseek(model):
        if effort == "none":
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        if "deepseek-v4" in (model or "").lower() and effort in {"low", "medium", "high", "max"}:
            # https://api-docs.deepseek.com/guides/thinking_mode/
            return {"extra_body": {"thinking": {"type": "enabled"}},
                    "reasoning_effort": "high" if effort == "medium" else effort}
        return {}
    if _model_is_gpt(model) and effort in _GPT_EFFORTS:
        return {"reasoning_effort": effort}
    return {}


def _reasoning_responses_kwargs(model: str | None, effort: str) -> dict[str, Any]:
    """Same, for a responses.create request (gpt only -- deepseek uses chat here)."""
    if not effort or effort == "default":
        return {}
    if _model_is_gpt(model) and effort in _GPT_EFFORTS:
        return {"reasoning": {"effort": effort}}
    return {}


class OpenAICompatibleAdapter:
    """LLM adapter over an OpenAI-compatible client (OpenAI, DeepSeek, ...)."""

    name = "openai_compatible"
    supports_bounded_completion = True

    def __init__(
        self,
        client: Any,
        reasoning_effort: str = "default",
        system_turn_reasoning_effort: str | None = None,
    ) -> None:
        self.client = client
        # Main reasoning control remains the value for chat, tools, summaries,
        # and every legacy surface. System stream requests may use the dedicated
        # YAML-only override; None inherits this main value.
        self._reasoning_effort = reasoning_effort
        self._system_turn_reasoning_effort = system_turn_reasoning_effort

    def prefers_chat_completions(self) -> bool:
        return _prefers_chat_completions(self.client)

    def has_chat_completions(self) -> bool:
        return _has_chat_completions(self.client)

    def create_responses(self, *, state: Any = None, **request: Any) -> Any:
        """One-shot Responses API call (synchronous tool loop / probe)."""
        reasoning = _reasoning_responses_kwargs(request.get("model"), self._reasoning_effort)
        request = dict(request)
        if "input" in request:
            request["input"] = _responses_input(request["input"])
        client = _client_for_turn(self.client, state)
        request = self._bounded_request({**reasoning, **request}, state, 'responses')
        return _request(client.responses.create, state=state, api='responses', **request)

    @staticmethod
    def _bounded_request(request, state, api):
        options = getattr(state, 'completion_options', None)
        if options is not None:
            key = 'max_output_tokens' if api == 'responses' else (
                'max_completion_tokens' if _model_is_gpt(request.get('model')) else 'max_tokens')
            request[key] = options.max_output_tokens
            options.check_input(request)
        return request

    @staticmethod
    def _check_completion(response, state, api):
        if getattr(state, 'completion_options', None) is None:
            return
        from spica.ports.model import CompletionIncomplete
        if api == 'responses':
            reason = _get_attr(response, 'status')
            if reason != 'completed':
                raise CompletionIncomplete(reason if reason in {'incomplete', 'failed', 'in_progress'} else 'missing_status')
        else:
            choices = list(_get_attr(response, 'choices', []) or [])
            reason = _get_attr(choices[0], 'finish_reason') if len(choices) == 1 else None
            if reason != 'stop':
                raise CompletionIncomplete(reason if reason in {
                    'length', 'content_filter', 'tool_calls', 'insufficient_system_resource', 'aborted'} else 'missing_finish_reason')

    @staticmethod
    def response_items(response: Any) -> list[dict[str, Any]]:
        return [_provider_value(item) for item in (_get_attr(response, "output", []) or [])]

    def complete_chat(self, model: str, prompt: ModelInput, state: Any) -> str:
        """One-shot Chat Completions call, returning the assistant text."""
        client = _client_for_turn(self.client, state)
        request = self._bounded_request(dict(model=model, messages=_chat_messages(prompt, state),
            **_reasoning_chat_kwargs(model, self._reasoning_effort)), state, 'chat')
        response = _request(client.chat.completions.create, state=state, api='chat', **request)
        _record_usage(state, response)
        self._check_completion(response, state, 'chat')
        choices = list(_get_attr(response, "choices", []) or [])
        if choices:
            message = _get_attr(choices[0], "message")
            return str(_get_attr(message, "content", "") or "")
        return ""

    def create_chat_with_tools(
        self,
        *,
        model: str,
        prompt: ModelInput,
        tools: list[dict[str, Any]],
        state: Any,
    ) -> tuple[list[dict[str, str]], str]:
        """One-shot Chat Completions tool probe (the chat-path counterpart of the
        Responses probe). Sends ``tools`` in the nested chat format and returns
        ``(tool_calls, text)`` where each tool_call is ``{"name", "arguments"}``
        (arguments = raw JSON string) and ``text`` is the assistant content."""
        client = _client_for_turn(self.client, state)
        probe_start_ms = now_ms()
        response = _request(client.chat.completions.create, state=state, api='chat',
            model=model,
            messages=_chat_messages(prompt, state),
            tools=to_chat_completions_tools(tools),
            **_reasoning_chat_kwargs(model, self._reasoning_effort),
        )
        log_timing("llm_chat_tool_probe", elapsed_ms(probe_start_ms), model=model)
        _record_usage(state, response)
        choices = list(_get_attr(response, "choices", []) or [])
        message = _get_attr(choices[0], "message") if choices else None
        text = str(_get_attr(message, "content", "") or "")
        calls: list[dict[str, str]] = []
        for item in list(_get_attr(message, "tool_calls", []) or []):
            function = _get_attr(item, "function")
            name = str(_get_attr(function, "name", "") or "")
            if name:
                calls.append(
                    {
                        "name": name,
                        **_call_continuation(item, message),
                        "arguments": str(_get_attr(function, "arguments", "") or "{}"),
                    }
                )
        return calls, text

    def iter_chat_with_tools(
        self,
        *,
        model: str,
        prompt: ModelInput,
        tools: list[dict[str, Any]],
        state: Any,
        tool_calls_sink: list[dict[str, str]],
    ) -> Iterator[str]:
        """STREAMING chat tool probe (the streaming counterpart of
        ``create_chat_with_tools``). Streams ``chat.completions`` WITH tools and:

        - yields ``delta.content`` text live (so the JSON answer of a no-tool turn
          plays as it generates -- the latency win; a plain-text tool preamble like
          "让我先看看屏幕" carries no ``"answer":`` field, so the caller's
          JsonAnswerExtractor drops it and nothing is spoken);
        - accumulates ``delta.tool_calls`` across chunks BY INDEX (the streamed
          ``function.arguments`` arrive in fragments) and, at stream end, appends the
          completed ``{"name","arguments"}`` calls to ``tool_calls_sink`` (a return
          channel -- a generator cannot return a value cleanly).

        Single-worker / serial use only (one turn streams at a time). Provider
        usage trailers are recorded at the shared request boundary."""
        probe_start_ms = now_ms()
        client = _client_for_turn(self.client, state)
        stream = _request(client.chat.completions.create, state=state, api='chat',
            model=model,
            messages=_chat_messages(prompt, state),
            tools=to_chat_completions_tools(tools),
            stream=True,
            **_reasoning_chat_kwargs(model, self._reasoning_effort),
        )
        acc: dict[int, dict[str, str]] = {}
        reasoning_content = ""
        assistant_text = ""
        try:
            for chunk in stream:
                choices = list(_get_attr(chunk, "choices", []) or [])
                if not choices:
                    continue
                delta = _get_attr(choices[0], "delta")
                content = str(_get_attr(delta, "content", "") or "")
                reasoning_content += str(_get_attr(delta, "reasoning_content", "") or "")
                assistant_text += content
                if content:
                    yield content
                for item in list(_get_attr(delta, "tool_calls", []) or []):
                    index = int(_get_attr(item, "index", 0) or 0)
                    slot = acc.setdefault(index, {"name": "", "arguments": ""})
                    call_id = str(_get_attr(item, "id", "") or "")
                    if call_id:
                        slot["id"] = call_id
                    function = _get_attr(item, "function")
                    name = str(_get_attr(function, "name", "") or "")
                    if name:
                        slot["name"] = name
                    arguments = str(_get_attr(function, "arguments", "") or "")
                    if arguments:
                        slot["arguments"] += arguments
        finally:
            _close_stream(stream)
        log_timing("llm_chat_tool_probe", elapsed_ms(probe_start_ms), model=model, streamed=True)
        for index in sorted(acc):
            if acc[index]["name"]:
                tool_calls_sink.append(
                    {**acc[index], "arguments": acc[index]["arguments"] or "{}",
                     **({"reasoning_content": reasoning_content} if reasoning_content else {}),
                     **({"assistant_text": assistant_text} if assistant_text else {})}
                )

    def iter_response_text(self, request: dict[str, Any], state: Any) -> Iterator[str]:
        """Stream assistant text deltas, with all fallbacks handled internally."""
        return _iter_response_text(self.client, request, state, self._reasoning_effort)

    def complete_text(self, prompt: ModelInput, *, model: str) -> str:
        """One-shot, turn-independent completion (Phase 8 summarization). Reuses the
        same endpoint/branch logic as the dialogue path but non-streaming and without
        a TurnContext -- a throwaway state stub only carries usage. NOT run_turn."""
        return self._complete(prompt, model=model)

    def _complete(self, prompt: ModelInput, *, model: str, options=None) -> str:
        state = SimpleNamespace(timing={}, response_id=None, completion_options=options)
        if self.prefers_chat_completions():
            return self.complete_chat(model, prompt, state)
        response = self.create_responses(model=model, input=prompt, state=state)
        _record_usage(state, response)
        self._check_completion(response, state, 'responses')
        return str(_get_attr(response, "output_text", "") or "")

    # ------------------------------------------------------------------ #
    # TextModel v2 (OO migration Phase 6a, spica/ports/model.py). Thin over
    # the v1 methods so the endpoint/fallback logic keeps a single home (fix
    # a bug once): complete() reuses complete_text()'s Responses/Chat branch;
    # stream() assembles the request dict HERE (the depth v1 lacks) and
    # reuses iter_response_text's fallback tree. No new I/O branches.
    # ------------------------------------------------------------------ #

    def complete(self, prompt: ModelInput, *, model: str, options=None) -> str:
        return self._complete(prompt, model=model, options=options)

    def stream(self, prompt: ModelInput, *, model: str, state: Any) -> Iterator[str]:
        request = getattr(state, "request", None)
        effective_reasoning_effort = self._reasoning_effort
        if (
            getattr(request, "interaction_mode", None) == "system"
            and self._system_turn_reasoning_effort is not None
        ):
            effective_reasoning_effort = self._system_turn_reasoning_effort
        return _iter_response_text(
            self.client,
            {"model": model, "input": prompt},
            state,
            effective_reasoning_effort,
        )

    # ------------------------------------------------------------------ #
    # ToolCallingModel v2 (Phase 7-c2). Same single-home rule as complete/
    # stream: the endpoint family branch and the provider response parsing
    # live HERE; the runtime only sees ToolProbeResult / ToolProbeStream.
    # Usage accounting (no-double ruling): the chat family records inside
    # create_chat_with_tools via _record_usage(state, ...) -> usage=None on
    # the result; the Responses family returns response.usage so the runtime
    # observer records it (today's tool_round semantics, unchanged).
    # ------------------------------------------------------------------ #

    def probe(
        self, prompt: ModelInput, tools: list[dict[str, Any]], *, model: str, state: Any
    ) -> ToolProbeResult:
        if self.prefers_chat_completions():
            calls, text = self.create_chat_with_tools(
                model=model, prompt=prompt, tools=tools, state=state
            )
            return ToolProbeResult(calls=calls, text=text)
        response = self.create_responses(
            model=model, input=prompt, tools=tools, state=state, **_instructions_kwargs(state)
        )
        function_calls = [
            item for item in list(_get_attr(response, "output", []) or [])
            if _get_attr(item, "type") == "function_call"
        ]
        calls = [
            {
                "name": str(_get_attr(item, "name", "")),
                **({"id": str(_get_attr(item, "call_id"))} if _get_attr(item, "call_id") else {}),
                "arguments": str(_get_attr(item, "arguments", "") or "{}"),
            }
            for item in function_calls
        ]
        return ToolProbeResult(
            calls=calls,
            text=str(_get_attr(response, "output_text", "") or ""),
            response_id=str(_get_attr(response, "id", "") or "") or None,
            usage=_get_attr(response, "usage"),
            response_items=self.response_items(response),
        )

    def probe_stream(
        self, prompt: ModelInput, tools: list[dict[str, Any]], *, model: str, state: Any
    ) -> ToolProbeStream | None:
        if not self.prefers_chat_completions():
            return None  # Responses family: probes do not stream (family signal)
        # LAZY by construction: iter_chat_with_tools is a generator function
        # (calling it does no I/O) and ToolProbeStream only pulls it when
        # .deltas is iterated -- the client stream opens at first consumption.
        return ToolProbeStream(
            lambda sink: self.iter_chat_with_tools(
                model=model, prompt=prompt, tools=tools, state=state, tool_calls_sink=sink
            )
        )


# --------------------------------------------------------------------------- #
# Moved verbatim from agent/streaming_pipeline.py (Phase 5). Behaviour-identical.
# --------------------------------------------------------------------------- #

def _request(create, *, state, api, **kwargs):
    timeout = getattr(getattr(state, 'request', None), 'model_request_timeout_seconds', None)
    if timeout is not None:
        kwargs['timeout'] = timeout
    return _measured_request(create, state=state, api=api, **kwargs)


def _check_turn_context(state: Any) -> None:
    cancelled = getattr(getattr(state, 'request', None), 'cancelled', None)
    if cancelled is not None and cancelled.is_set():
        raise CancelledError('turn cancelled before model request')
    check = getattr(state, 'before_model_request', None)
    if callable(check):
        check(state)


def _client_for_turn(client: Any, state: Any) -> Any:
    _check_turn_context(state)
    # SDK retries cannot recheck a turn's selected sources between HTTP calls.
    # Independent summaries/organizers retain their configured retry policy.
    if callable(getattr(state, 'before_model_request', None)):
        return _client_with_retry_disabled(client, state)
    return client


def _instructions_kwargs(state: Any) -> dict[str, str]:
    instructions = getattr(getattr(state, "prompt", None), "model_instructions", "")
    return {"instructions": instructions} if instructions else {}


def _call_continuation(item: Any, message: Any) -> dict[str, str]:
    fields = {"id": _get_attr(item, "id"),
              "reasoning_content": _get_attr(message, "reasoning_content"),
              "assistant_text": _get_attr(message, "content")}
    return {key: str(value) for key, value in fields.items() if value}


def _provider_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, list):
        return [_provider_value(item) for item in value]
    if not isinstance(value, dict) and hasattr(value, "__dict__"):
        value = vars(value)
    if isinstance(value, dict):
        return {key: _provider_value(item) for key, item in value.items()}
    return value


def _responses_input(prompt: ModelInput) -> str | list[dict[str, Any]]:
    """Translate messages to Responses items without changing source roles."""
    if isinstance(prompt, str):
        return prompt
    items: list[dict[str, Any]] = []
    for message in prompt:
        if message.get("response_items"):
            items.extend(deepcopy(message["response_items"]))
            continue
        if message["role"] == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                          "output": message["content"]})
            continue
        if message.get("content"):
            items.append({"role": message["role"], "content": message["content"]})
        for call in message.get("tool_calls", []):
            items.append({"type": "function_call", "call_id": call["id"],
                          "name": call["function"]["name"],
                          "arguments": call["function"]["arguments"]})
    return items


def _chat_messages(prompt: ModelInput, state: Any) -> list[dict[str, Any]]:
    instructions = _instructions_kwargs(state).get("instructions")
    messages = deepcopy(prompt) if isinstance(prompt, list) else [{"role": "user", "content": prompt}]
    for message in messages:
        # Responses-only continuation travels on the canonical message, but
        # Chat uses that message's visible content and matched tool calls.
        message.pop("response_items", None)
    if instructions:
        messages.insert(0, {"role": "system", "content": instructions})
    return messages


def _iter_response_text(
    client: Any,
    request: dict[str, Any],
    state: Any,
    reasoning_effort: str = "default",
) -> Iterator[str]:
    request = {**request, **_instructions_kwargs(state)}
    llm_client = _client_with_retry_disabled(client, state)
    if _prefers_chat_completions(llm_client):
        state.timing["llm_stream_fallback_used"] = True
        state.timing["llm_stream_fallback_reason"] = "chat_completions_compatible_client"
        yield from _iter_chat_completion_text(llm_client, request, state, reasoning_effort=reasoning_effort)
        return

    stream_request = dict(request)
    stream_request["input"] = _responses_input(stream_request.get("input", ""))
    stream_request["stream"] = True
    stream_request.update(_reasoning_responses_kwargs(request.get("model"), reasoning_effort))
    stream_create_start_ms = now_ms()
    streamed_text = ""
    _check_turn_context(state)
    try:
        stream = _request(llm_client.responses.create, state=state, api='responses', **stream_request)
        state.timing["llm_stream_create_ms"] = elapsed_ms(stream_create_start_ms)
        log_timing(
            "llm_stream_create",
            state.timing["llm_stream_create_ms"],
            model=request.get("model"),
            max_retries=state.timing.get("llm_stream_max_retries"),
            retry_disabled=state.timing.get("llm_stream_retry_disabled"),
        )
    except TypeError as exc:
        state.timing["llm_stream_create_ms"] = elapsed_ms(stream_create_start_ms)
        state.timing["llm_stream_fallback_used"] = True
        state.timing["llm_stream_fallback_reason"] = "stream_request_type_error"
        state.timing["llm_stream_error"] = str(exc)
        yield _fallback_response_text(llm_client, request, state, streamed_text, reasoning_effort)
        return
    except Exception as exc:
        state.timing["llm_stream_create_ms"] = elapsed_ms(stream_create_start_ms)
        state.timing["llm_stream_fallback_used"] = True
        state.timing["llm_stream_fallback_reason"] = "stream_create_error"
        state.timing["llm_stream_error"] = str(exc)
        log_timing(
            "llm_stream_fallback",
            state.timing["llm_stream_create_ms"],
            phase="create",
            model=request.get("model"),
            error=str(exc),
        )
        if _is_responses_api_not_found(exc) and _has_chat_completions(llm_client):
            state.timing["llm_stream_fallback_reason"] = "responses_api_not_found_chat_completions"
            yield from _iter_chat_completion_text(
                llm_client, request, state, streamed_text, reasoning_effort=reasoning_effort)
            return
        yield _fallback_response_text(llm_client, request, state, streamed_text, reasoning_effort)
        return

    if hasattr(stream, "output_text"):
        state.timing["llm_stream_fallback_used"] = True
        state.timing["llm_stream_fallback_reason"] = "non_stream_response"
        _record_usage(state, stream)
        yield str(_get_attr(stream, "output_text", "") or "")
        return

    state.timing["llm_stream_fallback_used"] = False
    stream_error = None
    try:
        for event in stream:
            event_type = str(_get_attr(event, "type", "") or "")
            if event_type == "response.output_text.delta":
                delta = str(_get_attr(event, "delta", "") or "")
                streamed_text += delta
                yield delta
            elif event_type == "response.completed":
                response = _get_attr(event, "response")
                if response is not None:
                    _record_usage(state, response)
                    state.response_id = str(_get_attr(response, "id", "") or "") or state.response_id
            elif event_type == "response.failed":
                error = _get_attr(event, "error")
                raise RuntimeError(str(error or "LLM streaming failed."))
    except Exception as exc:
        stream_error = str(exc)
    finally:
        # Close before fallback. A close failure must escape this generator,
        # never turn GeneratorExit/cancellation into a new model request.
        _close_stream(stream)
    if stream_error is not None:
        state.timing["llm_stream_fallback_used"] = True
        state.timing["llm_stream_fallback_reason"] = "stream_iteration_error"
        state.timing["llm_stream_error"] = stream_error
        log_timing(
            "llm_stream_fallback",
            elapsed_ms(stream_create_start_ms),
            phase="iteration",
            model=request.get("model"),
            streamed_chars=len(streamed_text),
            error=stream_error,
        )
        yield _fallback_response_text(llm_client, request, state, streamed_text, reasoning_effort)


def _client_with_retry_disabled(client: Any, state: Any) -> Any:
    state.timing["llm_stream_max_retries"] = 0
    if not hasattr(client, "with_options"):
        state.timing["llm_stream_retry_disabled"] = False
        return client
    try:
        retry_disabled_client = client.with_options(max_retries=0)
    except TypeError:
        state.timing["llm_stream_retry_disabled"] = False
        return client
    state.timing["llm_stream_retry_disabled"] = True
    return retry_disabled_client


def _fallback_response_text(
    client: Any,
    request: dict[str, Any],
    state: Any,
    already_streamed: str = "",
    reasoning_effort: str = "default",
) -> str:
    if _prefers_chat_completions(client):
        return "".join(_iter_chat_completion_text(
            client, request, state, already_streamed, reasoning_effort=reasoning_effort))

    fallback_request = {key: value for key, value in request.items() if key != "stream"}
    fallback_request["input"] = _responses_input(fallback_request.get("input", ""))
    fallback_request.update(_reasoning_responses_kwargs(fallback_request.get("model"), reasoning_effort))
    fallback_start_ms = now_ms()
    _check_turn_context(state)
    response = _request(client.responses.create, state=state, api='responses', **fallback_request)
    fallback_ms = elapsed_ms(fallback_start_ms)
    state.timing["llm_fallback_response_ms"] = fallback_ms
    _record_usage(state, response)
    fallback_text = str(_get_attr(response, "output_text", "") or "")
    log_timing(
        "llm_stream_fallback_response",
        fallback_ms,
        model=fallback_request.get("model"),
        fallback_chars=len(fallback_text),
        already_streamed_chars=len(already_streamed),
    )
    if already_streamed and fallback_text.startswith(already_streamed):
        return fallback_text[len(already_streamed):]
    return fallback_text


def _prefers_chat_completions(client: Any) -> bool:
    base_url = str(_get_attr(client, "base_url", "") or "").lower()
    return "deepseek" in base_url and _has_chat_completions(client)


def _has_chat_completions(client: Any) -> bool:
    chat = _get_attr(client, "chat")
    completions = _get_attr(chat, "completions") if chat is not None else None
    return completions is not None and hasattr(completions, "create")


def _is_responses_api_not_found(exc: Exception) -> bool:
    status_code = _get_attr(exc, "status_code")
    if status_code == 404:
        return True
    return "404" in str(exc)


def _iter_chat_completion_text(
    client: Any,
    request: dict[str, Any],
    state: Any,
    already_streamed: str = "",
    reasoning_effort: str = "default",
) -> Iterator[str]:
    chat_request = {
        "model": request.get("model"),
        "messages": _chat_messages(request.get("input") or "", state),
        "stream": True,
        **_reasoning_chat_kwargs(request.get("model"), reasoning_effort),
    }
    if (_model_is_deepseek(request.get("model"))
            and getattr(getattr(state, "request", None), "interaction_mode", None) == "system"):
        # Proactive dialogue already supplies the JSON contract and no tools.
        # Ask the provider to enforce it instead of relying only on prompting.
        chat_request["response_format"] = {"type": "json_object"}
    chat_start_ms = now_ms()
    full_text = ""
    _check_turn_context(state)
    stream = None
    try:
        stream = _request(client.chat.completions.create, state=state, api='chat', **chat_request)
        state.timing["llm_chat_stream_create_ms"] = elapsed_ms(chat_start_ms)
        state.timing["llm_chat_completions_fallback_used"] = True
        log_timing(
            "llm_chat_stream_create",
            state.timing["llm_chat_stream_create_ms"],
            model=chat_request.get("model"),
        )
        for chunk in stream:
            choices = list(_get_attr(chunk, "choices", []) or [])
            if not choices:
                continue
            delta = _get_attr(choices[0], "delta")
            content = str(_get_attr(delta, "content", "") or "")
            if not content:
                continue
            full_text += content
            yield content
        if not full_text.strip():
            # A provider can finish after reasoning-only chunks. Those are not
            # an answer and must never be spoken or silently parsed as an apology.
            # Reuse the existing single fallback; healthy streams add no calls.
            raise RuntimeError("LLM returned no answer content (empty chat stream).")
        return
    except Exception as exc:
        state.timing["llm_chat_stream_error"] = str(exc)
        log_timing(
            "llm_chat_stream_error",
            elapsed_ms(chat_start_ms),
            model=chat_request.get("model"),
            error=str(exc),
        )
    finally:
        _close_stream(stream)

    # Review #3 (AABC fix): the dedupe baseline must include what THIS stream
    # already yielded -- on the chat-first path ``already_streamed`` is "" and
    # the locally streamed prefix was never stripped, so a stream dying after
    # "A" plus a fallback answering "ABC" played "AABC" in UI/TTS/memory.
    streamed = already_streamed + full_text
    fallback_request = dict(chat_request)
    fallback_request["stream"] = False
    _check_turn_context(state)
    response = _request(client.chat.completions.create, state=state, api='chat', **fallback_request)
    choices = list(_get_attr(response, "choices", []) or [])
    if choices:
        message = _get_attr(choices[0], "message")
        full_text = str(_get_attr(message, "content", "") or "")
    _record_usage(state, response)
    if not full_text.strip():
        raise RuntimeError("LLM returned no answer content after stream fallback.")
    if streamed and full_text.startswith(streamed):
        yield full_text[len(streamed):]
    else:
        # Registered edge: a non-deterministic re-answer whose prefix differs
        # from what already streamed cannot be deduped -- keep the historical
        # whole-text yield (we cannot un-say the streamed prefix).
        yield full_text


def _close_stream(stream: Any) -> None:
    # The SDK Stream owns a generator cycle, so dropping its reference does not
    # release HTTP/SSE promptly. Only the consuming generator closes it here.
    close = getattr(stream, "close", None)
    if callable(close):
        close()


def _get_attr(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)
