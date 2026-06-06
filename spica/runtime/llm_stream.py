"""LLM streaming access for the runtime (Phase 6C).

Thin layer over the Phase 5 LLM adapter: resolve the active adapter (or wrap the
raw client) and provide the small response helpers the runtime needs. The actual
streaming + DeepSeek/OpenAI branch lives in spica/adapters/llm. Qt-free.
"""

from __future__ import annotations

from typing import Any

from spica.adapters.llm import OpenAICompatibleAdapter


def llm_adapter(services: Any) -> OpenAICompatibleAdapter:
    """Resolve the active LLM adapter, falling back to wrapping the raw client."""
    return services.llm_adapter or OpenAICompatibleAdapter(services.llm_client)


def get_attr(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def record_usage(state: Any, response: Any) -> None:
    usage = get_attr(response, "usage")
    if not usage:
        return
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = get_attr(usage, key)
        if value is not None:
            state.timing[key] = value
