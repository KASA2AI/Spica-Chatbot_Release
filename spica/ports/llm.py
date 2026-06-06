"""LLM capability port (Phase 5).

A character-agnostic seam over the chat model. Adapters encapsulate client
construction and the OpenAI-Responses vs Chat-Completions (e.g. DeepSeek) branch,
exposing a uniform surface so the pipeline never special-cases a provider.

``state`` is passed as ``Any`` to avoid a spica -> agent import; adapters only use
its ``timing`` dict / ``response_id`` / ``raw_model_output`` attributes.
"""

from __future__ import annotations

from typing import Any, Iterator, Protocol, runtime_checkable


@runtime_checkable
class LLMPort(Protocol):
    name: str

    def prefers_chat_completions(self) -> bool:
        """True when the backing client should use Chat Completions, not Responses."""
        ...

    def iter_response_text(self, request: dict[str, Any], state: Any) -> Iterator[str]:
        """Stream assistant text deltas for ``request`` (handles all fallbacks)."""
        ...

    def create_responses(self, **request: Any) -> Any:
        """One-shot Responses API call (used by the synchronous tool loop)."""
        ...

    def complete_chat(self, model: str, prompt: str, state: Any) -> str:
        """One-shot Chat Completions call returning the assistant text."""
        ...
