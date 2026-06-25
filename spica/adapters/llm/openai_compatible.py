"""OpenAI-compatible LLM adapter (Phase 5).

Encapsulates the chat-model client I/O and the OpenAI-Responses vs
Chat-Completions (e.g. DeepSeek) branch + streaming fallbacks that previously
lived inline in ``agent/streaming_pipeline.py`` and ``agent/nodes.py``. The
module-level functions below are moved verbatim from the streaming pipeline
(zero behaviour change); the thin ``OpenAICompatibleAdapter`` binds them to a
client so the pipeline can depend on ``LLMPort`` instead of a raw client.

``state`` is typed ``Any`` to avoid a spica -> agent import; only its ``timing``
dict / ``response_id`` attributes are touched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Iterator

from common.timing import elapsed_ms, log_timing, now_ms


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


class OpenAICompatibleAdapter:
    """LLM adapter over an OpenAI-compatible client (OpenAI, DeepSeek, ...)."""

    name = "openai_compatible"

    def __init__(self, client: Any) -> None:
        self.client = client

    def prefers_chat_completions(self) -> bool:
        return _prefers_chat_completions(self.client)

    def has_chat_completions(self) -> bool:
        return _has_chat_completions(self.client)

    def create_responses(self, **request: Any) -> Any:
        """One-shot Responses API call (synchronous tool loop / probe)."""
        return self.client.responses.create(**request)

    def complete_chat(self, model: str, prompt: str, state: Any) -> str:
        """One-shot Chat Completions call, returning the assistant text."""
        response = self.client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
        )
        _record_usage(state, response)
        choices = list(_get_attr(response, "choices", []) or [])
        if choices:
            message = _get_attr(choices[0], "message")
            return str(_get_attr(message, "content", "") or "")
        return ""

    def create_chat_with_tools(
        self,
        *,
        model: str,
        prompt: str,
        tools: list[dict[str, Any]],
        state: Any,
    ) -> tuple[list[dict[str, str]], str]:
        """One-shot Chat Completions tool probe (the chat-path counterpart of the
        Responses probe). Sends ``tools`` in the nested chat format and returns
        ``(tool_calls, text)`` where each tool_call is ``{"name", "arguments"}``
        (arguments = raw JSON string) and ``text`` is the assistant content."""
        probe_start_ms = now_ms()
        response = self.client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            tools=to_chat_completions_tools(tools),
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
                        "arguments": str(_get_attr(function, "arguments", "") or "{}"),
                    }
                )
        return calls, text

    def iter_chat_with_tools(
        self,
        *,
        model: str,
        prompt: str,
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

        Single-worker / serial use only (one turn streams at a time). No usage
        recording in the loop (mirrors ``_iter_chat_completion_text``)."""
        probe_start_ms = now_ms()
        stream = self.client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            tools=to_chat_completions_tools(tools),
            stream=True,
        )
        acc: dict[int, dict[str, str]] = {}
        for chunk in stream:
            choices = list(_get_attr(chunk, "choices", []) or [])
            if not choices:
                continue
            delta = _get_attr(choices[0], "delta")
            content = str(_get_attr(delta, "content", "") or "")
            if content:
                yield content
            for item in list(_get_attr(delta, "tool_calls", []) or []):
                index = int(_get_attr(item, "index", 0) or 0)
                slot = acc.setdefault(index, {"name": "", "arguments": ""})
                function = _get_attr(item, "function")
                name = str(_get_attr(function, "name", "") or "")
                if name:
                    slot["name"] = name
                arguments = str(_get_attr(function, "arguments", "") or "")
                if arguments:
                    slot["arguments"] += arguments
        log_timing("llm_chat_tool_probe", elapsed_ms(probe_start_ms), model=model, streamed=True)
        for index in sorted(acc):
            if acc[index]["name"]:
                tool_calls_sink.append(
                    {"name": acc[index]["name"], "arguments": acc[index]["arguments"] or "{}"}
                )

    def iter_response_text(self, request: dict[str, Any], state: Any) -> Iterator[str]:
        """Stream assistant text deltas, with all fallbacks handled internally."""
        return _iter_response_text(self.client, request, state)

    def complete_text(self, prompt: str, *, model: str) -> str:
        """One-shot, turn-independent completion (Phase 8 summarization). Reuses the
        same endpoint/branch logic as the dialogue path but non-streaming and without
        a TurnContext -- a throwaway state stub only carries usage. NOT run_turn."""
        state = SimpleNamespace(timing={}, response_id=None)
        if self.prefers_chat_completions():
            return self.complete_chat(model, prompt, state)
        response = self.create_responses(model=model, input=prompt)
        _record_usage(state, response)
        return str(_get_attr(response, "output_text", "") or "")


# --------------------------------------------------------------------------- #
# Moved verbatim from agent/streaming_pipeline.py (Phase 5). Behaviour-identical.
# --------------------------------------------------------------------------- #

def _iter_response_text(
    client: Any,
    request: dict[str, Any],
    state: Any,
) -> Iterator[str]:
    llm_client = _client_with_retry_disabled(client, state)
    if _prefers_chat_completions(llm_client):
        state.timing["llm_stream_fallback_used"] = True
        state.timing["llm_stream_fallback_reason"] = "chat_completions_compatible_client"
        yield from _iter_chat_completion_text(llm_client, request, state)
        return

    stream_request = dict(request)
    stream_request["stream"] = True
    stream_create_start_ms = now_ms()
    streamed_text = ""
    try:
        stream = llm_client.responses.create(**stream_request)
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
        yield _fallback_response_text(llm_client, request, state, streamed_text)
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
            yield from _iter_chat_completion_text(llm_client, request, state, streamed_text)
            return
        yield _fallback_response_text(llm_client, request, state, streamed_text)
        return

    if hasattr(stream, "output_text"):
        state.timing["llm_stream_fallback_used"] = True
        state.timing["llm_stream_fallback_reason"] = "non_stream_response"
        _record_usage(state, stream)
        yield str(_get_attr(stream, "output_text", "") or "")
        return

    state.timing["llm_stream_fallback_used"] = False
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
        state.timing["llm_stream_fallback_used"] = True
        state.timing["llm_stream_fallback_reason"] = "stream_iteration_error"
        state.timing["llm_stream_error"] = str(exc)
        log_timing(
            "llm_stream_fallback",
            elapsed_ms(stream_create_start_ms),
            phase="iteration",
            model=request.get("model"),
            streamed_chars=len(streamed_text),
            error=str(exc),
        )
        yield _fallback_response_text(llm_client, request, state, streamed_text)


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
) -> str:
    if _prefers_chat_completions(client):
        return "".join(_iter_chat_completion_text(client, request, state, already_streamed))

    fallback_request = {key: value for key, value in request.items() if key != "stream"}
    fallback_start_ms = now_ms()
    response = client.responses.create(**fallback_request)
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
) -> Iterator[str]:
    chat_request = {
        "model": request.get("model"),
        "messages": [{"role": "user", "content": str(request.get("input") or "")}],
        "stream": True,
    }
    chat_start_ms = now_ms()
    full_text = ""
    try:
        stream = client.chat.completions.create(**chat_request)
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
        return
    except Exception as exc:
        state.timing["llm_chat_stream_error"] = str(exc)
        log_timing(
            "llm_chat_stream_error",
            elapsed_ms(chat_start_ms),
            model=chat_request.get("model"),
            error=str(exc),
        )

    # Review #3 (AABC fix): the dedupe baseline must include what THIS stream
    # already yielded -- on the chat-first path ``already_streamed`` is "" and
    # the locally streamed prefix was never stripped, so a stream dying after
    # "A" plus a fallback answering "ABC" played "AABC" in UI/TTS/memory.
    streamed = already_streamed + full_text
    fallback_request = dict(chat_request)
    fallback_request["stream"] = False
    response = client.chat.completions.create(**fallback_request)
    choices = list(_get_attr(response, "choices", []) or [])
    if choices:
        message = _get_attr(choices[0], "message")
        full_text = str(_get_attr(message, "content", "") or "")
    _record_usage(state, response)
    if streamed and full_text.startswith(streamed):
        yield full_text[len(streamed):]
    else:
        # Registered edge: a non-deterministic re-answer whose prefix differs
        # from what already streamed cannot be deduped -- keep the historical
        # whole-text yield (we cannot un-say the streamed prefix).
        yield full_text


def _record_usage(state: Any, response: Any) -> None:
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
