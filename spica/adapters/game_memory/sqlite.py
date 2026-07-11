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

Migration (AR-C0): ``character_relations`` durable identity is the composite
``(game_id, playthrough_id, relation_id)`` -- ``SCHEMA_VERSION = 2``. Adapter
construction dispatches on ``PRAGMA user_version`` + actual table shape
(fresh / legacy v0 / legacy v1 / current v2 / future); legacy DBs are rebuilt
transactionally after a WAL-safe backup. Unconditional version stamping is
gone: only a successful fresh create or migration stamps 2, every ambiguous or
future state fails loud (``GameMemoryMigrationError``) without modifying the
DB's logical state. ``schema_version()`` reads the stamp back.

Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

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

SCHEMA_VERSION = 2

_DEFAULT_DB_PATH = "spica_data/galgame.sqlite3"

# Canonical relation-table shapes as (name, type, notnull, pk) per PRAGMA
# table_info. v1's TEXT PRIMARY KEY is notnull=0 (rowid-table historical
# semantics); v2 declares NOT NULL on every PK column.
_V1_RELATIONS_SHAPE = (
    ("relation_id", "TEXT", 0, 1),
    ("game_id", "TEXT", 1, 0),
    ("playthrough_id", "TEXT", 1, 0),
    ("updated_at", "TEXT", 0, 0),
    ("data", "TEXT", 1, 0),
)
_V2_RELATIONS_SHAPE = (
    ("relation_id", "TEXT", 1, 3),
    ("game_id", "TEXT", 1, 1),
    ("playthrough_id", "TEXT", 1, 2),
    ("updated_at", "TEXT", 0, 0),
    ("data", "TEXT", 1, 0),
)

# Exact relation endpoint contracts. These are separate literals for v1/v2 and
# cover all three SQLite introspection lenses required by AR-C0: table_xinfo,
# index_list (unique/origin/partial), and index_xinfo (order/DESC/collation/key).
_V1_RELATIONS_TABLE_XINFO = (
    (0, "relation_id", "TEXT", 0, None, 1, 0),
    (1, "game_id", "TEXT", 1, None, 0, 0),
    (2, "playthrough_id", "TEXT", 1, None, 0, 0),
    (3, "updated_at", "TEXT", 0, None, 0, 0),
    (4, "data", "TEXT", 1, None, 0, 0),
)
_V2_RELATIONS_TABLE_XINFO = (
    (0, "relation_id", "TEXT", 1, None, 3, 0),
    (1, "game_id", "TEXT", 1, None, 1, 0),
    (2, "playthrough_id", "TEXT", 1, None, 2, 0),
    (3, "updated_at", "TEXT", 0, None, 0, 0),
    (4, "data", "TEXT", 1, None, 0, 0),
)
_RELATIONS_INDEX_LIST = (
    ("idx_relations_lookup", 0, "c", 0),
    ("sqlite_autoindex_character_relations_1", 1, "pk", 0),
)
_V1_RELATIONS_INDEX_XINFO = {
    "idx_relations_lookup": (
        (0, 1, "game_id", 0, "BINARY", 1),
        (1, 2, "playthrough_id", 0, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0),
    ),
    "sqlite_autoindex_character_relations_1": (
        (0, 0, "relation_id", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    ),
}
_V2_RELATIONS_INDEX_XINFO = {
    "idx_relations_lookup": (
        (0, 1, "game_id", 0, "BINARY", 1),
        (1, 2, "playthrough_id", 0, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0),
    ),
    "sqlite_autoindex_character_relations_1": (
        (0, 1, "game_id", 0, "BINARY", 1),
        (1, 2, "playthrough_id", 0, "BINARY", 1),
        (2, 0, "relation_id", 0, "BINARY", 1),
        (3, -1, None, 0, "BINARY", 0),
    ),
}

# Complete legacy-v1 business schema used to recognize unstamped/non-empty v0
# databases. Values are independent canonical literals normalized from
# ``PRAGMA table_xinfo`` as (name, type, notnull, default, pk, hidden). Extra
# user tables remain allowed/preserved, but every one of the eight v1 business
# tables and every canonical index on them must match exactly.
_LEGACY_V1_TABLE_XINFO = {
    "game_profiles": (
        ("game_id", "TEXT", 0, None, 1, 0),
        ("last_played_at", "TEXT", 0, None, 0, 0),
        ("data", "TEXT", 1, None, 0, 0),
    ),
    "play_sessions": (
        ("session_id", "TEXT", 0, None, 1, 0),
        ("game_id", "TEXT", 1, None, 0, 0),
        ("playthrough_id", "TEXT", 1, None, 0, 0),
        ("state", "TEXT", 1, None, 0, 0),
        ("started_at", "TEXT", 0, None, 0, 0),
        ("ended_at", "TEXT", 0, None, 0, 0),
        ("data", "TEXT", 1, None, 0, 0),
    ),
    "story_lines": (
        ("line_id", "TEXT", 0, None, 1, 0),
        ("session_id", "TEXT", 1, None, 0, 0),
        ("game_id", "TEXT", 1, None, 0, 0),
        ("playthrough_id", "TEXT", 1, None, 0, 0),
        ("status", "TEXT", 1, None, 0, 0),
        ("timestamp", "TEXT", 0, None, 0, 0),
        ("data", "TEXT", 1, None, 0, 0),
    ),
    "story_summaries": (
        ("summary_id", "TEXT", 0, None, 1, 0),
        ("game_id", "TEXT", 1, None, 0, 0),
        ("playthrough_id", "TEXT", 1, None, 0, 0),
        ("created_at", "TEXT", 0, None, 0, 0),
        ("data", "TEXT", 1, None, 0, 0),
    ),
    "progress_states": (
        ("game_id", "TEXT", 1, None, 1, 0),
        ("playthrough_id", "TEXT", 1, None, 2, 0),
        ("last_played_at", "TEXT", 0, None, 0, 0),
        ("data", "TEXT", 1, None, 0, 0),
    ),
    "character_relations": (
        ("relation_id", "TEXT", 0, None, 1, 0),
        ("game_id", "TEXT", 1, None, 0, 0),
        ("playthrough_id", "TEXT", 1, None, 0, 0),
        ("updated_at", "TEXT", 0, None, 0, 0),
        ("data", "TEXT", 1, None, 0, 0),
    ),
    "choice_events": (
        ("choice_id", "TEXT", 0, None, 1, 0),
        ("game_id", "TEXT", 1, None, 0, 0),
        ("playthrough_id", "TEXT", 1, None, 0, 0),
        ("timestamp", "TEXT", 0, None, 0, 0),
        ("data", "TEXT", 1, None, 0, 0),
    ),
    "companion_beats": (
        ("beat_id", "TEXT", 0, None, 1, 0),
        ("game_id", "TEXT", 1, None, 0, 0),
        ("user_id", "TEXT", 0, None, 0, 0),
        ("character_id", "TEXT", 0, None, 0, 0),
        ("created_at", "TEXT", 0, None, 0, 0),
        ("data", "TEXT", 1, None, 0, 0),
    ),
}

# DDL fingerprints close the gap left by SQLite's PRAGMA introspection: CHECK,
# FOREIGN KEY and collation on an unindexed column are not represented by
# table_xinfo/index_list/index_xinfo. These literals are the eight BASE v1
# business tables; optional IF NOT EXISTS and SQL spelling trivia are normalized
# token-by-token below, while every actual constraint token remains significant.
_LEGACY_V1_TABLE_DDL = {
    "game_profiles": """CREATE TABLE game_profiles (
        game_id TEXT PRIMARY KEY,
        last_played_at TEXT,
        data TEXT NOT NULL
    )""",
    "play_sessions": """CREATE TABLE play_sessions (
        session_id TEXT PRIMARY KEY,
        game_id TEXT NOT NULL,
        playthrough_id TEXT NOT NULL,
        state TEXT NOT NULL,
        started_at TEXT,
        ended_at TEXT,
        data TEXT NOT NULL
    )""",
    "story_lines": """CREATE TABLE story_lines (
        line_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        game_id TEXT NOT NULL,
        playthrough_id TEXT NOT NULL,
        status TEXT NOT NULL,
        timestamp TEXT,
        data TEXT NOT NULL
    )""",
    "story_summaries": """CREATE TABLE story_summaries (
        summary_id TEXT PRIMARY KEY,
        game_id TEXT NOT NULL,
        playthrough_id TEXT NOT NULL,
        created_at TEXT,
        data TEXT NOT NULL
    )""",
    "progress_states": """CREATE TABLE progress_states (
        game_id TEXT NOT NULL,
        playthrough_id TEXT NOT NULL,
        last_played_at TEXT,
        data TEXT NOT NULL,
        PRIMARY KEY (game_id, playthrough_id)
    )""",
    "character_relations": """CREATE TABLE character_relations (
        relation_id TEXT PRIMARY KEY,
        game_id TEXT NOT NULL,
        playthrough_id TEXT NOT NULL,
        updated_at TEXT,
        data TEXT NOT NULL
    )""",
    "choice_events": """CREATE TABLE choice_events (
        choice_id TEXT PRIMARY KEY,
        game_id TEXT NOT NULL,
        playthrough_id TEXT NOT NULL,
        timestamp TEXT,
        data TEXT NOT NULL
    )""",
    "companion_beats": """CREATE TABLE companion_beats (
        beat_id TEXT PRIMARY KEY,
        game_id TEXT NOT NULL,
        user_id TEXT,
        character_id TEXT,
        created_at TEXT,
        data TEXT NOT NULL
    )""",
}

# Canonical named indexes per required legacy table. PK autoindexes are derived
# from the independent table_xinfo literals above and checked alongside these.
_LEGACY_V1_NAMED_INDEX_COLUMNS = {
    "game_profiles": {},
    "play_sessions": {"idx_sessions_dangling": ("state", "ended_at")},
    "story_lines": {
        "idx_story_lines_lookup":
            ("game_id", "playthrough_id", "status", "timestamp"),
    },
    "story_summaries": {
        "idx_summaries_lookup": ("game_id", "playthrough_id", "created_at"),
    },
    "progress_states": {},
    "character_relations": {
        "idx_relations_lookup": ("game_id", "playthrough_id"),
    },
    "choice_events": {
        "idx_choices_lookup": ("game_id", "playthrough_id", "timestamp"),
    },
    "companion_beats": {
        "idx_beats_lookup":
            ("game_id", "user_id", "character_id", "created_at"),
    },
}

_V2_RELATIONS_DDL = """
    CREATE TABLE {table} (
        relation_id TEXT NOT NULL,
        game_id TEXT NOT NULL,
        playthrough_id TEXT NOT NULL,
        updated_at TEXT,
        data TEXT NOT NULL,
        PRIMARY KEY (game_id, playthrough_id, relation_id)
    )
"""

# Shared projection DML (AR-C1 §4.2): the single methods (add_summary /
# upsert_progress_state / upsert_character_relation) and the atomic
# apply_summary_projection command execute these SAME constants -- row semantics
# structurally cannot drift between the two write paths.
_SUMMARY_INSERT_DML = (
    "INSERT OR REPLACE INTO story_summaries "
    "(summary_id, game_id, playthrough_id, created_at, data) VALUES (?, ?, ?, ?, ?)"
)
_PROGRESS_UPSERT_DML = (
    "INSERT OR REPLACE INTO progress_states "
    "(game_id, playthrough_id, last_played_at, data) VALUES (?, ?, ?, ?)"
)
_RELATION_UPSERT_DML = (
    # AR-C0: scoped conflict update -- same scope rewrites its own row, other
    # scopes are never touched (INSERT OR REPLACE deleted cross-scope rows).
    "INSERT INTO character_relations "
    "(relation_id, game_id, playthrough_id, updated_at, data) VALUES (?, ?, ?, ?, ?) "
    "ON CONFLICT (game_id, playthrough_id, relation_id) "
    "DO UPDATE SET updated_at = excluded.updated_at, data = excluded.data"
)

_SQLITE_DDL_WHITESPACE = " \t\n\f\r"
_CANONICAL_DDL_KEYWORDS = frozenset({
    "CREATE", "TABLE", "IF", "NOT", "EXISTS", "PRIMARY", "KEY", "NULL",
})


def _sqlite_ascii_upper(value: str) -> str:
    """SQLite identifier case folding is ASCII-only, unlike str.upper()."""
    return "".join(
        chr(ord(char) - 32) if "a" <= char <= "z" else char
        for char in value)


def _quote_sqlite_identifier(identifier: str) -> str:
    """Quote a sqlite_master name for use in SQL identifier position."""
    return '"' + identifier.replace('"', '""') + '"'


def _sql_ddl_fingerprint(sql: str) -> tuple[tuple[str, str], ...]:
    """Token-aware SQLite DDL fingerprint; never rewrites quoted content.

    Whitespace/comments and identifier quoting/case are spelling trivia. String
    literals remain distinct tokens, so spaces, comment markers and escaped
    quotes inside them cannot be mistaken for syntax. BASE's idempotent creator
    used ``CREATE TABLE IF NOT EXISTS``; sqlite_master also quotes a table name
    after ALTER TABLE RENAME, so those two harmless forms normalize explicitly.
    """
    tokens: list[tuple[str, str]] = []
    length = len(sql)
    index = 0

    while index < length:
        char = sql[index]
        if char in _SQLITE_DDL_WHITESPACE:
            index += 1
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = length if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                raise ValueError("unterminated SQL block comment")
            index = end + 2
            continue
        if char == "'":
            index += 1
            value = []
            while index < length:
                if sql[index] == "'":
                    if index + 1 < length and sql[index + 1] == "'":
                        value.append("'")
                        index += 2
                        continue
                    index += 1
                    break
                value.append(sql[index])
                index += 1
            else:
                raise ValueError("unterminated SQL string literal")
            tokens.append(("string", "".join(value)))
            continue
        if char in ('"', "`", "["):
            close = "]" if char == "[" else char
            index += 1
            value = []
            while index < length:
                if sql[index] == close:
                    if index + 1 < length and sql[index + 1] == close:
                        value.append(close)
                        index += 2
                        continue
                    index += 1
                    break
                value.append(sql[index])
                index += 1
            else:
                raise ValueError("unterminated SQL quoted identifier")
            tokens.append(("identifier", _sqlite_ascii_upper("".join(value))))
            continue
        if char.isalnum() or char in "_$":
            start = index
            index += 1
            while index < length and (
                    sql[index].isalnum() or sql[index] in "_$"):
                index += 1
            value = _sqlite_ascii_upper(sql[start:index])
            kind = (
                "keyword" if value in _CANONICAL_DDL_KEYWORDS
                else "number" if sql[start].isdigit()
                else "identifier")
            tokens.append((kind, value))
            continue
        tokens.append(("symbol", char))
        index += 1

    while tokens and tokens[-1] == ("symbol", ";"):
        tokens.pop()
    create_if_missing = [
        ("keyword", "CREATE"),
        ("keyword", "TABLE"),
        ("keyword", "IF"),
        ("keyword", "NOT"),
        ("keyword", "EXISTS"),
    ]
    if tokens[:5] == create_if_missing:
        del tokens[2:5]
    return tuple(tokens)


class GameMemoryMigrationError(RuntimeError):
    """Fail-loud signal for every non-migratable DB state (AR-C0 §4)."""


class GameMemorySqliteAdapter:
    name = "sqlite"

    def __init__(self, db_path: str | Path = _DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Migration runs at construction, before any other connection is handed
        # out (single-process single-writer contract, §5.0).
        self._ensure_schema()

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

    # -- schema version dispatch (AR-C0) ---------------------------------------
    def _ensure_schema(self) -> None:
        """Dispatch on user_version + actual relation-table shape (§4 matrix).

        fresh -> create v2; exact legacy v1 shape (stamped 0 or 1) -> migrate;
        current v2 -> verify shape and open idempotently (zero writes); future
        version / any version-shape contradiction -> fail loud without touching
        the DB's logical state.
        """
        self._stale_tmp_gate()
        if not self.db_path.exists():
            self._create_fresh_v2()
            return
        self._recover_rollback_journal()
        insp = self._open_ro(self.db_path)
        try:
            version = int(insp.execute("PRAGMA user_version").fetchone()[0])
            tables = self._business_tables(insp)
            has_relations = "character_relations" in tables
            shape = self._relations_shape(insp) if has_relations else None
            v2_endpoint_ok = (
                self._relations_endpoint_ok(
                    insp, _V2_RELATIONS_TABLE_XINFO,
                    _V2_RELATIONS_INDEX_XINFO)
                and self._table_ddl_ok(
                    insp, "character_relations",
                    _V2_RELATIONS_DDL.format(table="character_relations")))
            legacy_v1_ok = self._legacy_v1_schema_ok(insp)
        finally:
            insp.close()

        if version == 0 and not tables:
            self._create_fresh_v2()
            return
        if version > SCHEMA_VERSION:
            raise GameMemoryMigrationError(
                f"{self.db_path}: user_version={version} is newer than this build's "
                f"SCHEMA_VERSION={SCHEMA_VERSION}; refusing to downgrade or stamp. "
                "Run a matching Spica build or restore a compatible backup.")
        if version == SCHEMA_VERSION:
            if shape == _V2_RELATIONS_SHAPE and v2_endpoint_ok:
                return  # idempotent open: no writes at all
            raise GameMemoryMigrationError(
                f"{self.db_path}: user_version=2 but character_relations does not "
                "match the v2 composite-PK shape; refusing to guess. Restore a "
                "backup or repair manually.")
        # version in (0-with-tables, 1): only an exact legacy-v1 shape migrates.
        if shape == _V1_RELATIONS_SHAPE and legacy_v1_ok:
            self._migrate_to_v2(source_version=version)
            return
        raise GameMemoryMigrationError(
            f"{self.db_path}: user_version={version} does not match the complete "
            "legacy-v1 business table/index contract; refusing to migrate. "
            "Restore a backup or repair manually.")

    def _recover_rollback_journal(self) -> None:
        """Let SQLite recover a crash-hot rollback journal before ro inspection.

        A ``mode=ro`` connection cannot perform mandatory rollback and fails
        with ``attempt to write a readonly database``. Opening the real DB
        writable is correct here (unlike restore preflight, which works on a
        staging copy): this is normal startup crash recovery and restores the
        last committed logical state. Do it only when the rollback sidecar is
        present, so ordinary/future-version inspection remains read-only.
        """
        journal = Path(str(self.db_path) + "-journal")
        if not journal.exists():
            return
        conn = sqlite3.connect(self.db_path)
        try:
            # Reading schema metadata forces SQLite to resolve a hot journal.
            conn.execute("PRAGMA user_version").fetchone()
        finally:
            conn.close()

    def _stale_tmp_gate(self) -> None:
        """§5.1.7 startup gate: a leftover tmp artifact family means a crashed
        migration. Fail loud, keep the scene, never auto-clean or reuse.

        Only ``os.path.samefile(tmp, final)`` (post-``os.link`` shared inode)
        proves post-publication residue that is safe to delete manually; a
        same-named final that is a DIFFERENT file must not be presumed (#34).
        """
        backups_dir = self.db_path.parent / "backups"
        if not backups_dir.is_dir():
            return
        suffixes = (".pre-arc0.tmp", ".pre-arc0.tmp-wal", ".pre-arc0.tmp-shm",
                    ".pre-arc0.tmp-journal")
        members = sorted(p for p in backups_dir.iterdir()
                         if p.name.endswith(suffixes))
        if not members:
            return
        names = ", ".join(str(p) for p in members)
        base_tmps = [p for p in members if p.name.endswith(".pre-arc0.tmp")]
        published = []
        for tmp in base_tmps:
            final = tmp.with_name(tmp.name[: -len(".tmp")] + ".bak")
            if final.exists() and os.path.samefile(tmp, final):
                published.append(final)
        if base_tmps and len(published) == len(base_tmps):
            finals = ", ".join(str(f) for f in published)
            raise GameMemoryMigrationError(
                f"stale backup tmp family found after a completed publication: "
                f"{names}. The published backup(s) {finals} share the same inode "
                "and are intact; the leftover tmp family may be safely deleted "
                "manually. Not auto-deleting; delete it and restart.")
        hints = []
        for tmp in base_tmps:
            final = tmp.with_name(tmp.name[: -len(".tmp")] + ".bak")
            if final.exists() and not os.path.samefile(tmp, final):
                hints.append(
                    f"{tmp.name} and {final.name} are DIFFERENT files -- do not "
                    "assume either is disposable")
        raise GameMemoryMigrationError(
            f"stale pre-migration backup tmp family found: {names}. A previous "
            "migration crashed mid-backup. Inspect, then quarantine or delete "
            "these files manually before restarting; tmp members are never valid "
            "restore candidates." + (" " + "; ".join(hints) + "." if hints else ""))

    @staticmethod
    def _open_ro(path: str | Path) -> sqlite3.Connection:
        """Read-only inspection/verify connection (§5.0 discipline).

        No write-capable PRAGMAs here, and never ``immutable=1`` -- it would
        skip un-checkpointed WAL content. The WAL-flipping ``_connect()`` must
        never be used on inspection paths.
        """
        uri = f"file:{quote(str(Path(path).resolve()), safe='/')}?mode=ro"
        return sqlite3.connect(uri, uri=True)

    @staticmethod
    def _business_tables(conn: sqlite3.Connection) -> set[str]:
        # GLOB, not LIKE: LIKE's '_' is a single-char wildcard and would also
        # exclude legitimate 'sqliteX...' user tables (§10 enumeration rule).
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'"
        ).fetchall()
        return {row[0] for row in rows}

    @staticmethod
    def _relations_shape(conn: sqlite3.Connection) -> tuple:
        rows = conn.execute("PRAGMA table_info(character_relations)").fetchall()
        return tuple(
            (row[1], (row[2] or "").upper(), int(row[3]), int(row[5])) for row in rows)

    @staticmethod
    def _table_xinfo_shape(conn: sqlite3.Connection, table: str) -> tuple:
        rows = conn.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
        return tuple(
            (row[1], (row[2] or "").upper(), int(row[3]), row[4],
             int(row[5]), int(row[6]))
            for row in rows)

    @staticmethod
    def _index_xinfo_shape(conn: sqlite3.Connection, index: str) -> tuple:
        rows = conn.execute(f'PRAGMA index_xinfo("{index}")').fetchall()
        return tuple(
            (row[2], int(row[3]), row[4], int(row[5])) for row in rows)

    @classmethod
    def _legacy_v1_schema_ok(cls, conn: sqlite3.Connection) -> bool:
        """Recognize the complete canonical v1 schema without rebuilding it."""
        if not cls._relations_endpoint_ok(
                conn, _V1_RELATIONS_TABLE_XINFO, _V1_RELATIONS_INDEX_XINFO):
            return False
        tables = cls._business_tables(conn)
        if not set(_LEGACY_V1_TABLE_XINFO).issubset(tables):
            return False
        for table, expected_table in _LEGACY_V1_TABLE_XINFO.items():
            if cls._table_xinfo_shape(conn, table) != expected_table:
                return False
            if not cls._table_ddl_ok(
                    conn, table, _LEGACY_V1_TABLE_DDL[table]):
                return False

            expected_indexes = {
                name: ((0, "c", 0), cls._rowid_index_xinfo(columns))
                for name, columns in _LEGACY_V1_NAMED_INDEX_COLUMNS[table].items()
            }
            pk_columns = tuple(
                row[0] for row in sorted(
                    (item for item in expected_table if item[4]),
                    key=lambda item: item[4]))
            if pk_columns:
                expected_indexes[f"sqlite_autoindex_{table}_1"] = (
                    (1, "pk", 0), cls._rowid_index_xinfo(pk_columns))

            actual_indexes = {
                row[1]: (int(row[2]), row[3], int(row[4]))
                for row in conn.execute(f'PRAGMA index_list("{table}")').fetchall()
            }
            if set(actual_indexes) != set(expected_indexes):
                return False
            for name, (metadata, xinfo) in expected_indexes.items():
                if actual_indexes[name] != metadata:
                    return False
                if cls._index_xinfo_shape(conn, name) != xinfo:
                    return False
        return True

    @staticmethod
    def _table_ddl_ok(conn: sqlite3.Connection, table: str,
                      expected_sql: str) -> bool:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        if row is None or row[0] is None:
            return False
        try:
            return _sql_ddl_fingerprint(row[0]) == _sql_ddl_fingerprint(expected_sql)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _rowid_index_xinfo(columns: tuple[str, ...]) -> tuple:
        return tuple((column, 0, "BINARY", 1) for column in columns) + (
            (None, 0, "BINARY", 0),)

    @staticmethod
    def _relations_endpoint_ok(
            conn: sqlite3.Connection, expected_table_xinfo: tuple,
            expected_index_xinfo: dict[str, tuple]) -> bool:
        table_xinfo = tuple(tuple(row) for row in conn.execute(
            "PRAGMA table_xinfo(character_relations)").fetchall())
        if table_xinfo != expected_table_xinfo:
            return False
        index_list = tuple(sorted(
            (row[1], int(row[2]), row[3], int(row[4]))
            for row in conn.execute(
                "PRAGMA index_list(character_relations)").fetchall()))
        if index_list != _RELATIONS_INDEX_LIST:
            return False
        for name, expected in expected_index_xinfo.items():
            actual = tuple(tuple(row) for row in conn.execute(
                f'PRAGMA index_xinfo("{name}")').fetchall())
            if actual != expected:
                return False
        return True

    def _create_fresh_v2(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE game_profiles (
                    game_id TEXT PRIMARY KEY,
                    last_played_at TEXT,
                    data TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE play_sessions (
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
                CREATE TABLE story_lines (
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
                CREATE TABLE story_summaries (
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
                CREATE TABLE progress_states (
                    game_id TEXT NOT NULL,
                    playthrough_id TEXT NOT NULL,
                    last_played_at TEXT,
                    data TEXT NOT NULL,
                    PRIMARY KEY (game_id, playthrough_id)
                )
                """
            )
            # AR-C0: durable identity is the composite scope -- relation_id is a
            # scope-local id (session derives it from character names alone), so
            # a global PK would silently overwrite across games/playthroughs.
            conn.execute(_V2_RELATIONS_DDL.format(table="character_relations"))
            conn.execute(
                """
                CREATE TABLE choice_events (
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
                CREATE TABLE companion_beats (
                    beat_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    user_id TEXT,
                    character_id TEXT,
                    created_at TEXT,
                    data TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX idx_story_lines_lookup ON story_lines(game_id, playthrough_id, status, timestamp)")
            conn.execute("CREATE INDEX idx_summaries_lookup ON story_summaries(game_id, playthrough_id, created_at)")
            conn.execute("CREATE INDEX idx_choices_lookup ON choice_events(game_id, playthrough_id, timestamp)")
            conn.execute("CREATE INDEX idx_relations_lookup ON character_relations(game_id, playthrough_id)")
            conn.execute("CREATE INDEX idx_beats_lookup ON companion_beats(game_id, user_id, character_id, created_at)")
            conn.execute("CREATE INDEX idx_sessions_dangling ON play_sessions(state, ended_at)")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        finally:
            conn.close()

    # -- v1 -> v2 migration (AR-C0 §5) -----------------------------------------
    def _migrate_to_v2(self, source_version: int) -> None:
        """Transactional table rebuild (§5.2): lock, re-verify, rebuild, stamp.

        Any failure rolls back: old table intact, old user_version intact,
        adapter construction raises.
        """
        conn = sqlite3.connect(self.db_path)
        conn.isolation_level = None  # manual transaction control
        try:
            self._exec(conn, "PRAGMA busy_timeout=5000")
            self._exec(conn, "BEGIN IMMEDIATE")
            self._reverify_in_lock(conn, source_version)
            # SQLite drops table-owned triggers together with DROP TABLE. They
            # are user schema objects, not part of AR-C0's intended PK delta,
            # so capture their canonical CREATE SQL under the migration lock
            # and recreate them on the rebuilt table in the same transaction.
            relation_triggers = [
                (row[0], row[1])
                for row in conn.execute(
                    "SELECT name, sql, tbl_name FROM sqlite_master "
                    "WHERE type='trigger' ORDER BY name")
                if _sqlite_ascii_upper(row[2]) == "CHARACTER_RELATIONS"
            ]
            if any(sql is None for _name, sql in relation_triggers):
                raise GameMemoryMigrationError(
                    "character_relations has a trigger without restorable CREATE SQL")
            migration_table = self._available_migration_table_name(conn)
            # WAL-safe backup of the locked, committed pre-migration state; any
            # backup/publication failure aborts before the rebuild starts.
            self._create_pre_migration_backup(conn, source_version)
            self._exec(conn, _V2_RELATIONS_DDL.format(table=migration_table))
            self._exec(
                conn,
                f"INSERT INTO {migration_table} "
                "(relation_id, game_id, playthrough_id, updated_at, data) "
                "SELECT relation_id, game_id, playthrough_id, updated_at, data "
                "FROM character_relations")
            self._exec(conn, "DROP INDEX IF EXISTS idx_relations_lookup")
            self._exec(conn, "DROP TABLE character_relations")
            self._exec(
                conn,
                f"ALTER TABLE {migration_table} RENAME TO character_relations")
            self._exec(conn, "CREATE INDEX idx_relations_lookup ON character_relations(game_id, playthrough_id)")
            for _name, trigger_sql in relation_triggers:
                self._exec(conn, trigger_sql)
            self._exec(conn, f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._exec(conn, "COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass  # no open transaction -- keep the primary exception
            raise
        finally:
            conn.close()

    def _reverify_in_lock(self, conn: sqlite3.Connection, source_version: int) -> None:
        """§5.2 locked re-verify -- the unlocked inspection may be stale (#18)."""
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version != source_version or not self._legacy_v1_schema_ok(conn):
            raise GameMemoryMigrationError(
                f"{self.db_path}: DB state changed between inspection and the "
                f"migration lock (user_version now {version}); aborting untouched.")

    @staticmethod
    def _available_migration_table_name(conn: sqlite3.Connection) -> str:
        """Pick an internal rebuild name outside the user's schema namespace."""
        existing = {
            _sqlite_ascii_upper(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master")
        }
        base = "character_relations_v2"
        candidate = base
        suffix = 0
        while _sqlite_ascii_upper(candidate) in existing:
            suffix += 1
            candidate = f"{base}_arc0_{suffix}"
        return candidate

    @staticmethod
    def _exec(conn: sqlite3.Connection, sql: str) -> sqlite3.Cursor:
        # Single execution seam for migration statements -- the fault-injection
        # point for the §9 rollback matrix (#16).
        return conn.execute(sql)

    @staticmethod
    def _exec_p(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        # Parameterized twin of _exec: the single execution seam for the AR-C1
        # projection transaction -- the fault-injection point for its §9.3
        # matrix, COMMIT included. _exec itself is AR-C0 territory (migration
        # matrix) and stays untouched.
        return conn.execute(sql, params)

    # -- pre-migration backup: two-phase tmp -> verify -> publish (§5.1) -------
    def _create_pre_migration_backup(self, migration_conn: sqlite3.Connection,
                                     source_version: int) -> Path:
        """Fixed §5.0 connection roles and §5.1 publication protocol.

        tmp (outside the recovery namespace) -> journal normalization -> close
        backup connections -> ro verify -> close verify -> atomic no-clobber
        ``os.link`` publication -> unlink tmp family. Any pre-publication
        failure: close every helper connection first, then best-effort family
        cleanup, zero files in the ``*.pre-arc0.bak`` namespace, original
        exception preserved.
        """
        backups_dir = self.db_path.parent / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{self.db_path.name}.v{source_version}.{self._backup_timestamp()}.pre-arc0"
        tmp = backups_dir / f"{stem}.tmp"
        final = backups_dir / f"{stem}.bak"

        source_conn: sqlite3.Connection | None = None
        target_conn: sqlite3.Connection | None = None
        verify_conn: sqlite3.Connection | None = None
        try:
            # NEVER migration_conn itself: holding BEGIN IMMEDIATE while calling
            # backup() deadlocks (Python's backup() retries busy forever, §5.0).
            source_conn = self._open_backup_source_conn()
            target_conn = sqlite3.connect(tmp)
            source_conn.backup(target_conn)
            # Target inherits the source's WAL header; normalize so the ro
            # verify stage materializes zero sidecars. The PRAGMA can silently
            # answer 'wal' instead of raising -- check the returned value.
            mode = self._normalize_target_journal(target_conn)
            if str(mode).lower() != "delete":
                raise GameMemoryMigrationError(
                    f"backup journal normalization returned {mode!r} (expected "
                    "'delete'); aborting before publication.")
            self._close_conn(target_conn, "backup_target_conn")
            target_conn = None
            self._close_conn(source_conn, "backup_source_conn")
            source_conn = None

            verify_conn = self._open_ro(tmp)
            self._verify_backup_tmp(verify_conn, migration_conn, source_version, tmp)
            # Publication with any open handle on tmp is a corruption risk;
            # close the sequential verify connection before the commit point.
            self._close_conn(verify_conn, "backup_verify_conn")
            verify_conn = None

            self._publish_backup(tmp, final)
        except BaseException:
            # Pre-publication failure. Order is contractual (#35): every close
            # ATTEMPT happens before the first cleanup action; a failing close
            # keeps the primary exception, still tries the remaining closes,
            # and leaves the family to the stale gate.
            close_failed = False
            for conn_, role in ((target_conn, "backup_target_conn"),
                                (source_conn, "backup_source_conn"),
                                (verify_conn, "backup_verify_conn")):
                if conn_ is None:
                    continue
                try:
                    self._close_conn(conn_, role)
                except Exception:
                    close_failed = True
            if not close_failed:
                try:
                    self._cleanup_tmp_family(tmp)
                except Exception:
                    pass  # cleanup must never replace the pre-publication error
            raise
        # Post-publication cleanup failure keeps the valid final; the leftover
        # family is the next startup gate's problem (safe-delete guidance, #28).
        try:
            self._cleanup_tmp_family(tmp)
        except Exception:
            pass
        return final

    @staticmethod
    def _backup_timestamp() -> str:
        return time.strftime("%Y%m%d-%H%M%S")

    def _open_backup_source_conn(self) -> sqlite3.Connection:
        return self._open_ro(self.db_path)

    @staticmethod
    def _normalize_target_journal(target_conn: sqlite3.Connection) -> str:
        return target_conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]

    @staticmethod
    def _close_conn(conn: sqlite3.Connection, role: str) -> None:
        # Seam: role-tagged close so tests can pin ordering/identity (§9 #20/#26/#35).
        conn.close()

    def _verify_backup_tmp(self, verify_conn: sqlite3.Connection,
                           migration_conn: sqlite3.Connection,
                           source_version: int, tmp: Path) -> None:
        """§5.1.3: version reconciliation, all schema objects, per-table
        full-column full-row parity, integrity -- against the locked source."""
        got_version = int(verify_conn.execute("PRAGMA user_version").fetchone()[0])
        if got_version != source_version:
            raise GameMemoryMigrationError(
                f"backup tmp user_version={got_version} != source {source_version}")
        schema_sql = "SELECT type, name, tbl_name, sql FROM sqlite_master"
        tmp_schema = sorted(tuple(r) for r in verify_conn.execute(schema_sql))
        src_schema = sorted(tuple(r) for r in migration_conn.execute(schema_sql))
        if tmp_schema != src_schema:
            raise GameMemoryMigrationError("backup tmp schema objects differ from source")
        tables_sql = ("SELECT name FROM sqlite_master WHERE type='table' "
                      "AND name NOT GLOB 'sqlite_*'")
        tmp_tables = {r[0] for r in verify_conn.execute(tables_sql)}
        src_tables = {r[0] for r in migration_conn.execute(tables_sql)}
        if tmp_tables != src_tables:
            raise GameMemoryMigrationError("backup tmp table set differs from source")
        for table in sorted(src_tables):
            quoted_table = _quote_sqlite_identifier(table)
            select = f"SELECT * FROM {quoted_table}"  # noqa: S608 -- safely quoted
            tmp_rows = Counter(tuple(r) for r in verify_conn.execute(select))
            src_rows = Counter(tuple(r) for r in migration_conn.execute(select))
            if tmp_rows != src_rows:
                raise GameMemoryMigrationError(
                    f"backup tmp rows differ from source in table {table}")
        integrity = verify_conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise GameMemoryMigrationError(f"backup tmp integrity_check: {integrity}")

    @staticmethod
    def _publish_backup(tmp: Path, final: Path) -> None:
        """Atomic no-clobber publication commit point (§5.1.5).

        POSIX rename silently overwrites -- ``os.link`` raises FileExistsError
        instead. A pre-existing final is never overwritten or deleted; missing
        hardlink support / any other OSError is a pre-commit failure.
        """
        try:
            os.link(tmp, final)
        except FileExistsError as exc:
            raise GameMemoryMigrationError(
                f"backup target already exists, refusing to overwrite: {final}") from exc
        except OSError as exc:
            raise GameMemoryMigrationError(
                f"backup publication failed ({exc}); no formal backup produced") from exc

    def _cleanup_tmp_family(self, tmp: Path) -> None:
        # Sidecars go first. Only after every sidecar is gone may the base tmp
        # name be removed: after os.link publication that base name is the
        # samefile proof used by the next-start gate for safe-delete guidance.
        sidecar_failed = False
        for member in self._tmp_family(tmp)[1:]:
            try:
                member.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                sidecar_failed = True
        if sidecar_failed:
            return
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    @staticmethod
    def _tmp_family(tmp: Path) -> tuple[Path, ...]:
        return (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm"),
                Path(str(tmp) + "-journal"))

    def schema_version(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    @staticmethod
    def _dump(model: Any) -> str:
        return json.dumps(model.to_dict(), ensure_ascii=False)

    # Projection row builders (AR-C1): the single methods and the atomic command
    # bind the SAME parameter tuples to the shared DML constants -- one place to
    # change a row shape, zero drift between the two write paths.
    def _summary_row(self, summary: StorySummary) -> tuple[Any, ...]:
        return (
            summary.summary_id,
            summary.game_id,
            summary.playthrough_id,
            summary.created_at,
            self._dump(summary),
        )

    def _progress_row(self, state: GameProgressState) -> tuple[Any, ...]:
        return (state.game_id, state.playthrough_id, state.last_played_at, self._dump(state))

    def _relation_row(self, relation: CharacterRelation) -> tuple[Any, ...]:
        return (
            relation.relation_id,
            relation.game_id,
            relation.playthrough_id,
            relation.updated_at,
            self._dump(relation),
        )

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

    def current_pending_story_line(
        self, game_id: str, playthrough_id: str, session_id: str | None
    ) -> StoryLine | None:
        # B1: scoped by session_id so a crash-residue PENDING_CURRENT row from an
        # already-ended session is never returned (dangling recovery does not
        # reconcile pending rows). _write_pending_current persists BEFORE the
        # in-memory field, so this DB read is never staler than the owner's state.
        # WAL + statement atomicity (Fix #6) -> a consistent committed snapshot.
        if not session_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM story_lines "
                "WHERE game_id = ? AND playthrough_id = ? AND session_id = ? AND status = ? "
                "ORDER BY timestamp DESC, line_id DESC LIMIT 1",
                (game_id, playthrough_id, session_id, StoryLineStatus.PENDING_CURRENT.value),
            ).fetchone()
        return StoryLine.from_dict(json.loads(row["data"])) if row else None

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
            conn.execute(_SUMMARY_INSERT_DML, self._summary_row(summary))
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
            conn.execute(_PROGRESS_UPSERT_DML, self._progress_row(state))

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
        # Scoped ON CONFLICT semantics live on _RELATION_UPSERT_DML (AR-C0).
        with self._connect() as conn:
            conn.execute(_RELATION_UPSERT_DML, self._relation_row(relation))
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

    # -- atomic summary projection (AR-C1) -------------------------------------
    def apply_summary_projection(
        self,
        summary: StorySummary,
        progress: GameProgressState,
        relations: Sequence[CharacterRelation],
    ) -> str:
        """One transaction, all-or-nothing (see GameMemoryPort docstring).

        Values are fully materialized by the caller; the transaction runs no
        reads, no LLM calls, no events -- lock time is three DML statements.
        BEGIN IMMEDIATE takes the write lock up front (no mid-transaction busy
        upgrade); _migrate_to_v2 is the in-repo precedent for this shape. The
        connection is created on the calling thread (sqlite3 default
        check_same_thread).
        """
        conn = sqlite3.connect(self.db_path)
        conn.isolation_level = None  # manual transaction control
        try:
            self._exec_p(conn, "PRAGMA busy_timeout=5000")
            self._exec_p(conn, "BEGIN IMMEDIATE")
            self._exec_p(conn, _SUMMARY_INSERT_DML, self._summary_row(summary))
            self._exec_p(conn, _PROGRESS_UPSERT_DML, self._progress_row(progress))
            for relation in relations:
                self._exec_p(conn, _RELATION_UPSERT_DML, self._relation_row(relation))
            self._exec_p(conn, "COMMIT")
        except BaseException:
            # catch-and-RERAISE keeps BaseException (a KeyboardInterrupt inside
            # the transaction must still roll back before propagating) -- unlike
            # the session-side catch-and-swallow points, which only catch
            # Exception (AR-C1 v1.4 discipline).
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass  # no open transaction -- keep the primary exception
            raise
        finally:
            conn.close()
        return summary.summary_id

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
