"""Application host: the composition root for the Spica platform.

Phase 1 of the platform refactor moves backend construction out of the PySide
``OverlayWindow`` and into ``AppHost.initialize()``. This is a *mechanical* move:
``initialize()`` still constructs exactly today's ``SimpleAgent`` / TTS adapter /
``VisualDiffService`` with identical behaviour. The UI no longer ``new``s any
service -- it calls ``AppHost().initialize()`` and reads the services back.

INVARIANT (CLAUDE.md #1): this module -- and everything under ``spica/`` -- must
never import PySide / Qt / any GUI library. The services constructed here are
all Qt-free, so the host stays framework-agnostic. That is what lets a future
Web/React front-end subscribe to the host without the core changing.

The host exposes two narrow surfaces rather than one fat object:

- ``conversation_surface`` (for the chat window) -- Phase 1: a thin alias to the
  current ``SimpleAgent``; its full protocol shape lands in Phase 6.
- ``management_surface`` (for the settings centre) -- a ``NotImplementedError``
  placeholder until Phase 8, so we do not design that interface prematurely.
"""

from __future__ import annotations

from typing import Any, Callable

from spica.config.manager import ConfigManager
from spica.config.schema import AppConfig
from spica.config.secrets import Secrets, load_secrets
from spica.core.chat_engine import ChatEngine
from spica.host.agent_assembly import build_agent_services
from spica.plugins.registry import CapabilityRegistry
from spica.adapters.llm import OpenAICompatibleAdapter
from spica.adapters.memory import SqliteMemoryAdapter
from spica.adapters.tts import build_tts
from spica.adapters.visual import build_spica_visual
from agent_tools.tts import CURRENT_GPTSOVITS_PROVIDERS, GPTSoVITSTool, load_tts_config


class AppHost:
    """Owns the backend services and wires them together at startup."""

    def __init__(self) -> None:
        self.config: AppConfig | None = None
        self.secrets: Secrets | None = None
        self.visual_tool: Any | None = None
        self.tts_tool: Any | None = None
        self.tts_adapter: Any | None = None
        self.services: Any | None = None
        self.chat_engine: Any | None = None
        self.tts_provider: str = "gptsovits_current"
        self.registry = CapabilityRegistry()
        self._register_builtin_adapters()

    def _register_builtin_adapters(self) -> None:
        """Register the built-in capability adapters by name (Phase 5).

        Resolving by the name in config (e.g. ``config.llm.provider``) is what
        makes "swap the engine by changing a config name" work; this is also the
        seam Phase 8 plugins register into.
        """
        self.registry.register_llm(
            "openai_compatible", lambda client=None: OpenAICompatibleAdapter(client)
        )
        for provider in (*CURRENT_GPTSOVITS_PROVIDERS, "dummy"):
            self.registry.register_tts(
                provider, lambda config=None, service=None: build_tts(config, service)
            )
        self.registry.register_visual("spica_diff", build_spica_visual)
        self.registry.register_memory(
            "sqlite", lambda store=None, recent=None: SqliteMemoryAdapter(store, recent)
        )

    def initialize(self) -> None:
        """Construct the backend services (moved verbatim from the UI).

        Mechanical move of ``OverlayWindow._init_backend``'s construction logic,
        with zero behaviour change. On failure it salvages ``visual_tool`` (so
        the character can still render) and re-raises, leaving the UI to surface
        the error message and read back whatever was built.
        """
        try:
            self.config = ConfigManager().load()
            self.secrets = load_secrets()
            self.visual_tool = self.registry.resolve_visual("spica_diff")
            tts_config = load_tts_config()
            self.tts_provider = str(
                tts_config.get("provider")
                or tts_config.get("tts_provider")
                or "gptsovits_current"
            )
            self.tts_tool = GPTSoVITSTool() if self.tts_provider in CURRENT_GPTSOVITS_PROVIDERS else None
            self.tts_adapter = self.registry.resolve_tts(
                self.tts_provider, config=tts_config, service=self.tts_tool
            )
            self.services = build_agent_services(
                self.config,
                self.secrets,
                tts_adapter=self.tts_adapter,
                visual_tool=self.visual_tool,
            )
            # Resolve and inject the LLM / memory adapters by configured name.
            self.services.llm_adapter = self.registry.resolve_llm(
                self.config.llm.provider, client=self.services.llm_client
            )
            self.services.memory_adapter = self.registry.resolve_memory(
                self.config.memory.provider,
                store=self.services.memory_store,
                recent=self.services.recent_memory,
            )
            # ChatEngine is the conversation core (Phase 6D: SimpleAgent dissolved
            # into ChatEngine + spica/host/agent_assembly).
            self.chat_engine = ChatEngine(self.services, self.config)
        except Exception:
            if self.visual_tool is None:
                try:
                    self.visual_tool = build_spica_visual()
                except Exception:
                    self.visual_tool = None
            raise

    @property
    def conversation_surface(self) -> Any:
        """Entry point for the chat window: the ChatEngine (None before initialize)."""
        return self.chat_engine

    def warmup(self, on_progress: Callable[[str, str], None]) -> None:
        """Run startup warmup (Phase 6E), reporting progress as
        ``on_progress(stage, message)`` where stage is
        ``"initializing" | "ready" | "error"``.

        Qt-free: the warmup logic (LLM ready + TTS model warmup), formerly in the
        UI's StartupWarmupWorker, now lives here so the host orchestrates startup.
        The UI runs this on a background thread and maps stages to its loading UI.
        """
        surface = self.conversation_surface
        tts = self.tts_adapter
        try:
            model = str(getattr(surface, "model", "") or "unknown")
            on_progress("initializing", f"LLM API 初始化完成：{model}")
            public_config = getattr(tts, "public_config", None)
            warmup = getattr(tts, "warmup", None)
            provider_name = str(getattr(tts, "name", None) or "TTS")
            if public_config is None or warmup is None:
                on_progress("ready", f"LLM API 已初始化，{provider_name} 无需启动预热。")
                return

            config = public_config()
            if not bool(config.get("warmup_on_startup", True)):
                on_progress("ready", f"LLM API 已初始化，{provider_name} 启动预热已关闭。")
                return

            configured_emotions = config.get("warmup_emotions")
            if isinstance(configured_emotions, list) and configured_emotions:
                emotions = [str(item) for item in configured_emotions if str(item).strip()]
            else:
                emotions = [str(config.get("warmup_emotion") or "happy")]
            if not emotions:
                emotions = [str(config.get("warmup_emotion") or "happy")]

            on_progress("initializing", f"正在预热 {provider_name} 模型...")
            results = [warmup(emotion=item, synthesize=True) for item in emotions]
            failed_results = [item for item in results if not item.get("ok")]
            total_duration_ms = sum(float(item.get("duration_ms") or 0) for item in results)
            if failed_results:
                messages = ", ".join(str(item.get("error") or "unknown") for item in failed_results)
                on_progress("error", f"{provider_name} warmup failed：{messages}")
                return
            on_progress("ready", f"{provider_name} 模型已就绪（{total_duration_ms:.0f}ms）。")
        except Exception as exc:
            on_progress("error", f"启动预热失败：{exc}")

    @property
    def management_surface(self) -> Any:
        """Entry point for the settings centre. Implemented in Phase 8."""
        raise NotImplementedError("ManagementSurface 在 Phase 8 实现")
