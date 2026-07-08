"""Fold a turn's RuntimeEvent stream into a synchronous response payload (C2).

The synchronous path is now "drive run_turn with Inline, collect the events,
fold them" -- the inverse of streaming. Instead of the UI consuming events live,
``fold_events`` replays them into the single dict ``ChatEngine.run_voice`` returns.

This is deliberately LOSSY relative to the old ``build_response_node`` payload:
the event stream carries less than the old ``AgentState`` did. fold guarantees
only the *consumed* whitelist --
``answer`` / ``conversation_id`` / ``emotion`` / ``audio_url`` + ``audio_path`` /
``visual`` / ``timing`` -- plus, on failure, an ``error`` payload mirroring the
streaming error semantics. The sync-only extras (``tts_chunks`` / ``tts_params`` /
``tools`` / full visual semantics / fine error codes) are intentionally dropped;
see the difference whitelist in tests/test_fold.py. Full parity waits for a
richer ``done`` TurnSummary (C3c/C7).

audio/visual are taken from the FIRST play unit (a representative, not the
old whole-answer audio) -- another documented lossy reduction.

Pure: no ``agent`` import, Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from typing import Any, Iterable

from spica.core.events import DoneEvent, ErrorEvent, RuntimeEvent, UnitReadyEvent


def fold_events(events: Iterable[RuntimeEvent], conversation_id: str = "default") -> dict[str, Any]:
    """Collapse a run_turn event stream into a response payload dict."""
    done: DoneEvent | None = None
    error: ErrorEvent | None = None
    units: dict[int, UnitReadyEvent] = {}
    for event in events:
        if isinstance(event, DoneEvent):
            done = event
        elif isinstance(event, ErrorEvent):
            error = event
        elif isinstance(event, UnitReadyEvent):
            units[event.index] = event

    if done is None:
        return _error_payload(error, conversation_id)

    first = units.get(0)
    return {
        "answer": done.answer,
        "conversation_id": conversation_id,
        "emotion": {
            "name": done.emotion,
            "label": done.emotion_label,
            "reason": done.emotion_reason,
        },
        "audio_url": first.audio_url if first else None,
        "audio_path": first.audio_path if first else None,
        "visual": dict(first.visual) if first else {},
        "timing": dict(done.timing),
    }


def _error_payload(error: ErrorEvent | None, conversation_id: str) -> dict[str, Any]:
    """Shape an error turn the way the streaming error path means it: no answer
    audio, an ``error`` carrying the stream's message. Error *code* granularity is
    on the difference whitelist (sync used codes like EMPTY_MESSAGE; fold doesn't
    have them from the event stream), so it uses a single generic code."""
    return {
        "answer": "メッセージを入力してください。",
        "conversation_id": conversation_id,
        # "惊" mirrors EMOTION_LABELS["surprised"]; not imported to avoid a new
        # spica -> agent edge, and the error-path emotion is difference-whitelisted.
        "emotion": {"name": "surprised", "label": "惊", "reason": "ターンが完了しませんでした。"},
        "audio_url": None,
        "audio_path": None,
        "visual": {},
        "timing": {},
        "error": {
            "code": "STREAM_ERROR",
            "message": error.message if error else "ターンが失敗しました。",
        },
    }
