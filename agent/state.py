from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class AgentState:
    conversation_id: str
    user_input: str
    emotion_override: str | None = None
    tts_param_overrides: dict[str, Any] | None = None
    visual_overrides: dict[str, Any] = field(default_factory=dict)
    recent_context: list[dict[str, str]] = field(default_factory=list)
    long_term_memories: list[dict[str, Any]] = field(default_factory=list)
    prompt_input: str | list[Any] | dict[str, Any] | None = None
    raw_model_output: str | None = None
    response_id: str | None = None
    parsed_reply: dict[str, str] | None = None
    answer: str | None = None
    emotion: str | None = None
    visual: dict[str, Any] | None = None
    tts_result: dict[str, Any] | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    timing: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    response_payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentServices:
    llm_client: Any
    tts_tool: Any | None
    visual_tool: Any | None
    memory_store: Any
    recent_memory: Any
    config: dict[str, Any]
    logger: Callable[..., None] | None = None
    tool_functions: dict[str, Callable[..., str]] = field(default_factory=dict)
    tool_schemas: list[dict[str, Any]] = field(default_factory=list)
