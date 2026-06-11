"""Manual galgame-memory feed facade (Phase 2).

Bypasses the game window + OCR so all five committed data kinds can be written
straight into a ``GameMemoryPort``. NO OCR, NO live session FSM (Phase 4), NO
gated-stage injection (Phase 3). Hand-fed lines are treated as already
confirmed -- they land as ``committed`` StoryLines and never pass through
``pending_current`` (which is an OCR-stream concept reserved for Phase 7).

Hand-fed data is not a real play session, so lines/summaries/choices/beats are
tied to a *synthetic* session id (``manual::<game_id>::<playthrough_id>``) and NO
``PlaySession`` row is fabricated (Phase 4 owns session lifecycle; the manual feed
therefore never shows up in ``dangling_play_sessions``).

Identity (``character_id`` / ``user_id``) is required at construction so this
domain layer does not import the conversation layer. Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

from spica.galgame.models import (
    ChoiceEvent,
    CompanionBeat,
    GameProgressState,
    StoryLine,
    StoryLineStatus,
    StorySummary,
    utc_now_iso,
)
from spica.ports.game_memory import GameMemoryPort


def _manual_session_id(game_id: str, playthrough_id: str) -> str:
    return f"manual::{game_id}::{playthrough_id}"


def _new_id() -> str:
    return uuid.uuid4().hex


def _placeholder_summary(lines: list[StoryLine]) -> str:
    # Phase 2 stub: NOT a real summary. The point of this phase is source_line_ids
    # selection / persistence / buffer advance, not summary quality (real LLM
    # summarization lands in Phase 8). Mark it clearly as a placeholder.
    return "（占位拼接）" + " ".join(line.text for line in lines)


def _normalize_options(options: Any) -> list[dict[str, Any]]:
    if not options:
        return []
    normalized: list[dict[str, Any]] = []
    for fallback_index, option in enumerate(options, start=1):
        if isinstance(option, dict):
            index = int(option.get("index", fallback_index))
            text = str(option.get("text", ""))
        else:
            index = fallback_index
            text = str(option)
        normalized.append({"index": index, "text": text})
    return normalized


def _resolve_selection(
    options: list[dict[str, Any]], selected_option: Any
) -> tuple[int | None, str | None]:
    if selected_option is None:
        return None, None
    # bool is an int subclass -- exclude it so True/False never read as an index.
    if isinstance(selected_option, int) and not isinstance(selected_option, bool):
        text = next((o["text"] for o in options if o["index"] == selected_option), None)
        return selected_option, text
    text = str(selected_option)
    index = next((o["index"] for o in options if o["text"] == text), None)
    return index, text


def _merge_progress(state: GameProgressState, fields: dict[str, Any]) -> GameProgressState:
    if not fields:
        return state
    valid = {f.name for f in dataclasses.fields(GameProgressState)}
    unknown = set(fields) - valid
    if unknown:
        raise TypeError(f"unknown GameProgressState fields: {sorted(unknown)}")
    return dataclasses.replace(state, **fields)


class ManualGameMemory:
    """Thin write facade over a ``GameMemoryPort`` for hand-fed galgame data."""

    def __init__(self, game_memory: GameMemoryPort, *, character_id: str, user_id: str) -> None:
        self._mem = game_memory
        self._character_id = character_id
        self._user_id = user_id

    def manual_add_story_line(
        self, game_id: str, speaker: str | None, text: str, playthrough_id: str = "default"
    ) -> str:
        line = StoryLine(
            line_id=_new_id(),
            session_id=_manual_session_id(game_id, playthrough_id),
            game_id=game_id,
            text=text,
            timestamp=utc_now_iso(),
            playthrough_id=playthrough_id,
            speaker=speaker,
            source="manual",
            confidence=1.0,
            raw_hash="",
            status=StoryLineStatus.COMMITTED,  # hand-fed = already confirmed
        )
        return self._mem.add_story_line(line)

    def manual_flush_summary(self, game_id: str, playthrough_id: str = "default") -> str | None:
        batch = self._mem.unsummarized_committed_story_lines(game_id, playthrough_id)
        if not batch:
            return None
        now = utc_now_iso()
        summary = StorySummary(
            summary_id=_new_id(),
            game_id=game_id,
            playthrough_id=playthrough_id,
            session_id=_manual_session_id(game_id, playthrough_id),
            source_line_ids=[line.line_id for line in batch],
            summary_zh=_placeholder_summary(batch),
            created_at=now,
            updated_at=now,
            source="manual_note",
            revision=1,
        )
        return self._mem.add_summary(summary)

    def manual_set_progress_state(
        self, game_id: str, *, playthrough_id: str = "default", **fields: Any
    ) -> None:
        existing = self._mem.get_progress_state(game_id, playthrough_id)
        base = existing or GameProgressState(game_id=game_id, playthrough_id=playthrough_id)
        state = _merge_progress(base, fields)
        if not state.last_played_at:
            state = _merge_progress(state, {"last_played_at": utc_now_iso()})
        self._mem.upsert_progress_state(state)

    def manual_add_choice_event(
        self,
        game_id: str,
        options: Any = None,
        selected_option: Any = None,
        *,
        playthrough_id: str = "default",
        selection_source: str = "user_reported",
    ) -> str:
        normalized = _normalize_options(options)
        selected_index, selected_text = _resolve_selection(normalized, selected_option)
        choice = ChoiceEvent(
            choice_id=_new_id(),
            game_id=game_id,
            playthrough_id=playthrough_id,
            session_id=_manual_session_id(game_id, playthrough_id),
            timestamp=utc_now_iso(),
            options=normalized,
            selected_option_index=selected_index,
            selected_option_text=selected_text,
            # source only meaningful when a selection was actually reported.
            selection_source=(selection_source if selected_option is not None else None),
        )
        return self._mem.add_choice_event(choice)

    def manual_add_companion_beat(
        self, game_id: str, beat_type: str, content: str, *, playthrough_id: str = "default"
    ) -> str:
        beat = CompanionBeat(
            beat_id=_new_id(),
            game_id=game_id,
            playthrough_id=playthrough_id,
            session_id=_manual_session_id(game_id, playthrough_id),
            type=beat_type,
            content=content,
            source="user",
            created_at=utc_now_iso(),
            scope={
                "character_id": self._character_id,
                "user_id": self._user_id,
                "game_id": game_id,
            },
        )
        return self._mem.add_companion_beat(beat)
