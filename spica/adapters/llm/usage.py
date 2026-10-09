"""Usage receipts at the provider request boundary, including stream cleanup.

Only identifiers and provider-reported counters reach the existing log sink.
An interrupted response without usage is unknown, never a zero-cost request.
"""
from __future__ import annotations

import json
import logging
import time
from uuid import uuid4

logger = logging.getLogger('spica.llm.usage')
# Native/model libraries may lower the root level after service startup.
# Keep these small accounting receipts visible through the existing handlers.
logger.setLevel(logging.INFO)


def _get(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _counts(response):
    usage = _get(response, 'usage')
    if usage is None:
        return {}
    input_tokens = _count(_get(usage, 'input_tokens', _get(usage, 'prompt_tokens')))
    output_tokens = _count(_get(usage, 'output_tokens', _get(usage, 'completion_tokens')))
    input_details = _get(usage, 'input_tokens_details', _get(usage, 'prompt_tokens_details'))
    output_details = _get(usage, 'output_tokens_details', _get(usage, 'completion_tokens_details'))
    hit = _count(_get(usage, 'prompt_cache_hit_tokens', _get(input_details, 'cached_tokens')))
    miss = _count(_get(usage, 'prompt_cache_miss_tokens'))
    if miss is None and input_tokens is not None and hit is not None and hit <= input_tokens:
        miss = input_tokens - hit
    return dict(input_tokens=input_tokens, output_tokens=output_tokens,
        total_tokens=_count(_get(usage, 'total_tokens')),
        cache_hit_tokens=hit, cache_miss_tokens=miss,
        reasoning_tokens=_count(_get(output_details, 'reasoning_tokens')))


def record_usage(state, response):
    if state is not None:
        state.timing.update({key: value for key, value in _counts(response).items() if value is not None})


class _Receipt:
    def __init__(self, state, api, request):
        turn = getattr(state, 'request', None)
        self.state, self.started, self.finished = state, time.monotonic(), False
        self.values = dict(request_id=uuid4().hex, model=request.get('model'), api=api,
            stream=bool(request.get('stream')), source=getattr(turn, 'input_source', '') or 'background',
            turn_id=getattr(turn, 'evidence_turn_id', None), provider_response_id=None,
            input_tokens=None, output_tokens=None, total_tokens=None,
            cache_hit_tokens=None, cache_miss_tokens=None, reasoning_tokens=None)
        attribution = getattr(getattr(state, 'completion_options', None), 'usage', None)
        if attribution is not None:
            self.values.update(stage=attribution.stage, batch_id=attribution.batch_id, attempt=attribution.attempt)

    def capture(self, response):
        response = _get(response, 'response') or response
        identifier = _get(response, 'id')
        if identifier:
            self.values['provider_response_id'] = str(identifier)
        self.values.update({key: value for key, value in _counts(response).items() if value is not None})
        choices = _get(response, 'choices') or []
        termination = _get(choices[0], 'finish_reason') if choices else _get(response, 'status')
        if termination in {'stop', 'length', 'content_filter', 'tool_calls', 'insufficient_system_resource',
                           'aborted', 'completed', 'incomplete', 'failed', 'in_progress'}:
            self.values['termination'] = termination
        record_usage(self.state, response)

    def finish(self, status, error=None):
        if self.finished:
            return
        self.finished = True
        self.values.update(status=status, usage_known=all(self.values[key] is not None
            for key in ('input_tokens', 'output_tokens')), elapsed_ms=round((time.monotonic()-self.started)*1000, 2))
        if error is not None:
            self.values['error_type'] = type(error).__name__
        try:
            logger.info('event=llm_usage %s', json.dumps(self.values, ensure_ascii=False))
        except Exception:
            pass  # Accounting must not interfere with reply/resource cleanup.


class _UsageStream:
    def __init__(self, stream, receipt):
        self.stream, self.receipt, self.iterator = stream, receipt, None

    def __iter__(self):
        return self

    def __next__(self):
        try:
            if self.iterator is None:
                self.iterator = iter(self.stream)
            chunk = next(self.iterator)
            self.receipt.capture(chunk)
            return chunk
        except StopIteration:
            self.receipt.finish('completed')
            raise
        except BaseException as exc:
            self.receipt.finish('error', exc)
            raise

    def close(self):
        try:
            close = getattr(self.stream, 'close', None)
            if callable(close):
                close()
        finally:
            self.receipt.finish('cancelled')


def request(create, *, state, api, **kwargs):
    # Opt in for the deployed DeepSeek family; other compatible servers keep
    # their existing request contract. Always consume usage when returned.
    if api == 'chat' and kwargs.get('stream') and str(kwargs.get('model', '')).lower().startswith('deepseek'):
        kwargs['stream_options'] = {**kwargs.get('stream_options', {}), 'include_usage': True}
    receipt = _Receipt(state, api, kwargs)
    try:
        response = create(**kwargs)
    except BaseException as exc:
        receipt.finish('error', exc)
        raise
    if kwargs.get('stream') and _get(response, 'choices') is None and _get(response, 'output_text') is None:
        return _UsageStream(response, receipt)
    receipt.capture(response)
    receipt.finish('completed')
    return response
