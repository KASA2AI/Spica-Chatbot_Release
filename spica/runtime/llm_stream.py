"""LLM response helpers for the runtime (Phase 6C).

Small attribute / usage helpers the runtime needs around an LLM response. The
LLM port itself is resolved in spica.runtime.deps (C3b); the streaming +
DeepSeek/OpenAI branch lives in spica/adapters/llm. Qt-free.
"""

from __future__ import annotations

from typing import Any


def get_attr(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def record_usage(observer: Any, response: Any) -> None:
    # C5: writes the token counts through the injected TurnObserver (used by the
    # streaming tool round). The LLM adapter has its own internal _record_usage.
    usage = get_attr(response, "usage")
    if not usage:
        return
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = get_attr(usage, key)
        if value is not None:
            observer.mark(key, value)
