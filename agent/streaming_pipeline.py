"""Compatibility shim (Phase 6C).

The streaming pipeline has been decomposed into ``spica/runtime/*`` components and
``spica/runtime/orchestrator.py``. This module re-exports the public surface so
existing imports (``agent.simple_agent``, ``spica.core.chat_engine``, tests) keep
working unchanged.
"""

from __future__ import annotations

from agent.text_normalizer import build_tts_text
from spica.runtime.orchestrator import stream_voice_events
from spica.runtime.play_unit_splitter import JsonAnswerExtractor, PlayUnitSplitter

__all__ = [
    "build_tts_text",
    "stream_voice_events",
    "JsonAnswerExtractor",
    "PlayUnitSplitter",
]
