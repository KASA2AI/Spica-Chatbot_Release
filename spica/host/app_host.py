"""Application host: the composition root for the Spica platform.

``AppHost.initialize()`` constructs the backend services (LLM / TTS / Visual /
Memory adapters resolved by configured name from the ``CapabilityRegistry``, the
active character package, the built-in tools) and wires them into the conversation
core. The UI no longer ``new``s any service -- it calls ``AppHost().initialize()``
and reads the services back.

INVARIANT (CLAUDE.md #1): this module -- and everything under ``spica/`` -- must
never import PySide / Qt / any GUI library. The services constructed here are
all Qt-free, so the host stays framework-agnostic. That is what lets a future
Web/React front-end subscribe to the host without the core changing.

The host exposes two narrow surfaces rather than one fat object:

- ``conversation_surface`` (for the chat window) -- the ``ChatEngine`` that drives
  a turn (run / stream) and owns character / memory management.
- ``management_surface`` (for the settings centre) -- the ``ManagementSurface``
  that lists adapters / characters / plugins and reads / writes typed config.
"""

from __future__ import annotations

from typing import Any, Callable

from spica.config.manager import ConfigManager
from spica.config.schema import AppConfig
from spica.config.secrets import Secrets, load_secrets
from spica.core.chat_engine import ChatEngine
from spica.conversation.character_loader import DEFAULT_SPICA_SKILL_DIR
from spica.core.character import load_character_package
from spica.host.agent_assembly import build_agent_services
from spica.host.builtins import register_builtin_adapters
from spica.host.management import ManagementSurface
from spica.host.warmup import run_warmup
from spica.plugins.host import PluginHost
from spica.plugins.registry import CapabilityRegistry
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
        self.character_package: Any | None = None
        self.chat_engine: Any | None = None
        self.tts_provider: str = "gptsovits_current"
        self.registry = CapabilityRegistry()
        register_builtin_adapters(self.registry)
        self.plugin_host = PluginHost(self.registry)
        self._management = ManagementSurface(
            registry=self.registry,
            config_manager=ConfigManager(),
            plugin_host=self.plugin_host,
            characters_root=DEFAULT_SPICA_SKILL_DIR.parent,
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
            # Load external plugins so they can register adapters/tools into the
            # registry before capabilities are resolved by configured name (Phase 8).
            self.plugin_host.load()
            # Load the active character package first so its asset references
            # drive visual/tts construction (Phase 7b). Spica's package leaves the
            # paths unset -> engine defaults -> behaviour unchanged.
            self.character_package = load_character_package(
                self.config.character.package_dir or DEFAULT_SPICA_SKILL_DIR
            )
            # Keep skill_dir in sync so ChatEngine.set_interlocutor_name reloads
            # the active package's persona.
            self.config.character.skill_dir = self.character_package.skill_dir
            self.visual_tool = self.registry.resolve_visual(
                "spica_diff", config_path=self.character_package.visual_config_path
            )
            tts_config = (
                load_tts_config(self.character_package.tts_config_path)
                if self.character_package.tts_config_path
                else load_tts_config()
            )
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
                character_package=self.character_package,
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
            # C7: the turn resolves tools from the registry (inspect_screen ToolPort).
            self.services.tool_registry = self.registry
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

        Forwards to ``spica.host.warmup.run_warmup`` over the surfaces it uses.
        The UI runs this on a background thread and maps stages to its loading UI;
        keeping this method preserves that call site (``host.warmup(...)``).
        """
        run_warmup(self.conversation_surface, self.tts_adapter, on_progress)

    @property
    def management_surface(self) -> Any:
        """Entry point for the settings centre (Phase 8)."""
        return self._management
