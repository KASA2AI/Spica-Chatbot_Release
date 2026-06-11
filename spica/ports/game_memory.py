"""Galgame memory capability port (Phase 1).

The read/write surface over **committed** galgame data: game profiles, play
sessions, story lines, summaries, progress state, character relations, choice
events, companion beats. It is deliberately separate from ``MemoryPort`` /
``MemoryScope`` (CLAUDE.md #1.8) -- galgame storage must not pollute the existing
character long-term memory.

This port has NO knowledge of OCR, the live session FSM, or buffer derivation:
it persists and queries committed records only. The session owner (Phase 4+)
derives the working buffer from ``committed_story_lines`` + the summaries' source
ids; the gated stage (Phase 3) reads the committed snapshot through here.

Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from spica.galgame.models import (
    CharacterRelation,
    ChoiceEvent,
    CompanionBeat,
    GameProfile,
    GameProgressState,
    PlaySession,
    StoryLine,
    StoryLineStatus,
    StorySummary,
)


@runtime_checkable
class GameMemoryPort(Protocol):
    # -- game profile ---------------------------------------------------------
    def upsert_game_profile(self, profile: GameProfile) -> None: ...

    def get_game_profile(self, game_id: str) -> GameProfile | None: ...

    def last_played_game(self) -> GameProfile | None:
        """Most-recently played game (for offline 「昨天玩到哪了」, §15.2)."""
        ...

    # -- play session ---------------------------------------------------------
    def add_play_session(self, session: PlaySession) -> str: ...

    def update_play_session(self, session_id: str, **fields: Any) -> None: ...

    def get_play_session(self, session_id: str) -> PlaySession | None:
        """Read one PlaySession (for FSM->PlaySession projection checks / dangling
        recovery)."""
        ...

    def dangling_play_sessions(self) -> list[PlaySession]:
        """active/paused sessions with no ended_at (crash-recovery query, §12)."""
        ...

    # -- story lines ----------------------------------------------------------
    def add_story_line(self, line: StoryLine) -> str: ...

    def update_story_line_status(self, line_id: str, status: StoryLineStatus) -> None:
        """Advance a line's status; illegal transitions raise (via with_status)."""
        ...

    def committed_story_lines(self, game_id: str, playthrough_id: str = "default") -> list[StoryLine]:
        """Raw committed lines in timestamp order. The *unsummarized* buffer is
        derived by ``unsummarized_committed_story_lines``; the session's live
        working-set + snapshot concurrency are layered on in Phase 4/7/8."""
        ...

    def unsummarized_committed_story_lines(
        self, game_id: str, playthrough_id: str = "default"
    ) -> list[StoryLine]:
        """The Story buffer (§11): committed lines NOT covered by any
        ``StorySummary.source_line_ids``. Source of truth is the summaries
        themselves (reverse-lookup), so no per-line ``summarized`` flag exists.

        NOTE (Phase 7/8): the SQLite adapter unions source_line_ids across ALL
        summaries every call -- a full-history scan. Once real OCR accumulates
        large line counts, switch to an incremental marker / index table.
        """
        ...

    # -- summaries ------------------------------------------------------------
    def add_summary(self, summary: StorySummary) -> str: ...

    def recent_summaries(
        self, game_id: str, playthrough_id: str = "default", limit: int = 5
    ) -> list[StorySummary]: ...

    # -- progress state -------------------------------------------------------
    def upsert_progress_state(self, state: GameProgressState) -> None: ...

    def get_progress_state(
        self, game_id: str, playthrough_id: str = "default"
    ) -> GameProgressState | None: ...

    # -- character relations --------------------------------------------------
    def upsert_character_relation(self, relation: CharacterRelation) -> str: ...

    def character_relations(
        self, game_id: str, playthrough_id: str = "default"
    ) -> list[CharacterRelation]: ...

    # -- choice events --------------------------------------------------------
    def add_choice_event(self, choice: ChoiceEvent) -> str: ...

    def update_choice_event(self, choice_id: str, **fields: Any) -> None: ...

    def recent_choice_events(
        self, game_id: str, playthrough_id: str = "default", limit: int = 5
    ) -> list[ChoiceEvent]: ...

    # -- companion beats ------------------------------------------------------
    def add_companion_beat(self, beat: CompanionBeat) -> str: ...

    def companion_beats(
        self, game_id: str, user_id: str, character_id: str, limit: int = 10
    ) -> list[CompanionBeat]: ...
