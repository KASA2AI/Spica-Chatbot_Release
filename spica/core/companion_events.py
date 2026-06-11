"""Galgame Host->UI events + the sink contract (Phase 4).

A long-lived, per-turn-INDEPENDENT channel: ``GalgameCompanionSession`` emits these
from its OWN lifecycle (OCR loop / summary / window monitor in later phases), via
an injected Qt-free sink -- NOT from ``run_turn``. They are ``RuntimeEvent``
subclasses (NOT a parallel base) so they reuse the existing ``to_legacy_dict`` /
``event_from_legacy`` transport + ``GenericEvent`` fallback; each registers into
``events._FROM_DATA`` for strong-typed round-trip (import this module to register).

``CompanionEventSink`` is a Qt-free callable; the default ``noop_companion_sink``
lets a session run headless (no UI / tests / not-yet-started) without depending on
a live UI. ``spica/`` is Qt-free (CLAUDE.md #1); the concrete Qt bridge lives in
``ui/`` and is injected down (Phase 0 ④).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar

from spica.core.events import RuntimeEvent, register_event

CompanionEventSink = Callable[[RuntimeEvent], None]


def noop_companion_sink(event: RuntimeEvent) -> None:
    """Default sink: drop the event. Lets a session run with no UI attached."""
    return None


@dataclass(frozen=True)
class GalgameStatusChangedEvent(RuntimeEvent):
    kind: ClassVar[str] = "galgame_status_changed"
    state: str
    previous: str = ""
    message: str = ""

    def _data(self) -> dict[str, Any]:
        return {"state": self.state, "previous": self.previous, "message": self.message}


@dataclass(frozen=True)
class GalgameWindowLostEvent(RuntimeEvent):
    kind: ClassVar[str] = "galgame_window_lost"
    reason: str = ""

    def _data(self) -> dict[str, Any]:
        return {"reason": self.reason}


@dataclass(frozen=True)
class GalgameWindowRecoveredEvent(RuntimeEvent):
    kind: ClassVar[str] = "galgame_window_recovered"

    def _data(self) -> dict[str, Any]:
        return {}


@dataclass(frozen=True)
class GalgameSummaryStartedEvent(RuntimeEvent):
    kind: ClassVar[str] = "galgame_summary_started"
    reason: str = ""

    def _data(self) -> dict[str, Any]:
        return {"reason": self.reason}


@dataclass(frozen=True)
class GalgameSummaryProgressEvent(RuntimeEvent):
    kind: ClassVar[str] = "galgame_summary_progress"
    progress: float = 0.0
    message: str = ""

    def _data(self) -> dict[str, Any]:
        return {"progress": self.progress, "message": self.message}


@dataclass(frozen=True)
class GalgameSummaryDoneEvent(RuntimeEvent):
    kind: ClassVar[str] = "galgame_summary_done"
    summary_id: str | None = None

    def _data(self) -> dict[str, Any]:
        return {"summary_id": self.summary_id}


@dataclass(frozen=True)
class GalgameStableLineCommittedEvent(RuntimeEvent):
    # Type defined now; fired by the OCR text stream in Phase 7.
    kind: ClassVar[str] = "galgame_stable_line_committed"
    line_id: str
    speaker: str | None = None
    text: str = ""

    def _data(self) -> dict[str, Any]:
        return {"line_id": self.line_id, "speaker": self.speaker, "text": self.text}


@dataclass(frozen=True)
class GalgameChoiceDetectedEvent(RuntimeEvent):
    kind: ClassVar[str] = "galgame_choice_detected"
    choice_id: str
    options: list[dict[str, Any]] = field(default_factory=list)

    def _data(self) -> dict[str, Any]:
        return {"choice_id": self.choice_id, "options": self.options}


@dataclass(frozen=True)
class GalgameChoiceRecordedEvent(RuntimeEvent):
    kind: ClassVar[str] = "galgame_choice_recorded"
    choice_id: str
    selected_index: int | None = None
    selected_text: str | None = None

    def _data(self) -> dict[str, Any]:
        return {
            "choice_id": self.choice_id,
            "selected_index": self.selected_index,
            "selected_text": self.selected_text,
        }


@dataclass(frozen=True)
class GalgameOcrPreviewReadyEvent(RuntimeEvent):
    # Backend -> UI: a captured/cropped region preview (PNG bytes, matching the
    # existing image_bytes pattern). suspect_blank flags a likely blank/black frame
    # (Wayland/occlusion) so the UI can warn instead of showing a silent black box.
    kind: ClassVar[str] = "galgame_ocr_preview_ready"
    region: str = "dialog"  # dialog | speaker
    image_png: bytes = b""
    width: int = 0
    height: int = 0
    suspect_blank: bool = False

    def _data(self) -> dict[str, Any]:
        return {
            "region": self.region,
            "image_png": self.image_png,
            "width": self.width,
            "height": self.height,
            "suspect_blank": self.suspect_blank,
        }


@dataclass(frozen=True)
class GalgameOcrTestResultEvent(RuntimeEvent):
    kind: ClassVar[str] = "galgame_ocr_test_result"
    dialog_text: str = ""
    speaker_text: str | None = None
    speaker_strategy: str = "region"

    def _data(self) -> dict[str, Any]:
        return {
            "dialog_text": self.dialog_text,
            "speaker_text": self.speaker_text,
            "speaker_strategy": self.speaker_strategy,
        }


@dataclass(frozen=True)
class GalgameWindowCandidatesEvent(RuntimeEvent):
    # Backend -> UI: candidate windows awaiting the user's pick (mode="pick") or a
    # first-time confirm (mode="confirm"). candidates are serialized dicts.
    kind: ClassVar[str] = "galgame_window_candidates"
    candidates: list[dict[str, Any]] = field(default_factory=list)
    mode: str = "pick"  # pick | confirm

    def _data(self) -> dict[str, Any]:
        return {"candidates": self.candidates, "mode": self.mode}


@dataclass(frozen=True)
class GalgameGameBoundEvent(RuntimeEvent):
    kind: ClassVar[str] = "galgame_game_bound"
    game_id: str
    window_id: str = ""
    title: str = ""

    def _data(self) -> dict[str, Any]:
        return {"game_id": self.game_id, "window_id": self.window_id, "title": self.title}


@dataclass(frozen=True)
class GalgameBindFailedEvent(RuntimeEvent):
    # Readable failure with a machine code + the lightweight options to offer (§4.4):
    # e.g. rechoose_launch / manual_bind / install_wmctrl / retry / cancel.
    kind: ClassVar[str] = "galgame_bind_failed"
    reason: str = ""
    code: str = ""
    options: list[str] = field(default_factory=list)

    def _data(self) -> dict[str, Any]:
        return {"reason": self.reason, "code": self.code, "options": self.options}


@dataclass(frozen=True)
class GalgameErrorEvent(RuntimeEvent):
    kind: ClassVar[str] = "galgame_error"
    message: str
    code: str = ""
    session_id: str = ""
    target_state: str = ""

    def _data(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "code": self.code,
            "session_id": self.session_id,
            "target_state": self.target_state,
        }


# Strong-typed legacy round-trip: register each kind. Without this they would still
# survive as GenericEvent (lossless), but the typed reconstruction is preferred.
register_event(
    GalgameStatusChangedEvent.kind,
    lambda d: GalgameStatusChangedEvent(
        state=d.get("state", ""), previous=d.get("previous", ""), message=d.get("message", "")
    ),
)
register_event(GalgameWindowLostEvent.kind, lambda d: GalgameWindowLostEvent(reason=d.get("reason", "")))
register_event(GalgameWindowRecoveredEvent.kind, lambda d: GalgameWindowRecoveredEvent())
register_event(GalgameSummaryStartedEvent.kind, lambda d: GalgameSummaryStartedEvent(reason=d.get("reason", "")))
register_event(
    GalgameSummaryProgressEvent.kind,
    lambda d: GalgameSummaryProgressEvent(progress=d.get("progress", 0.0), message=d.get("message", "")),
)
register_event(GalgameSummaryDoneEvent.kind, lambda d: GalgameSummaryDoneEvent(summary_id=d.get("summary_id")))
register_event(
    GalgameStableLineCommittedEvent.kind,
    lambda d: GalgameStableLineCommittedEvent(
        line_id=d.get("line_id", ""), speaker=d.get("speaker"), text=d.get("text", "")
    ),
)
register_event(
    GalgameChoiceDetectedEvent.kind,
    lambda d: GalgameChoiceDetectedEvent(choice_id=d.get("choice_id", ""), options=d.get("options") or []),
)
register_event(
    GalgameChoiceRecordedEvent.kind,
    lambda d: GalgameChoiceRecordedEvent(
        choice_id=d.get("choice_id", ""),
        selected_index=d.get("selected_index"),
        selected_text=d.get("selected_text"),
    ),
)
register_event(
    GalgameOcrPreviewReadyEvent.kind,
    lambda d: GalgameOcrPreviewReadyEvent(
        region=d.get("region", "dialog"), image_png=d.get("image_png", b""),
        width=d.get("width", 0), height=d.get("height", 0), suspect_blank=bool(d.get("suspect_blank", False)),
    ),
)
register_event(
    GalgameOcrTestResultEvent.kind,
    lambda d: GalgameOcrTestResultEvent(
        dialog_text=d.get("dialog_text", ""), speaker_text=d.get("speaker_text"),
        speaker_strategy=d.get("speaker_strategy", "region"),
    ),
)
register_event(
    GalgameWindowCandidatesEvent.kind,
    lambda d: GalgameWindowCandidatesEvent(candidates=d.get("candidates") or [], mode=d.get("mode", "pick")),
)
register_event(
    GalgameGameBoundEvent.kind,
    lambda d: GalgameGameBoundEvent(
        game_id=d.get("game_id", ""), window_id=d.get("window_id", ""), title=d.get("title", "")
    ),
)
register_event(
    GalgameBindFailedEvent.kind,
    lambda d: GalgameBindFailedEvent(
        reason=d.get("reason", ""), code=d.get("code", ""), options=d.get("options") or []
    ),
)
register_event(
    GalgameErrorEvent.kind,
    lambda d: GalgameErrorEvent(
        message=d.get("message", ""), code=d.get("code", ""),
        session_id=d.get("session_id", ""), target_state=d.get("target_state", ""),
    ),
)


__all__ = [
    "CompanionEventSink",
    "noop_companion_sink",
    "GalgameStatusChangedEvent",
    "GalgameWindowLostEvent",
    "GalgameWindowRecoveredEvent",
    "GalgameSummaryStartedEvent",
    "GalgameSummaryProgressEvent",
    "GalgameSummaryDoneEvent",
    "GalgameStableLineCommittedEvent",
    "GalgameChoiceDetectedEvent",
    "GalgameChoiceRecordedEvent",
    "GalgameOcrPreviewReadyEvent",
    "GalgameOcrTestResultEvent",
    "GalgameWindowCandidatesEvent",
    "GalgameGameBoundEvent",
    "GalgameBindFailedEvent",
    "GalgameErrorEvent",
]
