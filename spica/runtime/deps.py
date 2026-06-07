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

from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any

from spica.config.schema import AppConfig
from spica.ports.llm import LLMPort
from spica.ports.memory import MemoryPort
from spica.ports.tts import TTSPort
from spica.ports.visual import VisualPort
from spica.runtime.exec_strategy import ExecStrategy, Inline
from spica.runtime.tools import LegacyFunctionToolSet, ToolSet


class _NoopObserver:
    """Placeholder until C5's ``TurnObserver`` -- records nothing."""

    def span(self, name: str, **fields: Any) -> Any:
        return nullcontext()

    def mark(self, name: str, value: float | None = None, **fields: Any) -> None:
        return None

    def snapshot(self) -> dict[str, Any]:
        return {}


class _InlineJobs:
    """Placeholder until C6's ``InlineJobRunner`` -- runs jobs synchronously."""

    def submit(self, fn: Any) -> None:
        fn()

    def drain(self, timeout: float | None = None) -> None:
        return None


@dataclass(frozen=True)
class TurnDeps:
    config: AppConfig
    llm: LLMPort | None
    tts: TTSPort | None
    visual: VisualPort | None
    memory: MemoryPort | None
    tools: ToolSet
    # Non-None by construction (C3a placeholders -> real impls in C5/C6).
    observer: Any = field(default_factory=_NoopObserver)
    jobs: Any = field(default_factory=_InlineJobs)
    # Placeholder: per-turn concurrency still flows via run_turn's exec_strategy
    # param in C3a; a later stage makes this the source of truth.
    exec_strategy: ExecStrategy = field(default_factory=Inline)

    @classmethod
    def from_services(cls, services: Any, app_config: AppConfig) -> "TurnDeps":
        """Map host-assembled AgentServices (resolved ports) into typed deps."""
        return cls(
            config=app_config,
            llm=services.llm_adapter,
            tts=services.tts_adapter,
            visual=services.visual_tool,
            memory=services.memory_adapter,
            tools=LegacyFunctionToolSet.from_services(services),
        )
