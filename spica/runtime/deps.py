"""Typed turn dependencies (core C3a).

``TurnDeps`` is the typed bundle a turn runs against: the validated ``AppConfig``,
the resolved capability ports (llm / tts / visual / memory), the ``ToolSet``, and
the injected policies (observer / jobs / exec). C3a stands this up and has the
runtime call tools through ``deps.tools``; the ports/config get used as the later
stages retire ``services.config.get`` (C3b) and the ``AgentState`` blackboard
(C3c). ``observer`` / ``jobs`` / ``exec_strategy`` are non-None *placeholders* now
so C5 / C6 are a clean "swap the implementation", not "delete the None checks".

Built by ``TurnDeps.from_services`` from the host-assembled ``AgentServices`` (the
host resolves the ports; this just maps them into the typed bundle -- kept here in
runtime rather than the host so ``spica/core`` doesn't import ``spica/host``).

Pure: no ``agent`` import, Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from spica.adapters.llm import OpenAICompatibleAdapter
from spica.adapters.memory import SqliteMemoryAdapter
from spica.config.schema import (
    AppConfig,
    CharacterConfig,
    LLMConfig,
    MemoryConfig,
    StreamConfig,
)
from spica.ports.game_memory import GameMemoryPort
from spica.ports.llm import LLMPort
from spica.ports.memory import MemoryPort
from spica.ports.tts import TTSPort
from spica.ports.visual import VisualPort
from spica.runtime.exec_strategy import ExecStrategy, Inline
from spica.runtime.jobs import InlineJobRunner
from spica.runtime.observer import NoopTurnObserver
from spica.runtime.tools import RegistryToolSet, ToolSet


@dataclass(frozen=True)
class TurnDeps:
    config: AppConfig
    llm: LLMPort | None
    tts: TTSPort | None
    visual: VisualPort | None
    memory: MemoryPort | None
    tools: ToolSet
    # galgame committed-data read/write port (Phase 3). Defaults None: legacy/test
    # callers and every plain chat turn never touch it (the gated stage's `none`
    # branch returns before reading it).
    game_memory: GameMemoryPort | None = None
    # Non-None by construction (C3a placeholders -> real impls in C5/C6).
    observer: Any = field(default_factory=NoopTurnObserver)
    jobs: Any = field(default_factory=InlineJobRunner)
    # Placeholder: per-turn concurrency still flows via run_turn's exec_strategy
    # param in C3a; a later stage makes this the source of truth.
    exec_strategy: ExecStrategy = field(default_factory=Inline)
    # OO migration Phase 3: prompt-context contributors consumed by
    # stages.contribute_context_node. None -> the galgame COMPATIBILITY auto-fill
    # below (a shim that never grows a second entry); explicit () = injection
    # off; future domains must register their FULL tuple explicitly via assembly
    # (including the galgame contributor), never rely on the auto-fill.
    context_contributors: tuple[Any, ...] | None = None

    def __post_init__(self) -> None:
        if self.context_contributors is None:
            # Function-level lazy import: deps.py keeps ZERO module-level galgame
            # edges (the cycle risk), and every direct TurnDeps(...) test
            # construction keeps working unchanged (the P1-1 auto-fill ruling).
            # NOT conditioned on game_memory: a missing port is handled by the
            # contributor's sections() no-op, which preserves today's span/timing
            # semantics for active turns (Phase 0 golden #2(d)).
            from spica.galgame.context_contributor import galgame_contributor

            object.__setattr__(self, "context_contributors", (galgame_contributor,))

    @classmethod
    def from_services(cls, services: Any, app_config: AppConfig) -> "TurnDeps":
        """Map host-assembled AgentServices (resolved ports) into typed deps.

        The ``or Adapter(...)`` is the ONE place that resolves a raw client/store
        into a port -- in production the host already resolved them (the ``or``
        takes the first branch); for legacy callers passing only a raw client it
        wraps. This keeps the dual-field fallback out of the runtime hot path.
        """
        return cls(
            config=app_config,
            llm=services.llm_adapter or OpenAICompatibleAdapter(services.llm_client),
            tts=services.tts_adapter,
            visual=services.visual_tool,
            memory=services.memory_adapter
            or SqliteMemoryAdapter(services.memory_store, services.recent_memory),
            # C7: registry-backed ToolSet. Host sets services.tool_registry (ToolPort
            # tools incl. inspect_screen); tests leave it None -> adapt the legacy
            # services tool table so injected fakes still work, golden unchanged.
            tools=(
                RegistryToolSet(services.tool_registry)
                if getattr(services, "tool_registry", None) is not None
                else RegistryToolSet.from_function_table(services.tool_schemas, services.tool_functions)
            ),
            # Host sets services.game_memory_adapter (Phase 3); legacy/test services
            # without it map to None (the gated stage then injects nothing).
            game_memory=getattr(services, "game_memory_adapter", None),
        )

    @classmethod
    def from_legacy_services(cls, services: Any) -> "TurnDeps":
        """Bridge a legacy dict-config services bundle into typed deps.

        Direct ``stream_voice_events`` callers (tests) and the compat sync path
        carry config as ``services.config`` (a dict), not an ``AppConfig``. This
        reverse-maps that dict so the runtime can run on typed deps. It is the one
        place allowed to read the legacy config dict (C3b); the StreamConfig
        defaults match the historical ``or N`` fallbacks exactly.
        """
        cfg = services.config
        app_config = AppConfig(
            llm=LLMConfig(model=cfg.get("model") or "gpt-4.1-mini"),
            memory=MemoryConfig(
                # Defaults match the stages' historical ``get(key, default)`` exactly,
                # so bridging a dict that omits a key reproduces today's behaviour (C4).
                recent_context_limit=int(cfg.get("recent_context_limit", 3)),
                long_term_memory_limit=int(cfg.get("long_term_memory_limit", 5)),
                long_term_memory_budget_chars=int(cfg.get("long_term_memory_budget_chars", 1200)),
                recent_turn_char_limit=int(cfg.get("recent_turn_char_limit", 360)),
                max_long_term_memories=int(cfg.get("max_long_term_memories", 200)),
            ),
            character=CharacterConfig(
                interlocutor_name=cfg.get("interlocutor_name"),
                character_id=cfg.get("character_id"),
                character_profile=cfg.get("character_profile"),
                character_name=cfg.get("character_name"),
            ),
            stream=StreamConfig(
                play_unit_min_chars=int(cfg.get("play_unit_min_chars") or 18),
                play_unit_max_chars=int(cfg.get("play_unit_max_chars") or 96),
                visual_stream_workers=int(cfg.get("visual_stream_workers") or 2),
            ),
            max_tool_rounds=int(cfg.get("max_tool_rounds", 3)),
        )
        return cls.from_services(services, app_config)
