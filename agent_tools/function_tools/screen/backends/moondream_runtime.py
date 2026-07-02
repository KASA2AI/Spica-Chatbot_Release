"""Moondream backend-load dispatch seam (LOCAL_RUNTIME_PLAN cut 4).

The ``MoondreamModelManager`` used to call ``MoondreamBackend.load(config)``
DIRECTLY -- so a provider swap had nowhere to hook. This module is the seam: the
manager now calls ``load_moondream_backend(config)`` instead. ``load_moondream_
backend`` routes through the host-installed Moondream provider when one is
installed, else falls back to the legacy ``MoondreamBackend.load`` -- byte-
identical to before.

WHY an install hook, not an import: ``model_manager`` lives in ``agent_tools`` and
the provider factory + new HF runtime live in ``spica/local_runtime`` /
``spica/host``. Rather than have ``model_manager`` import the spica factory
(provider-coupled + cycle-risk), the host (``spica``, which already imports
``agent_tools``) INSTALLS the chosen provider here at startup. Process-global, set
once -- consistent with the existing process-global ``_MANAGER`` singleton in
``model_manager`` and the sibling ``ocr_runtime`` seam.

ZERO-DIFF DEFAULT (P0): when ``screen.provider == "moondream_local"``
(default/fallback) the host does NOT install a provider, so
``load_moondream_backend`` calls the legacy ``MoondreamBackend.load(config)``
EXACTLY -- same signature, same kwargs, same order -- and the existing screen
behaviour is unchanged down to the byte.
"""

from __future__ import annotations

from typing import Any

from agent_tools.function_tools.screen.backends.moondream import MoondreamBackend
from agent_tools.function_tools.screen.config import ScreenPipelineConfig

# A Moondream-provider-shaped object (has ``.load(config) -> backend``) or None.
# None -> legacy default (bare ``MoondreamBackend.load``), the zero-diff path.
_ACTIVE_MOONDREAM_PROVIDER: Any | None = None


def set_active_moondream_provider(provider: Any | None) -> None:
    """Install the Moondream provider the manager seam uses (called once by the host)."""
    global _ACTIVE_MOONDREAM_PROVIDER
    _ACTIVE_MOONDREAM_PROVIDER = provider


def get_active_moondream_provider() -> Any | None:
    return _ACTIVE_MOONDREAM_PROVIDER


def reset_active_moondream_provider() -> None:
    """Clear the installed provider (test isolation; restores the legacy default)."""
    global _ACTIVE_MOONDREAM_PROVIDER
    _ACTIVE_MOONDREAM_PROVIDER = None


def load_moondream_backend(config: ScreenPipelineConfig) -> Any:
    """Load the Moondream backend through the installed provider, else the legacy.

    Returns a backend exposing ``.query(image, question) -> result-with-.text``
    (both ``MoondreamBackend`` and ``MoondreamHfBackend`` satisfy this). When no
    provider is installed, this calls ``MoondreamBackend.load(config)`` EXACTLY --
    byte-identical to the pre-cut manager behaviour (the zero-diff default)."""
    provider = _ACTIVE_MOONDREAM_PROVIDER
    if provider is None:
        return MoondreamBackend.load(config)  # legacy default -- byte-identical to pre-cut behaviour
    return provider.load(config)
