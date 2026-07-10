"""Built-in capability adapters (Phase 5).

The host registers these into the ``CapabilityRegistry`` at construction time;
``AppHost`` then resolves the active instance by the name in config (e.g.
``config.llm.provider``). Pulling the registration out of ``AppHost`` keeps the
host thin (CLAUDE.md #6) -- it stays pure wiring + forwarding while the catalogue
of what ships built-in lives here.

INVARIANT (CLAUDE.md #1): Qt-free -- only adapters / agent_tools, never any GUI.
"""

from __future__ import annotations

from spica.plugins.registry import CapabilityRegistry
from spica.adapters.llm import OpenAICompatibleAdapter
from spica.adapters.memory import SqliteMemoryAdapter
from spica.adapters.screen import LocalMoondreamScreenAnalysis
from spica.adapters.tools import InspectScreenTool
from spica.adapters.tts import build_tts
from spica.adapters.visual import build_spica_visual
from spica.runtime.stages import _compact_screen_tool_output
from agent_tools.tts import CURRENT_GPTSOVITS_PROVIDERS


def register_tts_providers(registry: CapabilityRegistry) -> None:
    """The TTS slice of the builtin catalogue, reusable on its own: "text_only"
    is the tts.enabled=false assembly (no model, ok results with no audio);
    "dummy" stays the test/demo placeholder. scripts/self_check.py's TTS worker
    registers ONLY this slice so an unrelated builtin's construction failure
    (screen tools etc.) can never read as a TTS failure."""
    for provider in (*CURRENT_GPTSOVITS_PROVIDERS, "dummy", "text_only"):
        registry.register_tts(
            provider, lambda config=None, service=None: build_tts(config, service)
        )


def register_builtin_adapters(registry: CapabilityRegistry, screen_config=None) -> None:
    """Register the built-in capability adapters by name (Phase 5).

    Resolving by the name in config (e.g. ``config.llm.provider``) is what makes
    "swap the engine by changing a config name" work; this is also the seam
    Phase 8 plugins register into.
    """
    registry.register_llm(
        "openai_compatible",
        lambda client=None, reasoning_effort="default": OpenAICompatibleAdapter(
            client, reasoning_effort=reasoning_effort
        ),
    )
    register_tts_providers(registry)
    registry.register_visual("spica_diff", build_spica_visual)
    registry.register_memory(
        "sqlite", lambda store=None, recent=None: SqliteMemoryAdapter(store, recent)
    )
    # C7: inspect_screen is the first real tool -- a ToolPort over the local
    # screen-analysis adapter, registered so the runtime resolves it via the
    # registry (not a static TOOL_SCHEMAS list). N0 gate + local-only preserved.
    # P1: it declares its HISTORICAL followup compactor (the same function the
    # frozen sync chain applies by name), so the streaming chain's generic
    # two-layer compaction reproduces the old special case byte for byte.
    # P0b 2a: the host resolves ScreenPipelineConfig ONCE and injects it; the
    # tool's config=None fallback stays for bare/demo construction only.
    screen_tool = InspectScreenTool(LocalMoondreamScreenAnalysis(), config=screen_config)
    registry.register_tool(
        screen_tool.schema(),
        screen_tool.run,
        compact_output=_compact_screen_tool_output,
        # screen.enabled=false -> not offered to the LLM at all (supply side);
        # the tool's run() re-checks BEFORE capturing (execution side). None
        # config (bare/demo construction) keeps the historical always-offered.
        available=None if screen_config is None else (lambda: bool(screen_config.enabled)),
    )
