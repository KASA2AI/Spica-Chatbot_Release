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

from typing import Any

from agent import SimpleAgent
from spica.config.manager import ConfigManager
from spica.config.schema import AppConfig
from spica.config.secrets import Secrets, load_secrets
from agent_tools.tts import (
    CURRENT_GPTSOVITS_PROVIDERS,
    GPTSoVITSTool,
    build_tts_adapter,
    load_tts_config,
)
from agent_tools.visual import VisualDiffService


class AppHost:
    """Owns the backend services and wires them together at startup."""

    def __init__(self) -> None:
        self.config: AppConfig | None = None
        self.secrets: Secrets | None = None
        self.visual_tool: Any | None = None
        self.tts_tool: Any | None = None
        self.tts_adapter: Any | None = None
        self.agent: Any | None = None
        self.tts_provider: str = "gptsovits_current"

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
            self.visual_tool = VisualDiffService()
            tts_config = load_tts_config()
            self.tts_provider = str(
                tts_config.get("provider")
                or tts_config.get("tts_provider")
                or "gptsovits_current"
            )
            if self.tts_provider in CURRENT_GPTSOVITS_PROVIDERS:
                self.tts_tool = GPTSoVITSTool()
                self.tts_adapter = build_tts_adapter(tts_config, service=self.tts_tool)
            else:
                self.tts_tool = None
                self.tts_adapter = build_tts_adapter(tts_config)
            self.agent = SimpleAgent(
                tts_adapter=self.tts_adapter,
                visual_tool=self.visual_tool,
                config=self.config,
                secrets=self.secrets,
            )
        except Exception:
            if self.visual_tool is None:
                try:
                    self.visual_tool = VisualDiffService()
                except Exception:
                    self.visual_tool = None
            raise

    @property
    def conversation_surface(self) -> Any:
        """Entry point for the chat window. Phase 1: alias to ``SimpleAgent``."""
        return self.agent

    @property
    def management_surface(self) -> Any:
        """Entry point for the settings centre. Implemented in Phase 8."""
        raise NotImplementedError("ManagementSurface 在 Phase 8 实现")
