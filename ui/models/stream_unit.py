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
    stream_id: int | None = None
    stream_kind: str | None = None
    text_ready_at_ms: float | None = None
    audio_started_at_ms: float | None = None
    audio_ready_at_ms: float | None = None
    playback_started_at_ms: float | None = None
    audio_finished_at_ms: float | None = None
    text_finished_at_ms: float | None = None
    playback_advance_at_ms: float | None = None
    playback_finished_at_ms: float | None = None
    audio_error: str | None = None
    last_wait_reason: str | None = None

    @property
    def timeline(self) -> "StreamUnitState":
        return self
