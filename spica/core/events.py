"""Runtime events crossing the Host -> UI boundary (Phase 6A).

Replaces the legacy ``{"event": kind, "data": {...}}`` dicts the streaming
pipeline emits with typed dataclasses, WITHOUT touching the pipeline internals
(that decomposition is Phase 6C). Each event serialises to the exact legacy dict
via ``to_legacy_dict`` and is reconstructed via ``event_from_legacy``, so the two
representations are losslessly interchangeable -- which lets the format-agnostic
golden tests run over both paths and lets the UI keep consuming dicts during the
transition (the reverse adapter lives in ``SimpleAgent.stream_voice``).

INVARIANT (CLAUDE.md #1 + #7): Qt-free; cross-boundary events are dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar


@dataclass(frozen=True)
class RuntimeEvent:
    """Base for all Host -> UI events. Subclasses declare ``kind`` + fields."""

    kind: ClassVar[str] = ""

    def _data(self) -> dict[str, Any]:
        raise NotImplementedError

    def to_legacy_dict(self) -> dict[str, Any]:
        return {"event": self.kind, "data": self._data()}


@dataclass(frozen=True)
class StatusEvent(RuntimeEvent):
    kind: ClassVar[str] = "status"
    state: str
    message: str = ""

    def _data(self) -> dict[str, Any]:
        return {"state": self.state, "message": self.message}


@dataclass(frozen=True)
class UnitTextReadyEvent(RuntimeEvent):
    kind: ClassVar[str] = "unit_text_ready"
    index: int
    display_text: str
    tts_text: str
    emotion: str
    timing: dict[str, Any] = field(default_factory=dict)

    def _data(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "display_text": self.display_text,
            "tts_text": self.tts_text,
            "emotion": self.emotion,
            "timing": self.timing,
        }


@dataclass(frozen=True)
class UnitVisualReadyEvent(RuntimeEvent):
    kind: ClassVar[str] = "unit_visual_ready"
    index: int
    visual: dict[str, Any]
    cue: dict[str, Any]
    visual_error: str | None = None
    timing: dict[str, Any] = field(default_factory=dict)

    def _data(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "visual": self.visual,
            "cue": self.cue,
            "visual_error": self.visual_error,
            "timing": self.timing,
        }


@dataclass(frozen=True)
class UnitAudioStartedEvent(RuntimeEvent):
    kind: ClassVar[str] = "unit_audio_started"
    index: int
    tts_text: str
    emotion: str
    timing: dict[str, Any] = field(default_factory=dict)

    def _data(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "tts_text": self.tts_text,
            "emotion": self.emotion,
            "timing": self.timing,
        }


@dataclass(frozen=True)
class UnitAudioReadyEvent(RuntimeEvent):
    kind: ClassVar[str] = "unit_audio_ready"
    index: int
    audio_url: str | None
    audio_path: str | None
    audio_error: str | None = None
    timing: dict[str, Any] = field(default_factory=dict)

    def _data(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "audio_url": self.audio_url,
            "audio_path": self.audio_path,
            "audio_error": self.audio_error,
            "timing": self.timing,
        }


@dataclass(frozen=True)
class UnitReadyEvent(RuntimeEvent):
    kind: ClassVar[str] = "unit_ready"
    index: int
    display_text: str
    tts_text: str
    emotion: str
    visual: dict[str, Any]
    audio_url: str | None
    audio_path: str | None
    timing: dict[str, Any] = field(default_factory=dict)
    audio_error: str | None = None

    def _data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "index": self.index,
            "display_text": self.display_text,
            "tts_text": self.tts_text,
            "emotion": self.emotion,
            "visual": self.visual,
            "audio_url": self.audio_url,
            "audio_path": self.audio_path,
            "timing": self.timing,
        }
        # audio_error only appears in the legacy dict when present (matches pipeline).
        if self.audio_error:
            data["audio_error"] = self.audio_error
        return data


@dataclass(frozen=True)
class DoneEvent(RuntimeEvent):
    kind: ClassVar[str] = "done"
    answer: str
    emotion: str
    emotion_label: str
    emotion_reason: str
    units_count: int
    timing: dict[str, Any] = field(default_factory=dict)

    def _data(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "emotion": self.emotion,
            "emotion_label": self.emotion_label,
            "emotion_reason": self.emotion_reason,
            "units_count": self.units_count,
            "timing": self.timing,
        }


@dataclass(frozen=True)
class ErrorEvent(RuntimeEvent):
    kind: ClassVar[str] = "error"
    message: str

    def _data(self) -> dict[str, Any]:
        return {"message": self.message}


@dataclass(frozen=True)
class GenericEvent(RuntimeEvent):
    """Fallback for unknown event kinds; carries the raw data verbatim so the
    live UI path never loses an event the typed classes don't model yet."""

    event_kind: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def _data(self) -> dict[str, Any]:
        return dict(self.data)

    def to_legacy_dict(self) -> dict[str, Any]:
        return {"event": self.event_kind, "data": dict(self.data)}


# kind -> constructor from a legacy ``data`` dict.
_FROM_DATA: dict[str, Callable[[dict[str, Any]], RuntimeEvent]] = {
    "status": lambda d: StatusEvent(state=d.get("state", ""), message=d.get("message", "")),
    "unit_text_ready": lambda d: UnitTextReadyEvent(
        index=d.get("index"),
        display_text=d.get("display_text", ""),
        tts_text=d.get("tts_text", ""),
        emotion=d.get("emotion", ""),
        timing=d.get("timing") or {},
    ),
    "unit_visual_ready": lambda d: UnitVisualReadyEvent(
        index=d.get("index"),
        visual=d.get("visual") or {},
        cue=d.get("cue") or {},
        visual_error=d.get("visual_error"),
        timing=d.get("timing") or {},
    ),
    "unit_audio_started": lambda d: UnitAudioStartedEvent(
        index=d.get("index"),
        tts_text=d.get("tts_text", ""),
        emotion=d.get("emotion", ""),
        timing=d.get("timing") or {},
    ),
    "unit_audio_ready": lambda d: UnitAudioReadyEvent(
        index=d.get("index"),
        audio_url=d.get("audio_url"),
        audio_path=d.get("audio_path"),
        audio_error=d.get("audio_error"),
        timing=d.get("timing") or {},
    ),
    "unit_ready": lambda d: UnitReadyEvent(
        index=d.get("index"),
        display_text=d.get("display_text", ""),
        tts_text=d.get("tts_text", ""),
        emotion=d.get("emotion", ""),
        visual=d.get("visual") or {},
        audio_url=d.get("audio_url"),
        audio_path=d.get("audio_path"),
        timing=d.get("timing") or {},
        audio_error=d.get("audio_error"),
    ),
    "done": lambda d: DoneEvent(
        answer=d.get("answer", ""),
        emotion=d.get("emotion", ""),
        emotion_label=d.get("emotion_label", ""),
        emotion_reason=d.get("emotion_reason", ""),
        units_count=d.get("units_count", 0),
        timing=d.get("timing") or {},
    ),
    "error": lambda d: ErrorEvent(message=d.get("message", "")),
}


def event_from_legacy(event: Any) -> RuntimeEvent:
    """Adapt a legacy ``{"event", "data"}`` dict (or pass through a RuntimeEvent)."""
    if isinstance(event, RuntimeEvent):
        return event
    kind = str(event.get("event") or "")
    data = event.get("data") or {}
    ctor = _FROM_DATA.get(kind)
    if ctor is None:
        return GenericEvent(event_kind=kind, data=dict(data))
    return ctor(data)


__all__ = [
    "RuntimeEvent",
    "StatusEvent",
    "UnitTextReadyEvent",
    "UnitVisualReadyEvent",
    "UnitAudioStartedEvent",
    "UnitAudioReadyEvent",
    "UnitReadyEvent",
    "DoneEvent",
    "ErrorEvent",
    "GenericEvent",
    "event_from_legacy",
]
