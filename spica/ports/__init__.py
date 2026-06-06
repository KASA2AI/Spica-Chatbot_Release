"""Capability protocols: LLM / TTS / Visual / Memory / Tool ports (Phase 5).

ASRPort is intentionally not defined yet (ASR is not on the core conversation
path; it arrives when a second ASR engine does -- see REFACTOR_PLAN Phase 9).
"""

from spica.ports.llm import LLMPort
from spica.ports.memory import MemoryItem, MemoryPort, MemoryScope
from spica.ports.tool import ToolPort
from spica.ports.tts import TTSPort
from spica.ports.visual import VisualPort

__all__ = [
    "LLMPort",
    "TTSPort",
    "VisualPort",
    "MemoryPort",
    "MemoryScope",
    "MemoryItem",
    "ToolPort",
]
