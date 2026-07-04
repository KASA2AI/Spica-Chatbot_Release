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
    # Phase 3: galgame committed-memory adapter (GameMemoryPort). Host wires it;
    # tests/legacy callers leave it None -> deps.game_memory None -> gated stage
    # injects nothing.
    game_memory_adapter: Any | None = None
    # Phase 5: galgame launch + window-binding adapters. Used by GameBinder (NOT by
    # the turn), so they live on services + the host factory only -- never in deps.
    game_launcher_adapter: Any | None = None
    window_locator_adapter: Any | None = None
    # Phase 6: galgame screen-capture + OCR adapters. Used by the OCR calibrator
    # (NOT by the turn) -> services + host factory only, never in deps.
    screen_capture_adapter: Any | None = None
    ocr_adapter: Any | None = None
    # C7: the host's CapabilityRegistry (set after built-in tools register). When
    # present the turn resolves tools from it (registry-backed ToolSet); tests leave
    # it None and the ToolSet adapts tool_schemas / tool_functions instead.
    tool_registry: Any = None
    # W1 (WINDOWS_COMPAT_PLAN §3.6 / A8): the folded effective platform, the ONE
    # persistent home platform consumers read (host.services.effective_platform) --
    # never a second sys.platform read. The default is the tests/legacy-construction
    # value (same tail-field convention as above); the production path
    # (build_agent_services) always writes the fold_platform() result.
    effective_platform: str = "linux"
