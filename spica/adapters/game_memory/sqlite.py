"""SQLite galgame-memory adapter (Phase 1).

Implements ``GameMemoryPort`` over a **dedicated** SQLite file, separate from the
character long-term store (``memory.sqlite3``) so galgame data never pollutes the
existing ``MemoryScope`` (CLAUDE.md #1.8). Same style as
``spica/adapters/memory/sqlite.py`` / ``memory/store.py`` but its own schema.

Storage shape: one table per entity, with the queryable fields promoted to
columns (game_id / playthrough_id / ids / status / timestamps) and the full model
serialized as a JSON ``data`` column. Reads rehydrate via ``Model.from_dict`` so
round-trip is exact; queries filter on the indexed columns.

Path: injected via ``db_path`` (default the relative ``spica_data/galgame.sqlite3``).
This adapter NEVER reads env and NEVER hardcodes an absolute path (CLAUDE.md #4 /
test_no_getenv). Production wiring (Phase 4-5 host) passes
``_REPO_ROOT / "spica_data" / "galgame.sqlite3"`` -- same口径 as the memory store,
with the repo-root computation living in the host, not here.

Migration: ``PRAGMA user_version`` is stamped at init (see ``SCHEMA_VERSION``) for
a future v2 migration; ``schema_version()`` reads it back.

Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

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

SCHEMA_VERSION = 1

_DEFAULT_DB_PATH = "spica_data/galgame.sqlite3"


class GameMemorySqliteAdapter:
    name = "sqlite"

    def __init__(self, db_path: str | Path = _DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # -- infra ----------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # Review #6: OCR-loop line writes / background summaries / turn reads run
        # concurrently -- WAL keeps readers unblocked by writers; busy_timeout
        # pins Python's implicit 5s default as an explicit, testable contract.
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS game_profiles (
                    game_id TEXT PRIMARY KEY,
                    last_played_at TEXT,
                    data TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS play_sessions (
                    session_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    playthrough_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    started_at TEXT,
                    ended_at TEXT,
                    data TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS story_lines (
                    line_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    game_id TEXT NOT NULL,
                    playthrough_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    timestamp TEXT,
                    data TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS story_summaries (
                    summary_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    playthrough_id TEXT NOT NULL,
                    created_at TEXT,
                    data TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS progress_states (
                    game_id TEXT NOT NULL,
                    playthrough_id TEXT NOT NULL,
                    last_played_at TEXT,
                    data TEXT NOT NULL,
                    PRIMARY KEY (game_id, playthrough_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS character_relations (
                    relation_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    playthrough_id TEXT NOT NULL,
                    updated_at TEXT,
                    data TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS choice_events (
                    choice_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    playthrough_id TEXT NOT NULL,
                    timestamp TEXT,
                    data TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS companion_beats (
                    beat_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    user_id TEXT,
                    character_id TEXT,
                    created_at TEXT,
                    data TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_story_lines_lookup ON story_lines(game_id, playthrough_id, status, timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_summaries_lookup ON story_summaries(game_id, playthrough_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_choices_lookup ON choice_events(game_id, playthrough_id, timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_relations_lookup ON character_relations(game_id, playthrough_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_beats_lookup ON companion_beats(game_id, user_id, character_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_dangling ON play_sessions(state, ended_at)")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def schema_version(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    @staticmethod
    def _dump(model: Any) -> str:
        return json.dumps(model.to_dict(), ensure_ascii=False)

    # -- game profile ---------------------------------------------------------
    def upsert_game_profile(self, profile: GameProfile) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO game_profiles (game_id, last_played_at, data) VALUES (?, ?, ?)",
                (profile.game_id, profile.last_played_at, self._dump(profile)),
            )

    def get_game_profile(self, game_id: str) -> GameProfile | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM game_profiles WHERE game_id = ?", (game_id,)
            ).fetchone()
        return GameProfile.from_dict(json.loads(row["data"])) if row else None

    def last_played_game(self) -> GameProfile | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM game_profiles "
                "ORDER BY (last_played_at IS NULL), last_played_at DESC LIMIT 1"
            ).fetchone()
        return GameProfile.from_dict(json.loads(row["data"])) if row else None

    # -- play session ---------------------------------------------------------
    def add_play_session(self, session: PlaySession) -> str:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO play_sessions "
                "(session_id, game_id, playthrough_id, state, started_at, ended_at, data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session.session_id,
                    session.game_id,
                    session.playthrough_id,
                    session.state,
                    session.started_at,
                    session.ended_at,
                    self._dump(session),
                ),
            )
        return session.session_id

    def update_play_session(self, session_id: str, **fields: Any) -> None:
        current = self._get_play_session(session_id)
        if current is None:
            raise KeyError(f"play session not found: {session_id!r}")
        updated = self._replace(current, fields)
        with self._connect() as conn:
            conn.execute(
                "UPDATE play_sessions SET game_id = ?, playthrough_id = ?, state = ?, "
                "started_at = ?, ended_at = ?, data = ? WHERE session_id = ?",
                (
                    updated.game_id,
                    updated.playthrough_id,
                    updated.state,
                    updated.started_at,
                    updated.ended_at,
                    self._dump(updated),
                    session_id,
                ),
            )

    def get_play_session(self, session_id: str) -> PlaySession | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM play_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return PlaySession.from_dict(json.loads(row["data"])) if row else None

    # Internal alias kept for the in-module callers (update_play_session).
    _get_play_session = get_play_session

    def dangling_play_sessions(self) -> list[PlaySession]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM play_sessions "
                "WHERE state IN ('active', 'paused') AND ended_at IS NULL "
                "ORDER BY started_at"
            ).fetchall()
        return [PlaySession.from_dict(json.loads(row["data"])) for row in rows]

    # -- story lines ----------------------------------------------------------
    def add_story_line(self, line: StoryLine) -> str:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO story_lines "
                "(line_id, session_id, game_id, playthrough_id, status, timestamp, data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    line.line_id,
                    line.session_id,
                    line.game_id,
                    line.playthrough_id,
                    line.status.value,
                    line.timestamp,
                    self._dump(line),
                ),
            )
        return line.line_id

    def update_story_line_status(self, line_id: str, status: StoryLineStatus) -> None:
        current = self._get_story_line(line_id)
        if current is None:
            raise KeyError(f"story line not found: {line_id!r}")
        # Route through with_status so illegal transitions raise (not silent).
        updated = current.with_status(status)
        with self._connect() as conn:
            conn.execute(
                "UPDATE story_lines SET status = ?, data = ? WHERE line_id = ?",
                (updated.status.value, self._dump(updated), line_id),
            )

    def _get_story_line(self, line_id: str) -> StoryLine | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM story_lines WHERE line_id = ?", (line_id,)
            ).fetchone()
        return StoryLine.from_dict(json.loads(row["data"])) if row else None

    def committed_story_lines(self, game_id: str, playthrough_id: str = "default") -> list[StoryLine]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM story_lines "
                "WHERE game_id = ? AND playthrough_id = ? AND status = ? "
                "ORDER BY timestamp, line_id",
                (game_id, playthrough_id, StoryLineStatus.COMMITTED.value),
            ).fetchall()
        return [StoryLine.from_dict(json.loads(row["data"])) for row in rows]

    def unsummarized_committed_story_lines(
        self, game_id: str, playthrough_id: str = "default"
    ) -> list[StoryLine]:
        summarized = self._summarized_line_ids(game_id, playthrough_id)
        return [
            line
            for line in self.committed_story_lines(game_id, playthrough_id)
            if line.line_id not in summarized
        ]

    def _summarized_line_ids(self, game_id: str, playthrough_id: str) -> set[str]:
        # NOTE (Phase 7/8): unions source_line_ids across ALL summaries of this
        # game/playthrough on every call -- a full-history scan. Fine at Phase 2
        # hand-fed scale; once real OCR accumulates many lines/summaries, switch to
        # an incremental marker or a (summary_id, line_id) index table.
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM story_summaries WHERE game_id = ? AND playthrough_id = ?",
                (game_id, playthrough_id),
            ).fetchall()
        ids: set[str] = set()
        for row in rows:
            ids.update(json.loads(row["data"]).get("source_line_ids") or [])
        return ids

    # -- summaries ------------------------------------------------------------
    def add_summary(self, summary: StorySummary) -> str:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO story_summaries "
                "(summary_id, game_id, playthrough_id, created_at, data) VALUES (?, ?, ?, ?, ?)",
                (
                    summary.summary_id,
                    summary.game_id,
                    summary.playthrough_id,
                    summary.created_at,
                    self._dump(summary),
                ),
            )
        return summary.summary_id

    def recent_summaries(
        self, game_id: str, playthrough_id: str = "default", limit: int = 5
    ) -> list[StorySummary]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM story_summaries "
                "WHERE game_id = ? AND playthrough_id = ? "
                "ORDER BY created_at DESC, summary_id DESC LIMIT ?",
                (game_id, playthrough_id, max(1, int(limit))),
            ).fetchall()
        return [StorySummary.from_dict(json.loads(row["data"])) for row in rows]

    # -- progress state -------------------------------------------------------
    def upsert_progress_state(self, state: GameProgressState) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO progress_states "
                "(game_id, playthrough_id, last_played_at, data) VALUES (?, ?, ?, ?)",
                (state.game_id, state.playthrough_id, state.last_played_at, self._dump(state)),
            )

    def get_progress_state(
        self, game_id: str, playthrough_id: str = "default"
    ) -> GameProgressState | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM progress_states WHERE game_id = ? AND playthrough_id = ?",
                (game_id, playthrough_id),
            ).fetchone()
        return GameProgressState.from_dict(json.loads(row["data"])) if row else None

    # -- character relations --------------------------------------------------
    def upsert_character_relation(self, relation: CharacterRelation) -> str:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO character_relations "
                "(relation_id, game_id, playthrough_id, updated_at, data) VALUES (?, ?, ?, ?, ?)",
                (
                    relation.relation_id,
                    relation.game_id,
                    relation.playthrough_id,
                    relation.updated_at,
                    self._dump(relation),
                ),
            )
        return relation.relation_id

    def character_relations(
        self, game_id: str, playthrough_id: str = "default"
    ) -> list[CharacterRelation]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM character_relations "
                "WHERE game_id = ? AND playthrough_id = ? ORDER BY updated_at DESC, relation_id",
                (game_id, playthrough_id),
            ).fetchall()
        return [CharacterRelation.from_dict(json.loads(row["data"])) for row in rows]

    # -- choice events --------------------------------------------------------
    def add_choice_event(self, choice: ChoiceEvent) -> str:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO choice_events "
                "(choice_id, game_id, playthrough_id, timestamp, data) VALUES (?, ?, ?, ?, ?)",
                (
                    choice.choice_id,
                    choice.game_id,
                    choice.playthrough_id,
                    choice.timestamp,
                    self._dump(choice),
                ),
            )
        return choice.choice_id

    def update_choice_event(self, choice_id: str, **fields: Any) -> None:
        current = self._get_choice_event(choice_id)
        if current is None:
            raise KeyError(f"choice event not found: {choice_id!r}")
        updated = self._replace(current, fields)
        with self._connect() as conn:
            conn.execute(
                "UPDATE choice_events SET game_id = ?, playthrough_id = ?, timestamp = ?, data = ? "
                "WHERE choice_id = ?",
                (updated.game_id, updated.playthrough_id, updated.timestamp, self._dump(updated), choice_id),
            )

    def _get_choice_event(self, choice_id: str) -> ChoiceEvent | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM choice_events WHERE choice_id = ?", (choice_id,)
            ).fetchone()
        return ChoiceEvent.from_dict(json.loads(row["data"])) if row else None

    def recent_choice_events(
        self, game_id: str, playthrough_id: str = "default", limit: int = 5
    ) -> list[ChoiceEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM choice_events "
                "WHERE game_id = ? AND playthrough_id = ? "
                "ORDER BY timestamp DESC, choice_id DESC LIMIT ?",
                (game_id, playthrough_id, max(1, int(limit))),
            ).fetchall()
        return [ChoiceEvent.from_dict(json.loads(row["data"])) for row in rows]

    # -- companion beats ------------------------------------------------------
    def add_companion_beat(self, beat: CompanionBeat) -> str:
        scope = beat.scope if isinstance(beat.scope, dict) else {}
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO companion_beats "
                "(beat_id, game_id, user_id, character_id, created_at, data) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    beat.beat_id,
                    beat.game_id,
                    scope.get("user_id"),
                    scope.get("character_id"),
                    beat.created_at,
                    self._dump(beat),
                ),
            )
        return beat.beat_id

    def companion_beats(
        self, game_id: str, user_id: str, character_id: str, limit: int = 10
    ) -> list[CompanionBeat]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM companion_beats "
                "WHERE game_id = ? AND user_id = ? AND character_id = ? "
                "ORDER BY created_at DESC, beat_id DESC LIMIT ?",
                (game_id, user_id, character_id, max(1, int(limit))),
            ).fetchall()
        return [CompanionBeat.from_dict(json.loads(row["data"])) for row in rows]

    # P5 D-P5-6: TWO read paths over the same table. Prompt injection EXCLUDES
    # silent reaction beats (NO_COMMENT / busy_drop accrue much faster than real
    # speech and would crowd her spoken words out of [COMPANION_CONTEXT]); the
    # similarity dedupe INCLUDES them (a swallowed scene is still a seen scene).
    # silent/source live in the JSON blob, so filter in Python over an over-fetch.
    _BEAT_FILTER_FETCH = 50

    def recent_companion_beats_for_prompt(
        self, game_id: str, user_id: str, character_id: str, limit: int = 10
    ) -> list[CompanionBeat]:
        beats = self.companion_beats(game_id, user_id, character_id, limit=self._BEAT_FILTER_FETCH)
        visible = [b for b in beats if not (b.meta or {}).get("silent")]
        return visible[: max(1, int(limit))]

    def recent_reaction_beats_for_dedupe(
        self, game_id: str, user_id: str, character_id: str, limit: int = 10
    ) -> list[CompanionBeat]:
        beats = self.companion_beats(game_id, user_id, character_id, limit=self._BEAT_FILTER_FETCH)
        reactions = [b for b in beats if b.source == "spica"]
        return reactions[: max(1, int(limit))]

    # -- helpers --------------------------------------------------------------
    @staticmethod
    def _replace(model: Any, fields: dict[str, Any]) -> Any:
        import dataclasses

        if not fields:
            return model
        valid = {f.name for f in dataclasses.fields(model)}
        unknown = set(fields) - valid
        if unknown:
            raise TypeError(f"unknown fields for {type(model).__name__}: {sorted(unknown)}")
        return dataclasses.replace(model, **fields)
