from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StreamUnitState:
    index: int
    display_text: str = "……"
    tts_text: str | None = None
    audio_path: str | None = None
    visual: dict[str, Any] = field(default_factory=dict)
    cue: dict[str, Any] = field(default_factory=dict)
    text_ready: bool = True
    audio_ready: bool = True
    visual_ready: bool = True
    playback_started: bool = False
    playback_finished: bool = False
