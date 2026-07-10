from __future__ import annotations

from .dummy import DummyTTSAdapter
from .gptsovits_current import CurrentGPTSoVITSAdapter
from .text_only import TextOnlyTTSAdapter

__all__ = ["CurrentGPTSoVITSAdapter", "DummyTTSAdapter", "TextOnlyTTSAdapter"]
