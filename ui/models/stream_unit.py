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
    visual_error: str | None = None
    visual_ready_at_ms: float | None = None
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


def merge_stream_unit_state(target: StreamUnitState, source: StreamUnitState) -> None:
    if source.display_text:
        target.display_text = source.display_text
    if source.tts_text is not None:
        target.tts_text = source.tts_text
    if source.audio_path:
        target.audio_path = source.audio_path
    if source.visual:
        target.visual = source.visual
    if source.cue:
        target.cue = source.cue
    target.text_ready = target.text_ready or source.text_ready
    target.audio_ready = target.audio_ready or source.audio_ready
    target.visual_ready = target.visual_ready or source.visual_ready
    if source.timeline.audio_error is not None:
        target.timeline.audio_error = source.timeline.audio_error
    if source.timeline.visual_error is not None:
        target.timeline.visual_error = source.timeline.visual_error
    if source.timeline.visual_ready_at_ms is not None:
        target.timeline.visual_ready_at_ms = source.timeline.visual_ready_at_ms


def is_stream_unit_ready_for_playback(unit: StreamUnitState) -> bool:
    return bool(unit.text_ready and unit.audio_ready)
