"""TTS adapters (Phase 5).

Thin wrapper over the existing ``agent_tools.tts`` provider factory. The current
``CurrentGPTSoVITSAdapter`` / ``DummyTTSAdapter`` already satisfy ``TTSPort``, so
this just exposes them for registry-based, resolve-by-name construction.
"""

from agent_tools.tts.manager import CURRENT_GPTSOVITS_PROVIDERS, build_tts_adapter


def build_tts(config: dict | None = None, service=None):
    """Build the configured TTS adapter (provider read from ``config``)."""
    return build_tts_adapter(config or {}, service=service)


__all__ = ["build_tts", "build_tts_adapter", "CURRENT_GPTSOVITS_PROVIDERS"]
