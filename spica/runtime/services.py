from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from agent_tools.tts.base import TTSAdapter

# AgentState (the per-turn blackboard) was dismantled in C3c -- the turn now runs
# on spica.runtime.context.TurnContext (typed per-stage sub-objects). AgentServices
# (the dependency container) stays until C4 flips services -> deps.


@dataclass
class AgentServices:
    llm_client: Any
    tts_adapter: TTSAdapter | None
    visual_tool: Any | None
    memory_store: Any
    recent_memory: Any
    config: dict[str, Any]
    logger: Callable[..., None] | None = None
    tool_functions: dict[str, Callable[..., str]] = field(default_factory=dict)
    tool_schemas: list[dict[str, Any]] = field(default_factory=list)
    # Phase 5: resolved capability adapters. When None, the pipeline lazily wraps
    # the raw llm_client / memory_store, so callers passing only the legacy fields
    # keep working unchanged.
    llm_adapter: Any | None = None
    memory_adapter: Any | None = None
    # C7: the host's CapabilityRegistry (set after built-in tools register). When
    # present the turn resolves tools from it (registry-backed ToolSet); tests leave
    # it None and the ToolSet adapts tool_schemas / tool_functions instead.
    tool_registry: Any = None
