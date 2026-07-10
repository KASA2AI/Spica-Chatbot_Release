"""AR-C0 migration/backup/restore matrix for the galgame-memory SQLite adapter.

Covers the version matrix (fresh/v0/v1/v2/future), the WAL-safe pre-migration
backup with its two-phase publication protocol, transactional rollback, stale
tmp gates, the restore runbook drills and the ``scripts/arc0_restore_preflight.py``
helper. Everything runs on temporary DBs only -- the real ``spica_data/`` is
never read or written (施工单 §9 discipline).

Legacy fixtures are built via direct sqlite3 (approved seam, §6.1); behavior
assertions go through the adapter public interface wherever possible.
"""

import errno
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT_HELPER = REPO_ROOT / "scripts" / "arc0_restore_preflight.py"

from spica.adapters.game_memory.sqlite import (
    SCHEMA_VERSION,
    GameMemoryMigrationError,
    GameMemorySqliteAdapter,
)
from spica.galgame.models import CharacterRelation, GameProfile

# -- legacy v1 fixture (verbatim BASE schema, pre-AR-C0) -----------------------

V1_TABLE_DDL = (
    """CREATE TABLE game_profiles (
        game_id TEXT PRIMARY KEY,
        last_played_at TEXT,
        data TEXT NOT NULL
    )""",
    """CREATE TABLE play_sessions (
        session_id TEXT PRIMARY KEY,
        game_id TEXT NOT NULL,
        playthrough_id TEXT NOT NULL,
        state TEXT NOT NULL,
        started_at TEXT,
        ended_at TEXT,
        data TEXT NOT NULL
    )""",
    """CREATE TABLE story_lines (
        line_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        game_id TEXT NOT NULL,
        playthrough_id TEXT NOT NULL,
        status TEXT NOT NULL,
        timestamp TEXT,
        data TEXT NOT NULL
    )""",
    """CREATE TABLE story_summaries (
        summary_id TEXT PRIMARY KEY,
        game_id TEXT NOT NULL,
        playthrough_id TEXT NOT NULL,
        created_at TEXT,
        data TEXT NOT NULL
    )""",
    """CREATE TABLE progress_states (
        game_id TEXT NOT NULL,
        playthrough_id TEXT NOT NULL,
        last_played_at TEXT,
        data TEXT NOT NULL,
        PRIMARY KEY (game_id, playthrough_id)
    )""",
    """CREATE TABLE character_relations (
        relation_id TEXT PRIMARY KEY,
        game_id TEXT NOT NULL,
        playthrough_id TEXT NOT NULL,
        updated_at TEXT,
        data TEXT NOT NULL
    )""",
    """CREATE TABLE choice_events (
        choice_id TEXT PRIMARY KEY,
        game_id TEXT NOT NULL,
        playthrough_id TEXT NOT NULL,
        timestamp TEXT,
        data TEXT NOT NULL
    )""",
    """CREATE TABLE companion_beats (
        beat_id TEXT PRIMARY KEY,
        game_id TEXT NOT NULL,
        user_id TEXT,
        character_id TEXT,
        created_at TEXT,
        data TEXT NOT NULL
    )""",
)

V1_TABLE_NAMES = (
    "game_profiles",
    "play_sessions",
    "story_lines",
    "story_summaries",
    "progress_states",
    "character_relations",
    "choice_events",
    "companion_beats",
)

# Independent malformed DDL literals for the second review's inline-constraint
# finding. They are intentionally test-owned rather than derived from either
# production or helper constants.
MALFORMED_V1_GAME_PROFILES_DDL = {
    "check": """CREATE TABLE game_profiles (
        game_id TEXT PRIMARY KEY,
        last_played_at TEXT,
        data TEXT NOT NULL,
        CHECK (game_id <> 'BLOCK')
    )""",
    "foreign_key": """CREATE TABLE game_profiles (
        game_id TEXT PRIMARY KEY,
        last_played_at TEXT,
        data TEXT NOT NULL,
        FOREIGN KEY (game_id) REFERENCES choice_events(choice_id)
    )""",
    "unique": """CREATE TABLE game_profiles (
        game_id TEXT PRIMARY KEY,
        last_played_at TEXT,
        data TEXT NOT NULL,
        UNIQUE (last_played_at)
    )""",
    "collate": """CREATE TABLE game_profiles (
        game_id TEXT PRIMARY KEY,
        last_played_at TEXT COLLATE NOCASE,
        data TEXT NOT NULL
    )""",
    "default": """CREATE TABLE game_profiles (
        game_id TEXT PRIMARY KEY,
        last_played_at TEXT DEFAULT 'never',
        data TEXT NOT NULL
    )""",
    "generated": """CREATE TABLE game_profiles (
        game_id TEXT PRIMARY KEY,
        last_played_at TEXT,
        data TEXT NOT NULL,
        guard TEXT GENERATED ALWAYS AS (game_id) VIRTUAL
    )""",
}

MALFORMED_V1_RELATIONS_CHECK_DDL = """CREATE TABLE character_relations (
    relation_id TEXT PRIMARY KEY,
    game_id TEXT NOT NULL,
    playthrough_id TEXT NOT NULL,
    updated_at TEXT,
    data TEXT NOT NULL,
    CHECK (relation_id <> 'BLOCK')
)"""

MALFORMED_QUOTED_KEYWORD_GAME_PROFILES_DDL = """CREATE TABLE game_profiles (
    game_id TEXT PRIMARY KEY,
    last_played_at TEXT,
    data TEXT "NOT" NULL
)"""

MALFORMED_NON_SQLITE_WHITESPACE_GAME_PROFILES_DDL = """CREATE TABLE game_profiles (
    game_id TEXT PRIMARY KEY,
    last_played_at TEXT,
    data TEXT\N{NO-BREAK SPACE}NOT NULL
)"""

MALFORMED_UNICODE_FOLD_GAME_PROFILES_DDL = """CREATE TABLE game_profiles (
    game_id TEXT PR\N{LATIN SMALL LETTER DOTLESS I}MARY KEY,
    last_played_at TEXT,
    data TEXT NOT NULL
)"""

MALFORMED_V2_RELATIONS_DDL = {
    "check": """CREATE TABLE character_relations (
        relation_id TEXT NOT NULL,
        game_id TEXT NOT NULL,
        playthrough_id TEXT NOT NULL,
        updated_at TEXT,
        data TEXT NOT NULL,
        PRIMARY KEY (game_id, playthrough_id, relation_id),
        CHECK (relation_id <> 'BLOCK')
    )""",
    "foreign_key": """CREATE TABLE character_relations (
        relation_id TEXT NOT NULL,
        game_id TEXT NOT NULL,
        playthrough_id TEXT NOT NULL,
        updated_at TEXT,
        data TEXT NOT NULL,
        PRIMARY KEY (game_id, playthrough_id, relation_id),
        FOREIGN KEY (game_id) REFERENCES game_profiles(game_id)
    )""",
    "unique": """CREATE TABLE character_relations (
        relation_id TEXT NOT NULL,
        game_id TEXT NOT NULL,
        playthrough_id TEXT NOT NULL,
        updated_at TEXT,
        data TEXT NOT NULL,
        PRIMARY KEY (game_id, playthrough_id, relation_id),
        UNIQUE (updated_at)
    )""",
    "collate": """CREATE TABLE character_relations (
        relation_id TEXT NOT NULL,
        game_id TEXT NOT NULL,
        playthrough_id TEXT NOT NULL,
        updated_at TEXT COLLATE NOCASE,
        data TEXT NOT NULL,
        PRIMARY KEY (game_id, playthrough_id, relation_id)
    )""",
    "default": """CREATE TABLE character_relations (
        relation_id TEXT NOT NULL,
        game_id TEXT NOT NULL,
        playthrough_id TEXT NOT NULL,
        updated_at TEXT DEFAULT 'never',
        data TEXT NOT NULL,
        PRIMARY KEY (game_id, playthrough_id, relation_id)
    )""",
    "generated": """CREATE TABLE character_relations (
        relation_id TEXT NOT NULL,
        game_id TEXT NOT NULL,
        playthrough_id TEXT NOT NULL,
        updated_at TEXT,
        data TEXT NOT NULL,
        guard TEXT GENERATED ALWAYS AS (relation_id) VIRTUAL,
        PRIMARY KEY (game_id, playthrough_id, relation_id)
    )""",
}

V1_INDEX_DDL = (
    "CREATE INDEX idx_story_lines_lookup ON story_lines(game_id, playthrough_id, status, timestamp)",
    "CREATE INDEX idx_summaries_lookup ON story_summaries(game_id, playthrough_id, created_at)",
    "CREATE INDEX idx_choices_lookup ON choice_events(game_id, playthrough_id, timestamp)",
    "CREATE INDEX idx_relations_lookup ON character_relations(game_id, playthrough_id)",
    "CREATE INDEX idx_beats_lookup ON companion_beats(game_id, user_id, character_id, created_at)",
    "CREATE INDEX idx_sessions_dangling ON play_sessions(state, ended_at)",
)

# Independent SQL identifier literals for §9 #14/#30/#41. The quoted spellings
# are test-owned worked examples, never generated by production/helper code.
EXTRA_IDENTIFIER_TABLES = (
    ("keyword", "select", '"select"'),
    ("space", "user data", '"user data"'),
    ("embedded_quote", 'quote"name', '"quote""name"'),
)

MALFORMED_RELATIONS_LOOKUP_DDL = {
    "unique": (
        "CREATE UNIQUE INDEX idx_relations_lookup "
        "ON character_relations(game_id, playthrough_id)"),
    "partial": (
        "CREATE INDEX idx_relations_lookup "
        "ON character_relations(game_id, playthrough_id) "
        "WHERE updated_at IS NOT NULL"),
    "desc": (
        "CREATE INDEX idx_relations_lookup "
        "ON character_relations(game_id DESC, playthrough_id)"),
    "nocase": (
        "CREATE INDEX idx_relations_lookup "
        "ON character_relations(game_id COLLATE NOCASE, playthrough_id)"),
}

EQUIVALENT_RELATIONS_LOOKUP_DDL = {
    "double_quotes": (
        'CREATE INDEX "idx_relations_lookup" '
        'ON "character_relations"("game_id", "playthrough_id")'),
    "brackets": (
        "CREATE INDEX [idx_relations_lookup] "
        "ON [character_relations]([game_id], [playthrough_id])"),
    "backticks": (
        "CREATE INDEX `idx_relations_lookup` "
        "ON `character_relations`(`game_id`, `playthrough_id`)"),
    "comments_whitespace": (
        "CREATE /* canonical lookup */ INDEX idx_relations_lookup\n"
        "ON character_relations (\n"
        "    game_id /* first key */,\n"
        "    playthrough_id\n"
        ")"),
}

# Independent spec literals for §9 #12/#42. These do not import or derive from
# production constants, so a wrong implementation cannot make the expectation
# move with it.
EXPECTED_RELATIONS_INDEX_LIST = (
    ("idx_relations_lookup", 0, "c", 0),
    ("sqlite_autoindex_character_relations_1", 1, "pk", 0),
)
EXPECTED_LOOKUP_INDEX_XINFO = (
    (0, 1, "game_id", 0, "BINARY", 1),
    (1, 2, "playthrough_id", 0, "BINARY", 1),
    (2, -1, None, 0, "BINARY", 0),
)
EXPECTED_V1_RELATIONS_ENDPOINT = {
    "table_xinfo": (
        (0, "relation_id", "TEXT", 0, None, 1, 0),
        (1, "game_id", "TEXT", 1, None, 0, 0),
        (2, "playthrough_id", "TEXT", 1, None, 0, 0),
        (3, "updated_at", "TEXT", 0, None, 0, 0),
        (4, "data", "TEXT", 1, None, 0, 0),
    ),
    "index_list": EXPECTED_RELATIONS_INDEX_LIST,
    "index_xinfo": {
        "idx_relations_lookup": EXPECTED_LOOKUP_INDEX_XINFO,
        "sqlite_autoindex_character_relations_1": (
            (0, 0, "relation_id", 0, "BINARY", 1),
            (1, -1, None, 0, "BINARY", 0),
        ),
    },
}
EXPECTED_V2_RELATIONS_ENDPOINT = {
    "table_xinfo": (
        (0, "relation_id", "TEXT", 1, None, 3, 0),
        (1, "game_id", "TEXT", 1, None, 1, 0),
        (2, "playthrough_id", "TEXT", 1, None, 2, 0),
        (3, "updated_at", "TEXT", 0, None, 0, 0),
        (4, "data", "TEXT", 1, None, 0, 0),
    ),
    "index_list": EXPECTED_RELATIONS_INDEX_LIST,
    "index_xinfo": {
        "idx_relations_lookup": EXPECTED_LOOKUP_INDEX_XINFO,
        "sqlite_autoindex_character_relations_1": (
            (0, 1, "game_id", 0, "BINARY", 1),
            (1, 2, "playthrough_id", 0, "BINARY", 1),
            (2, 0, "relation_id", 0, "BINARY", 1),
            (3, -1, None, 0, "BINARY", 0),
        ),
    },
}


def make_relation(relation_id: str, game_id: str, playthrough_id: str = "default",
                  summary: str = "", updated_at: str = "2026-07-10T10:00:00") -> CharacterRelation:
    return CharacterRelation(
        relation_id=relation_id, game_id=game_id, playthrough_id=playthrough_id,
        character_a="A", character_b="B", relation_summary=summary,
        updated_at=updated_at)


def build_legacy_db(path: Path, *, user_version: int = 1,
                    relations: tuple = (), profiles: tuple = (),
                    wal: bool = False, relations_ddl: str | None = None,
                    table_ddl_overrides: dict[str, str] | None = None,
                    if_not_exists: bool = False,
                    keep_open: bool = False) -> sqlite3.Connection | None:
    """Direct-sqlite3 legacy fixture: BASE (pre-AR-C0) schema + stamped version.

    ``relations_ddl`` swaps in a malformed character_relations shape (§9 #8).
    ``keep_open=True`` returns the fixture connection unclosed so WAL content
    stays un-checkpointed (closing the last connection would checkpoint, §9 #13).
    """
    conn = sqlite3.connect(path)
    try:
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        overrides = table_ddl_overrides or {}
        for table, ddl in zip(V1_TABLE_NAMES, V1_TABLE_DDL):
            ddl = overrides.get(table, ddl)
            if relations_ddl is not None and "CREATE TABLE character_relations" in ddl:
                ddl = relations_ddl
            if if_not_exists:
                ddl = ddl.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)
            conn.execute(ddl)
        for ddl in V1_INDEX_DDL:
            if relations_ddl is not None and "idx_relations_lookup" in ddl:
                continue  # malformed shapes may not have the indexed columns
            if if_not_exists:
                ddl = ddl.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1)
            conn.execute(ddl)
        for rel in relations:
            conn.execute(
                "INSERT INTO character_relations "
                "(relation_id, game_id, playthrough_id, updated_at, data) VALUES (?, ?, ?, ?, ?)",
                (rel.relation_id, rel.game_id, rel.playthrough_id, rel.updated_at,
                 json.dumps(rel.to_dict(), ensure_ascii=False)))
        for profile in profiles:
            conn.execute(
                "INSERT INTO game_profiles (game_id, last_played_at, data) VALUES (?, ?, ?)",
                (profile.game_id, profile.last_played_at,
                 json.dumps(profile.to_dict(), ensure_ascii=False)))
        conn.execute(f"PRAGMA user_version = {int(user_version)}")
        conn.commit()
        if keep_open:
            return conn
    finally:
        if not keep_open:
            conn.close()
    return None


def replace_relations_lookup(db_path: Path, ddl: str) -> None:
    """Approved schema-fixture seam for malformed endpoint contracts."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP INDEX idx_relations_lookup")
        conn.execute(ddl)
        conn.commit()
    finally:
        conn.close()


def rebuild_fixture_table(db_path: Path, table: str, ddl: str,
                          columns: tuple[str, ...],
                          indexes: tuple[tuple[str, str], ...] = ()) -> None:
    """Approved direct-SQLite seam for exact table-DDL contract fixtures."""
    old_table = f"{table}_fixture_old"
    quoted_columns = ", ".join(f'"{column}"' for column in columns)
    conn = sqlite3.connect(db_path)
    try:
        for index_name, _index_ddl in indexes:
            conn.execute(f'DROP INDEX IF EXISTS "{index_name}"')
        conn.execute(f'ALTER TABLE "{table}" RENAME TO "{old_table}"')
        conn.execute(ddl)
        conn.execute(
            f'INSERT INTO "{table}" ({quoted_columns}) '
            f'SELECT {quoted_columns} FROM "{old_table}"')
        conn.execute(f'DROP TABLE "{old_table}"')
        for _index_name, index_ddl in indexes:
            conn.execute(index_ddl)
        conn.commit()
    finally:
        conn.close()


def relations_table_info(db_path: Path) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return [tuple(row) for row in conn.execute(
            "PRAGMA table_info(character_relations)").fetchall()]
    finally:
        conn.close()


def open_ro(db_path: Path) -> sqlite3.Connection:
    """Inspection-discipline connection for test-side snapshots (no pragmas)."""
    return sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)


def logical_snapshot(db_path: Path) -> dict:
    """user_version + schema objects + every business-table row + journal mode.

    §9 #10's "logical state" -- sidecar materialization is explicitly allowed,
    so file-level comparison is NOT part of this snapshot.
    """
    conn = open_ro(db_path)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        schema = sorted(
            tuple(row) for row in conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall())
        tables = sorted(row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'"
        ).fetchall())
        rows = {
            table: sorted(repr(tuple(row)) for row in conn.execute(
                f"SELECT * FROM {table}").fetchall())
            for table in tables
        }
    finally:
        conn.close()
    return {"user_version": version, "journal_mode": journal_mode,
            "schema": schema, "rows": rows}


def backup_artifacts(data_dir: Path) -> list[str]:
    """Every file in the backup/restore namespace (published, tmp family, any)."""
    backups_dir = data_dir / "backups"
    if not backups_dir.is_dir():
        return []
    return sorted(p.name for p in backups_dir.iterdir())


class MigrationTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self.db_path = self.data_dir / "galgame.sqlite3"


class LegacyV1MigrationTest(MigrationTestBase):
    """§9 #6: valid v1 -> v2 table rebuild preserving every surviving row."""

    def test_v1_migrates_to_v2_preserving_surviving_rows(self):
        rels = (
            make_relation("rel::A::B", "g1", summary="g1 relation"),
            make_relation("rel::C::D", "g1", "route-a", summary="route-a relation"),
            make_relation("rel::E::F", "g2", summary="g2 relation"),
        )
        profile = GameProfile(game_id="g1", display_name="Game One",
                              created_at="t", updated_at="t")
        build_legacy_db(self.db_path, user_version=1, relations=rels,
                        profiles=(profile,))

        adapter = GameMemorySqliteAdapter(self.db_path)

        # Version stamped only after a successful rebuild.
        self.assertEqual(adapter.schema_version(), SCHEMA_VERSION)
        # Every surviving row is preserved verbatim and readable per scope.
        self.assertEqual(adapter.character_relations("g1"), [rels[0]])
        self.assertEqual(adapter.character_relations("g1", "route-a"), [rels[1]])
        self.assertEqual(adapter.character_relations("g2"), [rels[2]])
        # Unrelated tables are untouched by the rebuild.
        self.assertEqual(adapter.get_game_profile("g1"), profile)

        # Composite PK order (game_id=1, playthrough_id=2, relation_id=3).
        pk_by_name = {row[1]: row[5] for row in relations_table_info(self.db_path)}
        self.assertEqual(pk_by_name, {
            "game_id": 1, "playthrough_id": 2, "relation_id": 3,
            "updated_at": 0, "data": 0,
        })

        # idx_relations_lookup rebuilt on (game_id, playthrough_id).
        conn = sqlite3.connect(self.db_path)
        try:
            cols = [row[2] for row in conn.execute(
                "PRAGMA index_info(idx_relations_lookup)").fetchall()]
        finally:
            conn.close()
        self.assertEqual(cols, ["game_id", "playthrough_id"])

    def test_migrated_db_supports_scoped_upsert(self):
        # Post-migration the adapter's scoped upsert works on the rebuilt table.
        rels = (make_relation("rel::A::B", "g1", summary="old"),)
        build_legacy_db(self.db_path, user_version=1, relations=rels)

        adapter = GameMemorySqliteAdapter(self.db_path)
        adapter.upsert_character_relation(
            make_relation("rel::A::B", "g1", summary="new",
                          updated_at="2026-07-10T11:00:00"))
        adapter.upsert_character_relation(
            make_relation("rel::A::B", "g2", summary="g2 relation"))

        g1_rels = adapter.character_relations("g1")
        self.assertEqual([r.relation_summary for r in g1_rels], ["new"])
        self.assertEqual(
            [r.relation_summary for r in adapter.character_relations("g2")],
            ["g2 relation"])

    def test_relation_trigger_survives_migration_and_remains_usable(self):
        """A legal persisted trigger belongs to the legacy DB, not to AR-C0.

        Rebuilding ``character_relations`` must preserve that schema object;
        SQLite otherwise drops table-owned triggers together with the old table.
        """
        build_legacy_db(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE relation_audit (relation_id TEXT NOT NULL)")
            conn.execute(
                "CREATE TRIGGER keep_relation_audit "
                "AFTER INSERT ON character_relations "
                "BEGIN INSERT INTO relation_audit VALUES (new.relation_id); END")

        adapter = GameMemorySqliteAdapter(self.db_path)
        adapter.upsert_character_relation(make_relation("rel::A::B", "g1"))

        with sqlite3.connect(self.db_path) as conn:
            trigger = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?",
                ("keep_relation_audit",)).fetchone()
            audit_rows = conn.execute(
                "SELECT relation_id FROM relation_audit").fetchall()
        self.assertEqual(trigger, ("keep_relation_audit",))
        self.assertEqual(audit_rows, [("rel::A::B",)])


    def test_relation_trigger_table_name_is_case_insensitive_during_migration(self):
        build_legacy_db(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE relation_audit (value TEXT NOT NULL)")
            conn.execute(
                "CREATE TRIGGER MixedCaseTrigger "
                "AFTER INSERT ON CHARACTER_RELATIONS "
                "BEGIN INSERT INTO relation_audit VALUES (new.relation_id); END")

        adapter = GameMemorySqliteAdapter(self.db_path)
        adapter.upsert_character_relation(make_relation("rel::A::B", "g1"))

        with sqlite3.connect(self.db_path) as conn:
            trigger = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?",
                ("MixedCaseTrigger",)).fetchone()
            audit_rows = conn.execute("SELECT value FROM relation_audit").fetchall()
        self.assertEqual(trigger, ("MixedCaseTrigger",))
        self.assertEqual(audit_rows, [("rel::A::B",)])


class VersionMatrixTest(MigrationTestBase):
    """§9 #5/#7-#11 + #37: every §4 dispatch branch, each rejection zero-write."""

    def assert_no_backup_artifacts(self):
        # §9 #37 -- branches that never enter migration must leave both the
        # recovery namespace and the tmp family empty.
        self.assertEqual(backup_artifacts(self.data_dir), [])

    def test_fresh_db_creates_v2(self):
        # §9 #5 -- missing file: full v2 schema, stamped 2, usable immediately.
        adapter = GameMemorySqliteAdapter(self.db_path)
        self.assertEqual(adapter.schema_version(), SCHEMA_VERSION)
        adapter.upsert_character_relation(make_relation("rel::A::B", "g1", summary="s"))
        self.assertEqual(len(adapter.character_relations("g1")), 1)
        self.assert_no_backup_artifacts()

    def test_fresh_empty_file_creates_v2(self):
        # §4 fresh row also covers user_version=0 with no business tables.
        self.db_path.touch()
        adapter = GameMemorySqliteAdapter(self.db_path)
        self.assertEqual(adapter.schema_version(), SCHEMA_VERSION)
        self.assert_no_backup_artifacts()

    def test_valid_v0_legacy_migrates(self):
        # §9 #7 -- pre-versioning DB (user_version=0) with exact v1 shape.
        rels = (make_relation("rel::A::B", "g1", summary="v0 row"),)
        build_legacy_db(self.db_path, user_version=0, relations=rels)
        adapter = GameMemorySqliteAdapter(self.db_path)
        self.assertEqual(adapter.schema_version(), SCHEMA_VERSION)
        self.assertEqual(adapter.character_relations("g1"), [rels[0]])

    def test_valid_v0_base_if_not_exists_ddl_migrates(self):
        # Canonical legacy databases were created by BASE with IF NOT EXISTS.
        # That harmless clause must normalize to the same exact DDL contract.
        rel = make_relation("rel::A::B", "g1", summary="base ddl")
        build_legacy_db(
            self.db_path, user_version=0, relations=(rel,), if_not_exists=True)

        adapter = GameMemorySqliteAdapter(self.db_path)

        self.assertEqual(adapter.schema_version(), SCHEMA_VERSION)
        self.assertEqual(adapter.character_relations("g1"), [rel])

    def test_valid_v0_with_extra_user_table_migrates_and_preserves_it(self):
        # §9 #30 remains allowed: exact required v1 tables do not forbid an
        # unrelated user table, and migration must not rebuild or drop it.
        build_legacy_db(self.db_path, user_version=0)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("CREATE TABLE sqliteXtra (id TEXT PRIMARY KEY, data TEXT)")
            conn.execute("INSERT INTO sqliteXtra VALUES ('k', 'v')")
            conn.commit()
        finally:
            conn.close()

        GameMemorySqliteAdapter(self.db_path)

        conn = open_ro(self.db_path)
        try:
            row = conn.execute("SELECT id, data FROM sqliteXtra").fetchone()
        finally:
            conn.close()
        self.assertEqual(tuple(row), ("k", "v"))

    def test_user_table_named_like_migration_temp_is_preserved(self):
        build_legacy_db(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE character_relations_v2 (precious TEXT NOT NULL)")
            conn.execute(
                "INSERT INTO character_relations_v2 VALUES ('keep-user-data')")

        adapter = GameMemorySqliteAdapter(self.db_path)
        adapter.upsert_character_relation(make_relation("rel::A::B", "g1"))

        with sqlite3.connect(self.db_path) as conn:
            user_row = conn.execute(
                "SELECT precious FROM character_relations_v2").fetchone()
        self.assertEqual(adapter.schema_version(), SCHEMA_VERSION)
        self.assertEqual(user_row, ("keep-user-data",))
        self.assertEqual(
            [relation.relation_id for relation in adapter.character_relations("g1")],
            ["rel::A::B"])

    def test_hot_rollback_journal_recovers_before_v1_migration(self):
        committed = GameProfile(
            game_id="committed", display_name="committed before crash",
            created_at="t", updated_at="t")
        build_legacy_db(self.db_path, profiles=(committed,))
        plant_hot_journal(self.db_path)

        adapter = GameMemorySqliteAdapter(self.db_path)

        self.assertEqual(adapter.schema_version(), SCHEMA_VERSION)
        self.assertEqual(adapter.get_game_profile("committed"), committed)
        self.assertIsNone(adapter.get_game_profile("HOT0"))

    def test_hot_rollback_journal_future_version_recovers_then_fails_loud(self):
        build_legacy_db(self.db_path, user_version=SCHEMA_VERSION + 1)
        committed = logical_snapshot(self.db_path)
        plant_hot_journal(self.db_path)

        with self.assertRaises(GameMemoryMigrationError):
            GameMemorySqliteAdapter(self.db_path)

        self.assertEqual(logical_snapshot(self.db_path), committed)
        self.assert_no_backup_artifacts()

    def test_v0_rejects_inline_constraints_on_unrelated_canonical_table(self):
        # Finding 2 / §9 #8/#42: table_xinfo cannot see CHECK, FOREIGN KEY or
        # collation on an unindexed nullable column. All inline constraint forms
        # must be rejected before backup/migration, including the forms other
        # PRAGMA lenses already happen to expose.
        for case, ddl in MALFORMED_V1_GAME_PROFILES_DDL.items():
            with self.subTest(case=case):
                case_dir = self.data_dir / f"v0_profile_{case}"
                case_dir.mkdir()
                db = case_dir / "galgame.sqlite3"
                build_legacy_db(
                    db, user_version=0,
                    table_ddl_overrides={"game_profiles": ddl})
                before = logical_snapshot(db)

                with self.assertRaises(GameMemoryMigrationError):
                    GameMemorySqliteAdapter(db)

                self.assertEqual(logical_snapshot(db), before)
                self.assertEqual(backup_artifacts(case_dir), [])

    def test_v0_rejects_inline_check_on_relations_before_backup(self):
        # The v1 relation PRAGMA endpoint is otherwise byte-for-byte canonical.
        build_legacy_db(
            self.db_path, user_version=0,
            table_ddl_overrides={
                "character_relations": MALFORMED_V1_RELATIONS_CHECK_DDL,
            })
        before = logical_snapshot(self.db_path)

        with self.assertRaises(GameMemoryMigrationError):
            GameMemorySqliteAdapter(self.db_path)

        self.assertEqual(logical_snapshot(self.db_path), before)
        self.assert_no_backup_artifacts()

    def test_malformed_v0_fails_loud_without_touching_db(self):
        # §9 #8 -- v0 with a non-v1 relations shape must not migrate.
        build_legacy_db(
            self.db_path, user_version=0,
            relations_ddl="""CREATE TABLE character_relations (
                relation_id TEXT PRIMARY KEY,
                game_id TEXT NOT NULL,
                updated_at TEXT,
                data TEXT NOT NULL
            )""")
        before = logical_snapshot(self.db_path)
        with self.assertRaises(GameMemoryMigrationError):
            GameMemorySqliteAdapter(self.db_path)
        self.assertEqual(logical_snapshot(self.db_path), before)
        self.assert_no_backup_artifacts()

    def test_v0_with_tables_but_missing_relations_fails_loud(self):
        # §9 #8 variant -- business tables exist but the migration target is gone.
        build_legacy_db(self.db_path, user_version=0)
        conn = sqlite3.connect(self.db_path)
        conn.execute("DROP TABLE character_relations")
        conn.commit()
        conn.close()
        before = logical_snapshot(self.db_path)
        with self.assertRaises(GameMemoryMigrationError):
            GameMemorySqliteAdapter(self.db_path)
        self.assertEqual(logical_snapshot(self.db_path), before)
        self.assert_no_backup_artifacts()

    def test_v0_with_only_exact_relations_table_fails_before_backup(self):
        # Review remediation: an exact relations endpoint alone is not a
        # complete legacy-v1 database. All eight business tables are required.
        conn = sqlite3.connect(self.db_path)
        conn.execute(V1_TABLE_DDL[5])
        conn.execute(V1_INDEX_DDL[3])
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
        conn.close()
        before = logical_snapshot(self.db_path)

        with self.assertRaises(GameMemoryMigrationError):
            GameMemorySqliteAdapter(self.db_path)

        self.assertEqual(logical_snapshot(self.db_path), before)
        self.assert_no_backup_artifacts()

    def test_v0_with_malformed_unrelated_table_fails_before_backup(self):
        # Review remediation: relations may be exact while another required v1
        # business table is malformed; no table other than relations may be
        # rebuilt or silently tolerated.
        build_legacy_db(self.db_path, user_version=0)
        conn = sqlite3.connect(self.db_path)
        conn.execute("DROP TABLE game_profiles")
        conn.execute(
            "CREATE TABLE game_profiles (game_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        conn.commit()
        conn.close()
        before = logical_snapshot(self.db_path)

        with self.assertRaises(GameMemoryMigrationError):
            GameMemorySqliteAdapter(self.db_path)

        self.assertEqual(logical_snapshot(self.db_path), before)
        self.assert_no_backup_artifacts()

    def test_v0_with_missing_or_malformed_legacy_index_fails_before_backup(self):
        # Review remediation: all canonical legacy indexes are part of the v1
        # recognition contract, including uniqueness/origin/partial semantics.
        for case in ("missing", "unique"):
            with self.subTest(case=case):
                db = self.data_dir / f"v0_{case}_index.sqlite3"
                build_legacy_db(db, user_version=0)
                conn = sqlite3.connect(db)
                conn.execute("DROP INDEX idx_story_lines_lookup")
                if case == "unique":
                    conn.execute(
                        "CREATE UNIQUE INDEX idx_story_lines_lookup "
                        "ON story_lines(game_id, playthrough_id, status, timestamp)")
                conn.commit()
                conn.close()
                before = logical_snapshot(db)

                with self.assertRaises(GameMemoryMigrationError):
                    GameMemorySqliteAdapter(db)

                self.assertEqual(logical_snapshot(db), before)
                self.assertEqual(backup_artifacts(db.parent), [])

    def test_v2_reopen_is_idempotent(self):
        # §9 #9 -- second open verifies shape and changes nothing.
        adapter = GameMemorySqliteAdapter(self.db_path)
        adapter.upsert_character_relation(make_relation("rel::A::B", "g1", summary="s"))
        before = logical_snapshot(self.db_path)

        reopened = GameMemorySqliteAdapter(self.db_path)
        self.assertEqual(reopened.schema_version(), SCHEMA_VERSION)
        self.assertEqual(
            [r.relation_summary for r in reopened.character_relations("g1")], ["s"])
        self.assertEqual(logical_snapshot(self.db_path), before)
        self.assert_no_backup_artifacts()

    def test_v2_reopen_rejects_noncanonical_lookup_index_at_construction(self):
        # Review remediation / §9 #42: rejection belongs to the constructor
        # gate, before a second relation write can surface a late constraint
        # error. Column-name-only index_info checks miss every case below.
        for case, ddl in MALFORMED_RELATIONS_LOOKUP_DDL.items():
            with self.subTest(case=case):
                db = self.data_dir / f"v2_bad_lookup_{case}.sqlite3"
                GameMemorySqliteAdapter(db)
                replace_relations_lookup(db, ddl)
                before = logical_snapshot(db)

                with self.assertRaises(GameMemoryMigrationError):
                    GameMemorySqliteAdapter(db)

                self.assertEqual(logical_snapshot(db), before)
                self.assertEqual(backup_artifacts(db.parent), [])

    def test_v2_reopen_rejects_nonbinary_composite_pk_collation(self):
        # Exact PK validation also needs index_xinfo: table_xinfo alone does not
        # expose the NOCASE collation attached to a PK column.
        GameMemorySqliteAdapter(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DROP INDEX idx_relations_lookup")
            conn.execute(
                "ALTER TABLE character_relations RENAME TO character_relations_old")
            conn.execute(
                """CREATE TABLE character_relations (
                    relation_id TEXT NOT NULL,
                    game_id TEXT COLLATE NOCASE NOT NULL,
                    playthrough_id TEXT NOT NULL,
                    updated_at TEXT,
                    data TEXT NOT NULL,
                    PRIMARY KEY (game_id, playthrough_id, relation_id)
                )""")
            conn.execute(
                "INSERT INTO character_relations SELECT * FROM character_relations_old")
            conn.execute("DROP TABLE character_relations_old")
            conn.execute(
                "CREATE INDEX idx_relations_lookup "
                "ON character_relations(game_id, playthrough_id)")
            conn.commit()
        finally:
            conn.close()
        before = logical_snapshot(self.db_path)

        with self.assertRaises(GameMemoryMigrationError):
            GameMemorySqliteAdapter(self.db_path)

        self.assertEqual(logical_snapshot(self.db_path), before)
        self.assert_no_backup_artifacts()

    def test_v2_reopen_rejects_noncanonical_inline_constraints_at_construction(self):
        # Finding 2 / §9 #42: constructor rejection must precede any public write.
        # CHECK/FK/unindexed COLLATE are the PRAGMA-invisible red cases; UNIQUE,
        # DEFAULT and generated constraints pin the complete fail-closed family.
        relation_columns = (
            "relation_id", "game_id", "playthrough_id", "updated_at", "data")
        relation_indexes = ((
            "idx_relations_lookup",
            "CREATE INDEX idx_relations_lookup "
            "ON character_relations(game_id, playthrough_id)"),)
        for case, ddl in MALFORMED_V2_RELATIONS_DDL.items():
            with self.subTest(case=case):
                case_dir = self.data_dir / f"v2_relation_{case}"
                case_dir.mkdir()
                db = case_dir / "galgame.sqlite3"
                GameMemorySqliteAdapter(db)
                rebuild_fixture_table(
                    db, "character_relations", ddl, relation_columns,
                    relation_indexes)
                before = logical_snapshot(db)

                with self.assertRaises(GameMemoryMigrationError):
                    GameMemorySqliteAdapter(db)

                self.assertEqual(logical_snapshot(db), before)
                self.assertEqual(backup_artifacts(case_dir), [])

    def test_future_version_fails_loud_without_logical_change(self):
        # §9 #10 -- user_version>2: refuse, never downgrade, never stamp.
        GameMemorySqliteAdapter(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        conn.close()
        before = logical_snapshot(self.db_path)

        with self.assertRaises(GameMemoryMigrationError):
            GameMemorySqliteAdapter(self.db_path)
        # Logical contract only: content/schema/user_version/persistent journal
        # mode unchanged; -wal/-shm materialization is explicitly allowed.
        self.assertEqual(logical_snapshot(self.db_path), before)
        self.assert_no_backup_artifacts()

    def test_v1_stamp_with_v2_shape_fails_loud(self):
        # §9 #11 -- mislabeled DB: version says legacy, shape says migrated.
        GameMemorySqliteAdapter(self.db_path)  # builds real v2
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()
        before = logical_snapshot(self.db_path)

        with self.assertRaises(GameMemoryMigrationError):
            GameMemorySqliteAdapter(self.db_path)
        self.assertEqual(logical_snapshot(self.db_path), before)
        self.assert_no_backup_artifacts()

    def test_v2_stamp_with_v1_shape_fails_loud(self):
        # §9 #11 -- version says migrated, table is still the legacy shape.
        build_legacy_db(self.db_path, user_version=2,
                        relations=(make_relation("rel::A::B", "g1"),))
        before = logical_snapshot(self.db_path)

        with self.assertRaises(GameMemoryMigrationError):
            GameMemorySqliteAdapter(self.db_path)
        self.assertEqual(logical_snapshot(self.db_path), before)
        self.assert_no_backup_artifacts()


def published_backups(data_dir: Path) -> list[Path]:
    backups_dir = data_dir / "backups"
    if not backups_dir.is_dir():
        return []
    return sorted(backups_dir.glob("*.pre-arc0.bak"))


def tmp_family_members(data_dir: Path) -> list[Path]:
    backups_dir = data_dir / "backups"
    if not backups_dir.is_dir():
        return []
    suffixes = (".pre-arc0.tmp", ".pre-arc0.tmp-wal", ".pre-arc0.tmp-shm",
                ".pre-arc0.tmp-journal")
    return sorted(p for p in backups_dir.iterdir() if p.name.endswith(suffixes))


class PreMigrationBackupTest(MigrationTestBase):
    """§9 #13/#14/#27/#36: WAL-safe backup + two-phase publication happy path."""

    def test_backup_parity_quotes_every_dynamic_user_table_identifier(self):
        # Final-review remediation / §9 #14/#30: dynamically enumerated table
        # names are SQLite identifiers, not SQL fragments. Exact v0 and v1 both
        # preserve all rows in current v2 and in the verified formal backup.
        expected_rows = (("a", "first"), ("b", "second"))
        for version in (0, 1):
            for case, _table_name, quoted_table in EXTRA_IDENTIFIER_TABLES:
                with self.subTest(version=version, case=case):
                    case_dir = self.data_dir / f"identifier_v{version}_{case}"
                    case_dir.mkdir()
                    db = case_dir / "galgame.sqlite3"
                    build_legacy_db(db, user_version=version)
                    conn = sqlite3.connect(db)
                    try:
                        conn.execute(
                            f"CREATE TABLE {quoted_table} "
                            "(id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                        conn.executemany(
                            f"INSERT INTO {quoted_table} VALUES (?, ?)",
                            expected_rows)
                        conn.commit()
                    finally:
                        conn.close()

                    adapter = GameMemorySqliteAdapter(db)

                    self.assertEqual(adapter.schema_version(), SCHEMA_VERSION)
                    backups = published_backups(case_dir)
                    self.assertEqual(len(backups), 1)
                    for inspected in (db, backups[0]):
                        conn = open_ro(inspected)
                        try:
                            actual_rows = tuple(conn.execute(
                                f"SELECT id, payload FROM {quoted_table} "
                                "ORDER BY id").fetchall())
                        finally:
                            conn.close()
                        self.assertEqual(actual_rows, expected_rows)

    def test_backup_covers_uncheckpointed_wal_content(self):
        # §9 #13 -- rows living only in the -wal must be in the backup. Keep the
        # fixture connection open: closing the last connection checkpoints.
        rels = (make_relation("rel::A::B", "g1", summary="wal resident"),)
        fixture_conn = build_legacy_db(
            self.db_path, user_version=1, relations=rels, wal=True, keep_open=True)
        self.addCleanup(fixture_conn.close)
        wal_path = Path(str(self.db_path) + "-wal")
        self.assertTrue(wal_path.exists() and wal_path.stat().st_size > 0,
                        "fixture must hold its content in an un-checkpointed WAL")

        adapter = GameMemorySqliteAdapter(self.db_path)
        self.assertEqual(adapter.schema_version(), SCHEMA_VERSION)

        finals = published_backups(self.data_dir)
        self.assertEqual(len(finals), 1)
        conn = open_ro(finals[0])
        try:
            rows = conn.execute(
                "SELECT relation_id, game_id, playthrough_id FROM character_relations"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual([tuple(r) for r in rows], [("rel::A::B", "g1", "default")])

    def test_published_backup_full_contract(self):
        # §9 #14 -- name<->user_version, schema objects, per-table full parity,
        # integrity ok. All checks recomputed here, independent of the adapter's
        # own verify stage.
        rels = (make_relation("rel::A::B", "g1", summary="one"),
                make_relation("rel::C::D", "g2", "route-b", summary="two"))
        profile = GameProfile(game_id="g1", display_name="G", created_at="t",
                              updated_at="t")
        build_legacy_db(self.db_path, user_version=1, relations=rels,
                        profiles=(profile,))
        pre_snapshot = logical_snapshot(self.db_path)

        GameMemorySqliteAdapter(self.db_path)

        finals = published_backups(self.data_dir)
        self.assertEqual(len(finals), 1)
        final = finals[0]
        # Filename records the source version and the canonical shape.
        self.assertRegex(
            final.name,
            r"^galgame\.sqlite3\.v1\.[0-9]{8}-[0-9]{6}\.pre-arc0\.bak$")
        backup_snapshot = logical_snapshot(final)
        self.assertEqual(backup_snapshot["user_version"], 1)
        # Pre-migration logical state == backup logical state (schema + rows).
        self.assertEqual(backup_snapshot["schema"], pre_snapshot["schema"])
        self.assertEqual(backup_snapshot["rows"], pre_snapshot["rows"])
        conn = open_ro(final)
        try:
            self.assertEqual(
                conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()

    def test_backup_normalized_to_delete_journal_zero_sidecars(self):
        # §9 #27 -- published backup is DELETE-journal; reading it materializes
        # no -wal/-shm orphans.
        build_legacy_db(self.db_path, user_version=1, wal=True,
                        relations=(make_relation("rel::A::B", "g1"),))
        GameMemorySqliteAdapter(self.db_path)

        final = published_backups(self.data_dir)[0]
        conn = open_ro(final)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0],
                             "delete")
            conn.execute("SELECT count(*) FROM character_relations").fetchone()
        finally:
            conn.close()
        self.assertFalse(Path(str(final) + "-wal").exists())
        self.assertFalse(Path(str(final) + "-shm").exists())

    def test_success_path_cleanup_leaves_only_final(self):
        # §9 #36 -- after commit + unlink: final exists, tmp family fully gone.
        build_legacy_db(self.db_path, user_version=1,
                        relations=(make_relation("rel::A::B", "g1"),))
        GameMemorySqliteAdapter(self.db_path)
        self.assertEqual(len(published_backups(self.data_dir)), 1)
        self.assertEqual(tmp_family_members(self.data_dir), [])

    def test_v0_backup_records_source_version_zero(self):
        # §5.1 -- v0 legacy records v0 in the filename and stamps 0 internally.
        build_legacy_db(self.db_path, user_version=0,
                        relations=(make_relation("rel::A::B", "g1"),))
        GameMemorySqliteAdapter(self.db_path)
        final = published_backups(self.data_dir)[0]
        self.assertRegex(
            final.name,
            r"^galgame\.sqlite3\.v0\.[0-9]{8}-[0-9]{6}\.pre-arc0\.bak$")
        conn = open_ro(final)
        try:
            self.assertEqual(
                int(conn.execute("PRAGMA user_version").fetchone()[0]), 0)
        finally:
            conn.close()


class BackupProtocolFaultTest(MigrationTestBase):
    """§9 #15/#16/#17/#18/#19/#20/#22/#23/#25/#26/#32/#33/#35: fault injection
    over the backup/publication/rebuild pipeline via the approved seams."""

    def _build_v1(self, wal: bool = False) -> dict:
        build_legacy_db(self.db_path, user_version=1, wal=wal,
                        relations=(make_relation("rel::A::B", "g1", summary="keep"),),
                        profiles=(GameProfile(game_id="g1", display_name="G",
                                              created_at="t", updated_at="t"),))
        return logical_snapshot(self.db_path)

    def assert_untouched_v1(self, before: dict) -> None:
        self.assertEqual(logical_snapshot(self.db_path), before)

    def test_existing_final_is_never_overwritten(self):
        # §9 #15/#25 -- publication is os.link no-clobber; a colliding final
        # fails the migration loudly and survives byte-identical.
        before = self._build_v1()
        with mock.patch.object(GameMemorySqliteAdapter, "_backup_timestamp",
                               staticmethod(lambda: "20260710-120000")):
            backups = self.data_dir / "backups"
            backups.mkdir()
            final = backups / "galgame.sqlite3.v1.20260710-120000.pre-arc0.bak"
            final.write_bytes(b"precious existing backup")
            with self.assertRaises(GameMemoryMigrationError):
                GameMemorySqliteAdapter(self.db_path)
            self.assertEqual(final.read_bytes(), b"precious existing backup")
            self.assertEqual(published_backups(self.data_dir), [final])
        self.assertEqual(tmp_family_members(self.data_dir), [])
        self.assert_untouched_v1(before)

    def test_mid_backup_failure_original_exception_survives(self):
        # §9 #22 -- raise from Connection.backup's progress callback after at
        # least one page was copied and pages remain. This is a genuine partial
        # backup attempt, not a post-backup normalization failure.
        self._build_v1()
        conn = sqlite3.connect(self.db_path)
        conn.executemany(
            "INSERT INTO game_profiles (game_id, data) VALUES (?, ?)",
            [(f"PAD{i}", json.dumps({"pad": "x" * 8192})) for i in range(40)])
        conn.commit()
        conn.close()
        before = logical_snapshot(self.db_path)
        boom = RuntimeError("partial Connection.backup boom")
        progress_seen = []
        original_open = GameMemorySqliteAdapter._open_backup_source_conn

        class PartialBackupSource:
            def __init__(self, real):
                self.real = real

            def backup(self, target):
                def fail_after_partial_copy(status, remaining, total):
                    if 0 < remaining < total:
                        progress_seen.append((status, remaining, total))
                        raise boom
                return self.real.backup(
                    target, pages=1, progress=fail_after_partial_copy, sleep=0)

            def close(self):
                self.real.close()

        def open_partial_source(adapter):
            return PartialBackupSource(original_open(adapter))

        with mock.patch.object(GameMemorySqliteAdapter,
                               "_open_backup_source_conn",
                               open_partial_source):
            with self.assertRaises(RuntimeError) as ctx:
                GameMemorySqliteAdapter(self.db_path)
        self.assertIs(ctx.exception, boom)
        self.assertTrue(progress_seen, "backup must fail after a partial page copy")
        self.assertEqual(published_backups(self.data_dir), [])
        self.assertEqual(tmp_family_members(self.data_dir), [])
        self.assert_untouched_v1(before)

    def test_prepublication_cleanup_error_never_masks_primary_failure(self):
        # Review remediation: cleanup is secondary. Even an unexpected cleanup
        # exception must not replace the failure that aborted publication.
        before = self._build_v1()
        primary = ValueError("normalization primary")
        secondary = RuntimeError("cleanup secondary")
        with mock.patch.object(
                GameMemorySqliteAdapter, "_normalize_target_journal",
                staticmethod(mock.Mock(side_effect=primary))), \
             mock.patch.object(
                GameMemorySqliteAdapter, "_cleanup_tmp_family",
                mock.Mock(side_effect=secondary)):
            with self.assertRaises(ValueError) as ctx:
                GameMemorySqliteAdapter(self.db_path)

        self.assertIs(ctx.exception, primary)
        self.assertEqual(published_backups(self.data_dir), [])
        self.assertNotEqual(tmp_family_members(self.data_dir), [])
        self.assert_untouched_v1(before)

    def test_normalization_answering_wal_is_precommit_failure(self):
        # §9 #32 -- PRAGMA journal_mode=DELETE can silently answer 'wal'.
        before = self._build_v1()
        with mock.patch.object(GameMemorySqliteAdapter, "_normalize_target_journal",
                               staticmethod(lambda conn: "wal")):
            with self.assertRaises(GameMemoryMigrationError):
                GameMemorySqliteAdapter(self.db_path)
        self.assertEqual(published_backups(self.data_dir), [])
        self.assertEqual(tmp_family_members(self.data_dir), [])
        self.assert_untouched_v1(before)

    def test_tampered_tmp_fails_verify_and_never_publishes(self):
        # §9 #23 -- version / schema objects / row content / integrity: any
        # mismatch on the tmp aborts before publication and before migration.
        orig = GameMemorySqliteAdapter._verify_backup_tmp

        def rows_tamper(tmp: Path):
            conn = sqlite3.connect(tmp)
            conn.execute(
                "INSERT INTO game_profiles (game_id, data) VALUES ('EVIL', '{}')")
            conn.commit()
            conn.close()

        def version_tamper(tmp: Path):
            conn = sqlite3.connect(tmp)
            conn.execute("PRAGMA user_version = 9")
            conn.commit()
            conn.close()

        def schema_tamper(tmp: Path):
            conn = sqlite3.connect(tmp)
            conn.execute("CREATE TABLE evil_extra (x)")
            conn.commit()
            conn.close()

        def integrity_tamper(tmp: Path):
            size = tmp.stat().st_size
            with open(tmp, "r+b") as fh:
                fh.truncate(max(1024, size // 2))

        for name, tamper in (("rows", rows_tamper), ("version", version_tamper),
                             ("schema", schema_tamper), ("integrity", integrity_tamper)):
            with self.subTest(tamper=name):
                db = self.data_dir / f"tamper_{name}.sqlite3"
                build_legacy_db(db, user_version=1,
                                relations=(make_relation("rel::A::B", "g1"),))
                before = logical_snapshot(db)

                def tampering_verify(self_, verify_conn, migration_conn,
                                     source_version, tmp, _t=tamper):
                    _t(tmp)
                    return orig(self_, verify_conn, migration_conn,
                                source_version, tmp)

                with mock.patch.object(GameMemorySqliteAdapter, "_verify_backup_tmp",
                                       tampering_verify):
                    with self.assertRaises(
                            (GameMemoryMigrationError, sqlite3.DatabaseError)):
                        GameMemorySqliteAdapter(db)
                data_dir = db.parent
                self.assertEqual(published_backups(data_dir), [])
                self.assertEqual(logical_snapshot(db), before)

    def test_os_link_non_eexist_failure_is_precommit(self):
        # §9 #33 -- e.g. a filesystem without hardlink support.
        before = self._build_v1()
        with mock.patch("spica.adapters.game_memory.sqlite.os.link",
                        side_effect=OSError(errno.EPERM, "hardlinks unsupported")):
            with self.assertRaises(GameMemoryMigrationError):
                GameMemorySqliteAdapter(self.db_path)
        self.assertEqual(published_backups(self.data_dir), [])
        self.assertEqual(tmp_family_members(self.data_dir), [])
        self.assert_untouched_v1(before)

    def test_rebuild_fault_injection_rolls_back_everything(self):
        # §9 #16/#17 -- copy/swap/index/version/commit failure: full rollback,
        # old table intact, old user_version intact, legacy data still readable.
        orig_exec = GameMemorySqliteAdapter._exec
        prefixes = (
            "CREATE TABLE character_relations_v2",
            "INSERT INTO character_relations_v2",
            "DROP INDEX",
            "DROP TABLE character_relations",
            "ALTER TABLE character_relations_v2",
            "CREATE INDEX idx_relations_lookup",
            "PRAGMA user_version = 2",
            "COMMIT",
        )
        for i, prefix in enumerate(prefixes):
            with self.subTest(stmt=prefix):
                db = self.data_dir / f"inject_{i}.sqlite3"
                build_legacy_db(db, user_version=1,
                                relations=(make_relation("rel::A::B", "g1",
                                                         summary="keep"),))
                before = logical_snapshot(db)

                def failing_exec(conn, sql, _p=prefix):
                    if sql.strip().startswith(_p):
                        raise RuntimeError(f"injected failure at: {_p}")
                    return orig_exec(conn, sql)

                with mock.patch.object(GameMemorySqliteAdapter, "_exec",
                                       staticmethod(failing_exec)):
                    with self.assertRaises(RuntimeError):
                        GameMemorySqliteAdapter(db)

                after = logical_snapshot(db)
                self.assertEqual(after["user_version"], 1)
                self.assertEqual(after["schema"], before["schema"])
                self.assertEqual(after["rows"], before["rows"])
                # #17: legacy shape still readable the v1 way.
                conn = open_ro(db)
                try:
                    rows = conn.execute(
                        "SELECT relation_id, game_id, playthrough_id, updated_at "
                        "FROM character_relations").fetchall()
                finally:
                    conn.close()
                self.assertEqual([r[0] for r in rows], ["rel::A::B"])

    def test_tamper_between_inspection_and_lock_reverifies(self):
        # §9 #18 -- the unlocked inspection result must be re-proven under the
        # write lock; version or shape drift aborts with zero backup artifacts.
        orig = GameMemorySqliteAdapter._migrate_to_v2

        def version_drift(db_path: Path):
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA user_version = 9")
            conn.commit()
            conn.close()

        def shape_drift(db_path: Path):
            conn = sqlite3.connect(db_path)
            conn.execute("DROP TABLE character_relations")
            conn.execute("CREATE TABLE character_relations (relation_id TEXT PRIMARY KEY, data TEXT)")
            conn.commit()
            conn.close()

        for name, drift in (("version", version_drift), ("shape", shape_drift)):
            with self.subTest(drift=name):
                db = self.data_dir / f"drift_{name}.sqlite3"
                build_legacy_db(db, user_version=1,
                                relations=(make_relation("rel::A::B", "g1"),))

                def drift_then_migrate(self_, source_version, _d=drift):
                    _d(self_.db_path)
                    return orig(self_, source_version)

                with mock.patch.object(GameMemorySqliteAdapter, "_migrate_to_v2",
                                       drift_then_migrate):
                    with self.assertRaises(GameMemoryMigrationError):
                        GameMemorySqliteAdapter(db)
                # Re-verify runs before backup: nothing may be published.
                self.assertEqual(backup_artifacts(db.parent), [])

    def test_backup_snapshot_excludes_uncommitted_migration_writes(self):
        # §9 #19 -- backup = last committed pre-lock state. An uncommitted
        # migration_conn write staged before the backup must not appear in the
        # published backup, yet lands in the main DB with the rebuild COMMIT.
        self._build_v1(wal=True)
        orig_backup = GameMemorySqliteAdapter._create_pre_migration_backup

        def sentinel_then_backup(self_, migration_conn, source_version):
            migration_conn.execute(
                "INSERT INTO game_profiles (game_id, data) VALUES ('SENTINEL', '{}')")
            return orig_backup(self_, migration_conn, source_version)

        with mock.patch.object(GameMemorySqliteAdapter, "_create_pre_migration_backup",
                               sentinel_then_backup), \
             mock.patch.object(GameMemorySqliteAdapter, "_verify_backup_tmp",
                               lambda self_, *a, **k: None):
            # verify is bypassed: its source-side reader is migration_conn, which
            # legitimately sees its own uncommitted sentinel (tested in #23); here
            # we prove the backup reader does NOT.
            adapter = GameMemorySqliteAdapter(self.db_path)

        self.assertEqual(adapter.schema_version(), SCHEMA_VERSION)
        final = published_backups(self.data_dir)[0]
        conn = open_ro(final)
        try:
            backup_hit = conn.execute(
                "SELECT count(*) FROM game_profiles WHERE game_id='SENTINEL'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(backup_hit, 0)
        conn = open_ro(self.db_path)
        try:
            main_hit = conn.execute(
                "SELECT count(*) FROM game_profiles WHERE game_id='SENTINEL'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(main_hit, 1)

    def test_backup_source_is_independent_readonly_connection(self):
        # §9 #20 -- regression pin against the deadlock form: the backup source
        # must be a separate mode=ro connection, never migration_conn.
        self._build_v1()
        seen = {}
        orig_open = GameMemorySqliteAdapter._open_backup_source_conn
        orig_reverify = GameMemorySqliteAdapter._reverify_in_lock

        def spy_open(self_):
            conn = orig_open(self_)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE write_probe (x)")
            seen["source"] = conn
            return conn

        def spy_reverify(self_, conn, source_version):
            seen["migration"] = conn
            return orig_reverify(self_, conn, source_version)

        with mock.patch.object(GameMemorySqliteAdapter, "_open_backup_source_conn",
                               spy_open), \
             mock.patch.object(GameMemorySqliteAdapter, "_reverify_in_lock",
                               spy_reverify):
            GameMemorySqliteAdapter(self.db_path)
        self.assertIn("source", seen)
        self.assertIsNot(seen["source"], seen["migration"])

    def test_all_tmp_handles_closed_before_publication(self):
        # §9 #26 -- publication with any open tmp handle is the Blocker form.
        self._build_v1()
        events = []
        orig_close = GameMemorySqliteAdapter._close_conn
        orig_publish = GameMemorySqliteAdapter._publish_backup

        def spy_close(conn, role):
            events.append(("close", role))
            return orig_close(conn, role)

        def spy_publish(tmp, final):
            events.append(("publish", None))
            return orig_publish(tmp, final)

        with mock.patch.object(GameMemorySqliteAdapter, "_close_conn",
                               staticmethod(spy_close)), \
             mock.patch.object(GameMemorySqliteAdapter, "_publish_backup",
                               staticmethod(spy_publish)):
            GameMemorySqliteAdapter(self.db_path)

        publish_at = events.index(("publish", None))
        closed_before = {role for kind, role in events[:publish_at] if kind == "close"}
        self.assertLessEqual(
            {"backup_target_conn", "backup_source_conn", "backup_verify_conn"},
            closed_before)

    def test_failure_path_close_attempts_precede_first_cleanup(self):
        # §9 #35 -- event ORDER, not end state: on Linux unlink-then-close ends
        # green too, only the recorded order catches the wrong sequence.
        self._build_v1()
        events = []
        orig_close = GameMemorySqliteAdapter._close_conn
        orig_cleanup = GameMemorySqliteAdapter._cleanup_tmp_family

        def spy_close(conn, role):
            events.append(("close", role))
            return orig_close(conn, role)

        def spy_cleanup(self_, tmp):
            events.append(("cleanup", None))
            return orig_cleanup(self_, tmp)

        with mock.patch.object(GameMemorySqliteAdapter, "_close_conn",
                               staticmethod(spy_close)), \
             mock.patch.object(GameMemorySqliteAdapter, "_cleanup_tmp_family",
                               spy_cleanup), \
             mock.patch.object(GameMemorySqliteAdapter, "_verify_backup_tmp",
                               mock.Mock(side_effect=RuntimeError("verify boom"))):
            with self.assertRaises(RuntimeError):
                GameMemorySqliteAdapter(self.db_path)

        cleanup_at = events.index(("cleanup", None))
        close_indices = [i for i, (kind, _r) in enumerate(events) if kind == "close"]
        self.assertEqual(len(close_indices), 3)  # target, source, verify
        self.assertTrue(all(i < cleanup_at for i in close_indices))

    def test_close_failure_keeps_primary_exception_and_family(self):
        # §9 #35 second half -- a failing close: remaining closes still
        # attempted, primary exception preserved, family left to the stale gate.
        before = self._build_v1()
        events = []
        primary = ValueError("primary boom")
        orig_close = GameMemorySqliteAdapter._close_conn

        def failing_close(conn, role):
            events.append(("close", role))
            if role == "backup_target_conn":
                raise RuntimeError("close boom")
            return orig_close(conn, role)

        with mock.patch.object(GameMemorySqliteAdapter, "_normalize_target_journal",
                               staticmethod(mock.Mock(side_effect=primary))), \
             mock.patch.object(GameMemorySqliteAdapter, "_close_conn",
                               staticmethod(failing_close)):
            with self.assertRaises(ValueError) as ctx:
                GameMemorySqliteAdapter(self.db_path)
        self.assertIs(ctx.exception, primary)
        roles = [r for _k, r in events]
        self.assertIn("backup_target_conn", roles)
        self.assertIn("backup_source_conn", roles)  # still attempted after failure
        # Family retained for the stale gate rather than masking the failure.
        self.assertNotEqual(tmp_family_members(self.data_dir), [])
        self.assert_untouched_v1(before)


class StaleTmpGateTest(MigrationTestBase):
    """§9 #24/#28/#34: startup gate over crashed/leftover tmp artifact families."""

    def _plant_v1(self):
        build_legacy_db(self.db_path, user_version=1,
                        relations=(make_relation("rel::A::B", "g1"),))
        return logical_snapshot(self.db_path)

    def test_stale_family_blocks_startup_and_preserves_scene(self):
        # §9 #24 -- fail loud, keep the scene, include manual guidance.
        for member_name in ("galgame.sqlite3.v1.20260101-000000.pre-arc0.tmp",
                            "galgame.sqlite3.v1.20260101-000000.pre-arc0.tmp-wal",
                            "galgame.sqlite3.v1.20260101-000000.pre-arc0.tmp-shm",
                            "galgame.sqlite3.v1.20260101-000000.pre-arc0.tmp-journal"):
            with self.subTest(member=member_name):
                data_dir = self.data_dir / member_name.replace(".", "_")
                data_dir.mkdir()
                db = data_dir / "galgame.sqlite3"
                build_legacy_db(db, user_version=1,
                                relations=(make_relation("rel::A::B", "g1"),))
                before = logical_snapshot(db)
                backups = data_dir / "backups"
                backups.mkdir()
                member = backups / member_name
                member.write_bytes(b"crash residue")

                with self.assertRaises(GameMemoryMigrationError) as ctx:
                    GameMemorySqliteAdapter(db)
                msg = str(ctx.exception)
                self.assertIn(member_name, msg)
                self.assertIn("manually", msg)
                self.assertTrue(member.exists())  # scene preserved
                self.assertEqual(logical_snapshot(db), before)
                self.assertEqual(published_backups(data_dir), [])

    def test_double_name_samefile_gets_safe_delete_guidance(self):
        # §9 #34 -- same inode = post-commit residue: safe-delete guidance OK.
        self._plant_v1()
        backups = self.data_dir / "backups"
        backups.mkdir()
        tmp = backups / "galgame.sqlite3.v1.20260101-000000.pre-arc0.tmp"
        final = backups / "galgame.sqlite3.v1.20260101-000000.pre-arc0.bak"
        tmp.write_bytes(b"published then unlink failed")
        os.link(tmp, final)

        with self.assertRaises(GameMemoryMigrationError) as ctx:
            GameMemorySqliteAdapter(self.db_path)
        self.assertIn("safely deleted", str(ctx.exception))
        self.assertTrue(tmp.exists() and final.exists())

    def test_double_name_different_inode_never_claims_safe(self):
        # §9 #34 -- same names, different files: no safe-delete claim allowed.
        self._plant_v1()
        backups = self.data_dir / "backups"
        backups.mkdir()
        tmp = backups / "galgame.sqlite3.v1.20260101-000000.pre-arc0.tmp"
        final = backups / "galgame.sqlite3.v1.20260101-000000.pre-arc0.bak"
        tmp.write_bytes(b"one file")
        final.write_bytes(b"a different file")

        with self.assertRaises(GameMemoryMigrationError) as ctx:
            GameMemorySqliteAdapter(self.db_path)
        msg = str(ctx.exception)
        self.assertNotIn("safely deleted", msg)
        self.assertTrue(tmp.exists() and final.exists())

    def test_post_commit_cleanup_failure_keeps_final_then_gates_next_start(self):
        # §9 #28 review remediation -- exercise a REAL later-member unlink
        # failure after publication. Deleting base tmp first would destroy the
        # samefile proof and make the next startup misdiagnose mid-backup.
        self._plant_v1()
        original_publish = GameMemorySqliteAdapter._publish_backup
        original_unlink = Path.unlink
        captured = {}
        unlink_attempts = []

        def publish_then_plant_sidecars(tmp, final):
            original_publish(tmp, final)
            captured["tmp"] = tmp
            for suffix in ("-wal", "-shm", "-journal"):
                Path(str(tmp) + suffix).write_bytes(suffix.encode("ascii"))

        def fail_later_sidecar(path, *args, **kwargs):
            unlink_attempts.append(path)
            if str(path).endswith(".pre-arc0.tmp-shm"):
                raise PermissionError("injected tmp-shm unlink failure")
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(
                GameMemorySqliteAdapter, "_publish_backup",
                staticmethod(publish_then_plant_sidecars)), \
             mock.patch.object(Path, "unlink", fail_later_sidecar):
            adapter = GameMemorySqliteAdapter(self.db_path)
        self.assertEqual(adapter.schema_version(), SCHEMA_VERSION)
        finals = published_backups(self.data_dir)
        self.assertEqual(len(finals), 1)
        tmp = captured["tmp"]
        self.assertTrue(tmp.exists())
        self.assertTrue(os.path.samefile(tmp, finals[0]))
        self.assertTrue(Path(str(tmp) + "-shm").exists())
        self.assertFalse(Path(str(tmp) + "-wal").exists())
        self.assertFalse(Path(str(tmp) + "-journal").exists())
        self.assertIn(Path(str(tmp) + "-journal"), unlink_attempts)

        with self.assertRaises(GameMemoryMigrationError) as ctx:
            GameMemorySqliteAdapter(self.db_path)
        self.assertIn("safely deleted", str(ctx.exception))
        self.assertTrue(tmp.exists() and finals[0].exists())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sidecar_state(path: Path) -> dict:
    return {suffix: Path(str(path) + suffix).exists()
            for suffix in ("-wal", "-shm", "-journal")}


def corrupt_integrity_with_duplicate_index_rootpage(db_path: Path) -> None:
    """Create a real integrity failure while leaving endpoint SQL readable."""
    conn = sqlite3.connect(db_path)
    try:
        roots = dict(conn.execute(
            "SELECT name, rootpage FROM sqlite_master "
            "WHERE name IN ('idx_story_lines_lookup', 'idx_summaries_lookup')"))
        if set(roots) != {"idx_story_lines_lookup", "idx_summaries_lookup"}:
            raise AssertionError("integrity fixture indexes are missing")
        schema_version = int(
            conn.execute("PRAGMA schema_version").fetchone()[0])
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute(
            "UPDATE sqlite_master SET rootpage=? WHERE name='idx_story_lines_lookup'",
            (roots["idx_summaries_lookup"],))
        conn.execute(f"PRAGMA schema_version={schema_version + 1}")
        conn.execute("PRAGMA writable_schema=OFF")
        conn.commit()
    finally:
        conn.close()


def run_helper(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """#41 discipline: unrelated cwd + clean PYTHONPATH (stdlib-only pin)."""
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": ""}
    return subprocess.run(
        [sys.executable, str(PREFLIGHT_HELPER), *args],
        capture_output=True, text=True, env=env, cwd=cwd)


def load_helper_module():
    """Load the stdlib-only helper for approved connection fault injection."""
    spec = importlib.util.spec_from_file_location(
        "arc0_restore_preflight_under_test", PREFLIGHT_HELPER)
    if spec is None or spec.loader is None:  # pragma: no cover - import machinery
        raise RuntimeError("unable to load AR-C0 restore helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RestorePreflightHelperTest(MigrationTestBase):
    """§9 #38/#41/#42: real CLI acceptance of scripts/arc0_restore_preflight.py."""

    def setUp(self) -> None:
        super().setUp()
        self.unrelated_cwd = self.data_dir / "unrelated_cwd"
        self.unrelated_cwd.mkdir()

    def _migrated_pair(self) -> tuple[Path, Path]:
        """Real v1 fixture -> adapter migration -> (published candidate, current v2)."""
        build_legacy_db(
            self.db_path, user_version=1,
            relations=(make_relation("rel::A::B", "g1", summary="keep"),),
            profiles=(GameProfile(game_id="g1", display_name="G",
                                  created_at="t", updated_at="t"),),
            if_not_exists=True)
        GameMemorySqliteAdapter(self.db_path)
        candidate = published_backups(self.data_dir)[0]
        return candidate, self.db_path

    @staticmethod
    def _staged_current(current: Path, stage_dir: Path) -> Path:
        """Mimic runbook 0c: copy the whole current family into staging."""
        stage_dir.mkdir(parents=True, exist_ok=True)
        staged = stage_dir / current.name
        staged.write_bytes(current.read_bytes())
        for suffix in ("-wal", "-shm", "-journal"):
            src = Path(str(current) + suffix)
            if src.exists():
                Path(str(staged) + suffix).write_bytes(src.read_bytes())
        return staged

    @staticmethod
    def _replace_relations_table(db_path: Path, ddl: str) -> None:
        rebuild_fixture_table(
            db_path,
            "character_relations",
            ddl,
            ("relation_id", "game_id", "playthrough_id", "updated_at", "data"),
            (("idx_relations_lookup",
              "CREATE INDEX idx_relations_lookup "
              "ON character_relations(game_id, playthrough_id)"),))

    @staticmethod
    def _replace_game_profiles_table(db_path: Path, ddl: str) -> None:
        rebuild_fixture_table(
            db_path,
            "game_profiles",
            ddl,
            ("game_id", "last_played_at", "data"))

    def test_second_open_failure_closes_candidate_connection(self):
        # Review remediation: every successfully acquired helper connection is
        # registered for close before the next acquisition can fail.
        helper = load_helper_module()

        class CandidateConnection:
            closed = False

            def close(self):
                self.closed = True

        candidate = CandidateConnection()
        with mock.patch.object(helper, "open_ro", return_value=candidate), \
             mock.patch.object(helper, "open_rw",
                               side_effect=OSError("current open failed")):
            status = helper.main(["preflight", "candidate", "current", "1"])
        self.assertEqual(status, 1)
        self.assertTrue(candidate.closed)

    def test_candidate_close_failure_still_attempts_current_close(self):
        # Finding 1 red #3: candidate close is secondary to the primary Reject.
        helper = load_helper_module()
        closed = []
        stderr = io.StringIO()

        class CandidateConnection:
            def close(self):
                closed.append("candidate")
                raise OSError("SECONDARY candidate close failure")

        class CurrentConnection:
            def close(self):
                closed.append("current")

        with mock.patch.object(helper, "open_ro",
                               return_value=CandidateConnection()), \
             mock.patch.object(helper, "open_rw",
                               return_value=CurrentConnection()), \
             mock.patch.object(helper, "user_version",
                               side_effect=helper.Reject(
                                   "PRIMARY contract rejection")), \
             mock.patch.object(helper.sys, "stderr", stderr):
            status = helper.main(
                ["preflight", "candidate", "current", "1"])
        self.assertEqual(status, 1)
        self.assertCountEqual(closed, ["candidate", "current"])
        self.assertIn("PRIMARY contract rejection", stderr.getvalue())
        self.assertNotIn("SECONDARY candidate close failure", stderr.getvalue())

    def test_current_close_failure_still_attempts_candidate_close(self):
        # Finding 1 red #1: ExitStack closes current first; that failure must
        # not prevent the earlier candidate registration from being attempted.
        helper = load_helper_module()
        closed = []

        class CandidateConnection:
            def close(self):
                closed.append("candidate")

        class CurrentConnection:
            def close(self):
                closed.append("current")
                raise OSError("SECONDARY current close failure")

        with mock.patch.object(helper, "open_ro",
                               return_value=CandidateConnection()), \
             mock.patch.object(helper, "open_rw",
                               return_value=CurrentConnection()), \
             mock.patch.object(helper, "user_version",
                               side_effect=helper.Reject(
                                   "PRIMARY contract rejection")):
            status = helper.main(
                ["preflight", "candidate", "current", "1"])
        self.assertEqual(status, 1)
        self.assertEqual(closed, ["current", "candidate"])

    def test_preflight_primary_reject_survives_current_close_failure(self):
        # Finding 1 red #2: CLI output must report the contract failure, not the
        # secondary close failure raised during cleanup.
        helper = load_helper_module()
        stderr = io.StringIO()

        class CandidateConnection:
            def close(self):
                pass

        class CurrentConnection:
            def close(self):
                raise OSError("SECONDARY current close failure")

        with mock.patch.object(helper, "open_ro",
                               return_value=CandidateConnection()), \
             mock.patch.object(helper, "open_rw",
                               return_value=CurrentConnection()), \
             mock.patch.object(helper, "user_version",
                               side_effect=helper.Reject(
                                   "PRIMARY contract rejection")), \
             mock.patch.object(helper.sys, "stderr", stderr):
            status = helper.main(
                ["preflight", "candidate", "current", "1"])
        self.assertEqual(status, 1)
        self.assertIn("PRIMARY contract rejection", stderr.getvalue())
        self.assertNotIn("SECONDARY current close failure", stderr.getvalue())

    def test_verify_restored_primary_reject_survives_close_failure(self):
        # Finding 1 red #4: verify-restored follows the same primary-exception
        # precedence as preflight.
        helper = load_helper_module()
        stderr = io.StringIO()

        class RestoredConnection:
            def close(self):
                raise OSError("SECONDARY restored close failure")

        with mock.patch.object(helper, "open_ro",
                               return_value=RestoredConnection()), \
             mock.patch.object(helper, "user_version",
                               side_effect=helper.Reject(
                                   "PRIMARY restored rejection")), \
             mock.patch.object(helper.sys, "stderr", stderr):
            status = helper.main(
                ["verify-restored", "restored", "1"])
        self.assertEqual(status, 1)
        self.assertIn("PRIMARY restored rejection", stderr.getvalue())
        self.assertNotIn("SECONDARY restored close failure", stderr.getvalue())

    def test_verify_restored_close_only_failure_is_reported(self):
        # Finding 1 red #5: cleanup failures are suppressed only while another
        # exception is already propagating; alone they remain visible failures.
        helper = load_helper_module()
        stderr = io.StringIO()

        class RestoredConnection:
            def close(self):
                raise OSError("SECONDARY close-only failure")

        with mock.patch.object(helper, "open_ro",
                               return_value=RestoredConnection()), \
             mock.patch.object(helper, "user_version", return_value=1), \
             mock.patch.object(helper, "check_integrity", return_value=None), \
             mock.patch.object(helper.sys, "stderr", stderr):
            status = helper.main(
                ["verify-restored", "restored", "1"])
        self.assertEqual(status, 1)
        self.assertIn("SECONDARY close-only failure", stderr.getvalue())

    def test_helper_rejects_candidate_inline_constraint(self):
        # Finding 2 red #6: canonical PRAGMA endpoint plus a candidate CHECK is
        # still noncanonical v1 DDL; relation-delta comparison cannot detect it.
        candidate, current = self._migrated_pair()
        bad_candidate = self.data_dir / "candidate_inline_check.bak"
        bad_candidate.write_bytes(candidate.read_bytes())
        staged = self._staged_current(
            current, self.data_dir / "stage_candidate_inline")
        self._replace_relations_table(
            bad_candidate, MALFORMED_V1_RELATIONS_CHECK_DDL)

        result = run_helper(
            "preflight", str(bad_candidate), str(staged), "1",
            cwd=self.unrelated_cwd)

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("candidate", result.stderr)

    def test_helper_rejects_current_inline_constraint(self):
        # Finding 2 red #7: current-v2 CHECK and FOREIGN KEY variants each retain
        # the exact PRAGMA endpoint and must be rejected independently.
        candidate, current = self._migrated_pair()
        for case in ("check", "foreign_key"):
            with self.subTest(case=case):
                staged = self._staged_current(
                    current, self.data_dir / f"stage_current_inline_{case}")
                self._replace_relations_table(
                    staged, MALFORMED_V2_RELATIONS_DDL[case])

                result = run_helper(
                    "preflight", str(candidate), str(staged), "1",
                    cwd=self.unrelated_cwd)

                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("current", result.stderr)

    def test_helper_rejects_matching_inline_constraints_on_both_endpoints(self):
        # Finding 2 red #8: the same CHECK on both relation endpoints must not
        # cancel out as an allowed v1->v2 schema delta.
        candidate, current = self._migrated_pair()
        bad_candidate = self.data_dir / "candidate_matching_check.bak"
        bad_candidate.write_bytes(candidate.read_bytes())
        staged = self._staged_current(
            current, self.data_dir / "stage_matching_check")
        self._replace_relations_table(
            bad_candidate, MALFORMED_V1_RELATIONS_CHECK_DDL)
        self._replace_relations_table(
            staged, MALFORMED_V2_RELATIONS_DDL["check"])

        result = run_helper(
            "preflight", str(bad_candidate), str(staged), "1",
            cwd=self.unrelated_cwd)

        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_helper_rejects_matching_unrelated_inline_constraints(self):
        # Both endpoints must independently validate all eight required tables.
        # Raw schema parity alone would accept this shared unrelated-table CHECK.
        candidate, current = self._migrated_pair()
        bad_candidate = self.data_dir / "candidate_matching_profile_check.bak"
        bad_candidate.write_bytes(candidate.read_bytes())
        staged = self._staged_current(
            current, self.data_dir / "stage_matching_profile_check")
        ddl = MALFORMED_V1_GAME_PROFILES_DDL["check"]
        self._replace_game_profiles_table(bad_candidate, ddl)
        self._replace_game_profiles_table(staged, ddl)

        result = run_helper(
            "preflight", str(bad_candidate), str(staged), "1",
            cwd=self.unrelated_cwd)

        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_helper_rejects_quoted_identifier_impersonating_keyword(self):
        # Token-aware means quoted identifiers cannot collapse into bare SQL
        # keywords. SQLite parses TEXT "NOT" NULL as a nullable declared type,
        # yet the old fingerprint equated it with canonical TEXT NOT NULL; raw
        # candidate/current parity cannot save the helper when both share it.
        candidate, current = self._migrated_pair()
        bad_candidate = self.data_dir / "candidate_quoted_keyword.bak"
        bad_candidate.write_bytes(candidate.read_bytes())
        staged = self._staged_current(
            current, self.data_dir / "stage_quoted_keyword")
        for db in (bad_candidate, staged):
            self._replace_game_profiles_table(
                db, MALFORMED_QUOTED_KEYWORD_GAME_PROFILES_DDL)

        conn = open_ro(bad_candidate)
        try:
            data_column = next(
                row for row in conn.execute(
                    "PRAGMA table_xinfo(game_profiles)").fetchall()
                if row[1] == "data")
        finally:
            conn.close()
        self.assertEqual(data_column[2], 'TEXT "NOT"')
        self.assertEqual(data_column[3], 0)

        result = run_helper(
            "preflight", str(bad_candidate), str(staged), "1",
            cwd=self.unrelated_cwd)

        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_helper_rejects_python_only_whitespace_in_ddl(self):
        # Python's str.isspace() is broader than SQLite's lexer. SQLite treats
        # NBSP as part of the declared type, not as token-separating whitespace;
        # dropping it would make this nullable column impersonate TEXT NOT NULL.
        candidate, current = self._migrated_pair()
        bad_candidate = self.data_dir / "candidate_nbsp_keyword.bak"
        bad_candidate.write_bytes(candidate.read_bytes())
        staged = self._staged_current(
            current, self.data_dir / "stage_nbsp_keyword")
        for db in (bad_candidate, staged):
            self._replace_game_profiles_table(
                db, MALFORMED_NON_SQLITE_WHITESPACE_GAME_PROFILES_DDL)

        conn = open_ro(bad_candidate)
        try:
            data_column = next(
                row for row in conn.execute(
                    "PRAGMA table_xinfo(game_profiles)").fetchall()
                if row[1] == "data")
        finally:
            conn.close()
        self.assertEqual(data_column[2], "TEXT\N{NO-BREAK SPACE}NOT")
        self.assertEqual(data_column[3], 0)

        result = run_helper(
            "preflight", str(bad_candidate), str(staged), "1",
            cwd=self.unrelated_cwd)

        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_helper_rejects_python_unicode_fold_collision(self):
        # SQLite folds identifier case in ASCII only. Python upper() turns the
        # dotless-i token PRıMARY into PRIMARY, while SQLite parses it as part of
        # the declared type and does not create the canonical primary key.
        candidate, current = self._migrated_pair()
        bad_candidate = self.data_dir / "candidate_unicode_fold.bak"
        bad_candidate.write_bytes(candidate.read_bytes())
        staged = self._staged_current(
            current, self.data_dir / "stage_unicode_fold")
        for db in (bad_candidate, staged):
            self._replace_game_profiles_table(
                db, MALFORMED_UNICODE_FOLD_GAME_PROFILES_DDL)

        conn = open_ro(bad_candidate)
        try:
            game_id_column = next(
                row for row in conn.execute(
                    "PRAGMA table_xinfo(game_profiles)").fetchall()
                if row[1] == "game_id")
        finally:
            conn.close()
        self.assertEqual(
            game_id_column[2], "TEXT PR\N{LATIN SMALL LETTER DOTLESS I}MARY KEY")
        self.assertEqual(game_id_column[5], 0)

        result = run_helper(
            "preflight", str(bad_candidate), str(staged), "1",
            cwd=self.unrelated_cwd)

        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_preflight_passes_on_true_preimage_and_is_side_effect_free(self):
        # #41 pass path + byte/sidecar invariance (read-only discipline pin).
        candidate, current = self._migrated_pair()
        staged = self._staged_current(current, self.data_dir / "stage")
        cand_hash, cand_side = sha256(candidate), sidecar_state(candidate)

        result = run_helper("preflight", str(candidate), str(staged), "1",
                            cwd=self.unrelated_cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sha256(candidate), cand_hash)
        self.assertEqual(sidecar_state(candidate), cand_side)

    def test_preflight_quotes_dynamic_user_tables_and_detects_row_tampering(self):
        # Final-review remediation / §9 #30/#41: helper parity must quote every
        # sqlite_master table name, while still comparing every row after doing
        # so. Both source versions exercise real production backup/current pairs.
        rows = (("a", "original"), ("b", "stable"))
        for version in (0, 1):
            for case, _table_name, quoted_table in EXTRA_IDENTIFIER_TABLES:
                with self.subTest(version=version, case=case):
                    case_dir = self.data_dir / f"helper_identifier_v{version}_{case}"
                    case_dir.mkdir()
                    db = case_dir / "galgame.sqlite3"
                    build_legacy_db(db, user_version=version)
                    conn = sqlite3.connect(db)
                    try:
                        conn.execute(
                            f"CREATE TABLE {quoted_table} "
                            "(id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                        conn.executemany(
                            f"INSERT INTO {quoted_table} VALUES (?, ?)", rows)
                        conn.commit()
                    finally:
                        conn.close()
                    GameMemorySqliteAdapter(db)
                    candidate = published_backups(case_dir)[0]
                    staged = self._staged_current(
                        db, case_dir / "stage_current")

                    result = run_helper(
                        "preflight", str(candidate), str(staged), str(version),
                        cwd=self.unrelated_cwd)
                    self.assertEqual(result.returncode, 0, result.stderr)

                    conn = sqlite3.connect(staged)
                    try:
                        conn.execute(
                            f"UPDATE {quoted_table} SET payload='tampered' "
                            "WHERE id='a'")
                        conn.commit()
                    finally:
                        conn.close()
                    result = run_helper(
                        "preflight", str(candidate), str(staged), str(version),
                        cwd=self.unrelated_cwd)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("roll forward", result.stderr)

    def test_verify_restored_passes_and_is_side_effect_free(self):
        candidate, _current = self._migrated_pair()
        restored = self.data_dir / "restored.sqlite3"
        restored.write_bytes(candidate.read_bytes())
        before_hash, before_side = sha256(restored), sidecar_state(restored)

        result = run_helper("verify-restored", str(restored), "1",
                            cwd=self.unrelated_cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sha256(restored), before_hash)
        self.assertEqual(sidecar_state(restored), before_side)

    def test_current_integrity_failure_is_rejected(self):
        # §9 #41: endpoint/schema/parity can remain readable while the staged
        # current copy has a real duplicate-page integrity failure.
        candidate, current = self._migrated_pair()
        staged = self._staged_current(current, self.data_dir / "stage_bad_integrity")
        corrupt_integrity_with_duplicate_index_rootpage(staged)

        result = run_helper("preflight", str(candidate), str(staged), "1",
                            cwd=self.unrelated_cwd)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("current", result.stderr)

    def test_verify_restored_wrong_version_and_integrity_failures(self):
        # §9 #41: both post-copy gates are real CLI failures, not unit-only
        # checks hidden behind the preflight path.
        candidate, _current = self._migrated_pair()

        wrong_version = self.data_dir / "restored_wrong_version.sqlite3"
        wrong_version.write_bytes(candidate.read_bytes())
        result = run_helper("verify-restored", str(wrong_version), "0",
                            cwd=self.unrelated_cwd)
        self.assertNotEqual(result.returncode, 0)

        corrupt = self.data_dir / "restored_bad_integrity.sqlite3"
        corrupt.write_bytes(candidate.read_bytes())
        corrupt_integrity_with_duplicate_index_rootpage(corrupt)
        result = run_helper("verify-restored", str(corrupt), "1",
                            cwd=self.unrelated_cwd)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restored", result.stderr)

    def test_expected_v_reconciliation_and_domain(self):
        # #41: EXPECTED_V outside {0,1} and version mismatches all fail nonzero.
        candidate, current = self._migrated_pair()
        staged = self._staged_current(current, self.data_dir / "stage")
        for bad in ("2", "3", "x"):
            with self.subTest(expected_v=bad):
                result = run_helper("preflight", str(candidate), str(staged), bad,
                                    cwd=self.unrelated_cwd)
                self.assertNotEqual(result.returncode, 0)
        # Internal user_version disagreeing with EXPECTED_V (candidate is v1).
        result = run_helper("preflight", str(candidate), str(staged), "0",
                            cwd=self.unrelated_cwd)
        self.assertNotEqual(result.returncode, 0)

    def test_endpoint_contract_rejections(self):
        # §9 #42 -- mislabeled-v2 candidate / current not exact v2 / delta missing.
        candidate, current = self._migrated_pair()

        with self.subTest(case="mislabeled v2 candidate"):
            fake = self.data_dir / "mislabeled.bak"
            fake.write_bytes(current.read_bytes())  # v2 shape...
            conn = sqlite3.connect(fake)
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA user_version = 1")  # ...stamped as v1
            conn.commit()
            conn.close()
            staged = self._staged_current(current, self.data_dir / "stage_a")
            result = run_helper("preflight", str(fake), str(staged), "1",
                                cwd=self.unrelated_cwd)
            self.assertNotEqual(result.returncode, 0)

        with self.subTest(case="current stamped v1"):
            staged = self._staged_current(current, self.data_dir / "stage_b")
            conn = sqlite3.connect(staged)
            conn.execute("PRAGMA user_version = 1")
            conn.commit()
            conn.close()
            result = run_helper("preflight", str(candidate), str(staged), "1",
                                cwd=self.unrelated_cwd)
            self.assertNotEqual(result.returncode, 0)

        with self.subTest(case="current stamped v3 (destructive downgrade)"):
            staged = self._staged_current(current, self.data_dir / "stage_c")
            conn = sqlite3.connect(staged)
            conn.execute("PRAGMA user_version = 3")
            conn.commit()
            conn.close()
            result = run_helper("preflight", str(candidate), str(staged), "1",
                                cwd=self.unrelated_cwd)
            self.assertNotEqual(result.returncode, 0)

        with self.subTest(case="current has v1 shape (delta missing)"):
            # A "current" that is byte-wise the candidate re-stamped to 2: v1
            # shape under a v2 stamp -- the expected v1->v2 delta is absent.
            staged_dir = self.data_dir / "stage_d"
            staged_dir.mkdir()
            staged = staged_dir / "galgame.sqlite3"
            staged.write_bytes(candidate.read_bytes())
            conn = sqlite3.connect(staged)
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
            conn.close()
            result = run_helper("preflight", str(candidate), str(staged), "1",
                                cwd=self.unrelated_cwd)
            self.assertNotEqual(result.returncode, 0)

    def test_matching_malformed_endpoint_indexes_are_rejected(self):
        # §9 #42: delta comparison cannot catch the same malformed lookup on
        # both endpoints. Each endpoint must independently match its canonical
        # index_list/index_xinfo contract.
        candidate, current = self._migrated_pair()
        for case, ddl in MALFORMED_RELATIONS_LOOKUP_DDL.items():
            with self.subTest(case=case):
                bad_candidate = self.data_dir / f"candidate_{case}.bak"
                bad_candidate.write_bytes(candidate.read_bytes())
                staged = self._staged_current(
                    current, self.data_dir / f"stage_same_bad_{case}")
                replace_relations_lookup(bad_candidate, ddl)
                replace_relations_lookup(staged, ddl)

                result = run_helper(
                    "preflight", str(bad_candidate), str(staged), "1",
                    cwd=self.unrelated_cwd)
                self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_preflight_accepts_equivalent_lookup_index_sql_spellings(self):
        # Final-review remediation / §9 #41/#42: lookup structure is already
        # independently pinned by three PRAGMA lenses on both endpoints. Raw
        # sqlite_master spelling (including ALTER's quotes) is not a new delta.
        cases = tuple(EQUIVALENT_RELATIONS_LOOKUP_DDL) + ("alter_rename",)
        for case in cases:
            with self.subTest(case=case):
                case_dir = self.data_dir / f"lookup_spelling_{case}"
                case_dir.mkdir()
                db = case_dir / "galgame.sqlite3"
                build_legacy_db(
                    db, user_version=1,
                    relations=(make_relation("rel::A::B", "g1"),),
                    if_not_exists=True)
                if case == "alter_rename":
                    conn = sqlite3.connect(db)
                    try:
                        conn.execute(
                            "ALTER TABLE character_relations "
                            "RENAME TO character_relations_fixture_alias")
                        conn.execute(
                            "ALTER TABLE character_relations_fixture_alias "
                            "RENAME TO character_relations")
                        conn.commit()
                    finally:
                        conn.close()
                else:
                    replace_relations_lookup(
                        db, EQUIVALENT_RELATIONS_LOOKUP_DDL[case])

                adapter = GameMemorySqliteAdapter(db)
                self.assertEqual(adapter.schema_version(), SCHEMA_VERSION)
                candidate = published_backups(case_dir)[0]
                staged = self._staged_current(
                    db, case_dir / "stage_current")

                index_sql = (
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='index' AND name='idx_relations_lookup'")
                candidate_conn = open_ro(candidate)
                current_conn = open_ro(staged)
                try:
                    candidate_sql = candidate_conn.execute(index_sql).fetchone()[0]
                    current_sql = current_conn.execute(index_sql).fetchone()[0]
                finally:
                    current_conn.close()
                    candidate_conn.close()
                self.assertNotEqual(candidate_sql, current_sql)

                result = run_helper(
                    "preflight", str(candidate), str(staged), "1",
                    cwd=self.unrelated_cwd)

                self.assertEqual(result.returncode, 0, result.stderr)

    def test_parity_and_projection_rejections(self):
        # #41: unrelated-table parity / relations projection / schema delta /
        # integrity each independently force a nonzero exit.
        candidate, current = self._migrated_pair()

        def fresh_stage(tag: str) -> Path:
            return self._staged_current(current, self.data_dir / f"stage_{tag}")

        with self.subTest(case="post-migration write in unrelated table"):
            staged = fresh_stage("w")
            conn = sqlite3.connect(staged)
            conn.execute(
                "INSERT INTO game_profiles (game_id, data) VALUES ('POST', '{}')")
            conn.commit()
            conn.close()
            result = run_helper("preflight", str(candidate), str(staged), "1",
                                cwd=self.unrelated_cwd)
            self.assertNotEqual(result.returncode, 0)

        with self.subTest(case="surviving-row projection mismatch"):
            staged = fresh_stage("p")
            conn = sqlite3.connect(staged)
            conn.execute("UPDATE character_relations SET updated_at='9999'")
            conn.commit()
            conn.close()
            result = run_helper("preflight", str(candidate), str(staged), "1",
                                cwd=self.unrelated_cwd)
            self.assertNotEqual(result.returncode, 0)

        # §9 #31 tail -- rows remain equal, but each kind of candidate-only
        # schema object (index / trigger / table) is rejected before any swap.
        extra_schema_ddls = {
            "index": "CREATE INDEX evil_idx ON game_profiles(last_played_at)",
            "trigger": (
                "CREATE TRIGGER evil_trigger AFTER INSERT ON game_profiles "
                "BEGIN SELECT 1; END"),
            "table": "CREATE TABLE evil_table (value TEXT)",
        }
        for i, (kind, ddl) in enumerate(extra_schema_ddls.items()):
            with self.subTest(case=f"candidate extra {kind}"):
                doctored = self.data_dir / (
                    f"galgame.sqlite3.v1.2020010{i + 1}-000000.pre-arc0.bak")
                doctored.write_bytes(candidate.read_bytes())
                conn = sqlite3.connect(doctored)
                conn.execute(ddl)
                conn.commit()
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.close()
                staged = fresh_stage(f"schema_{kind}")
                result = run_helper(
                    "preflight", str(doctored), str(staged), "1",
                    cwd=self.unrelated_cwd)
                self.assertNotEqual(result.returncode, 0)

        with self.subTest(case="candidate integrity broken"):
            broken = self.data_dir / "galgame.sqlite3.v1.20200102-000000.pre-arc0.bak"
            data = candidate.read_bytes()
            broken.write_bytes(data[: max(1024, len(data) // 2)])  # torn file
            staged = fresh_stage("i")
            result = run_helper("preflight", str(broken), str(staged), "1",
                                cwd=self.unrelated_cwd)
            self.assertNotEqual(result.returncode, 0)

    def test_hot_journal_recovery_happens_on_staged_copy_only(self):
        # §9 #38 -- preflight recovers the STAGED current copy; the real family
        # bytes stay untouched. The mode=ro failure form on a hot-journal DB is
        # pinned as a known boundary.
        candidate, current = self._migrated_pair()
        # Manufacture a genuinely HOT rollback journal: a 1-page cache forces
        # the uncommitted transaction to spill pages into the DB file, which
        # journals the pre-images and finalizes the journal header. A journal
        # without spilled pages is considered cold and triggers no recovery.
        conn = sqlite3.connect(current)
        conn.isolation_level = None
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA cache_size=1")
        conn.execute("BEGIN IMMEDIATE")
        for i in range(200):
            conn.execute(
                "INSERT INTO game_profiles (game_id, data) VALUES (?, ?)",
                (f"HOT{i}", json.dumps({"pad": "x" * 512})))
        journal = Path(str(current) + "-journal")
        self.assertTrue(journal.exists() and journal.stat().st_size > 0)
        hot_db = current.read_bytes()
        hot_journal = journal.read_bytes()
        conn.execute("ROLLBACK")
        conn.close()
        current.write_bytes(hot_db)
        journal.write_bytes(hot_journal)

        # Known boundary pin: reading a hot-journal DB via mode=ro fails.
        ro = open_ro(current)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute("SELECT count(*) FROM game_profiles").fetchone()
        finally:
            ro.close()

        main_hash, journal_hash = sha256(current), sha256(journal)
        staged = self._staged_current(current, self.data_dir / "stage")

        result = run_helper("preflight", str(candidate), str(staged), "1",
                            cwd=self.unrelated_cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Real family bytes untouched; the journal was replayed on the copy only.
        self.assertEqual(sha256(current), main_hash)
        self.assertEqual(sha256(journal), journal_hash)


# §10 restore-first executable unit, rendered verbatim modulo @TOKENS@. The
# unit itself carries set -euo pipefail (v1.5: fail-fast must not depend on the
# caller's shell). @POST_PREFLIGHT_HOOK@ is empty for real drills; #31/#39 use
# it to simulate the candidate-replacement window the staging design closes.
RESTORE_UNIT = """#!/usr/bin/env bash
set -euo pipefail
# AR-C0 §10 restore unit. Precondition: all Spica processes stopped.
data_dir="@DATA_DIR@"
REPO_ROOT="@REPO_ROOT@"
BACKUP="@BACKUP@"

STAGE=""; Q=""
trap 'echo "FAILED: staging retained at ${STAGE:-<none>}; quarantine at ${Q:-<not created>}" >&2' ERR

# 0a. strict, complete, fail-closed canonical-name validation + EXPECTED_V.
BASE="$(basename -- "$BACKUP")"
if [[ "$BASE" =~ ^galgame\\.sqlite3\\.v([01])\\.[0-9]{8}-[0-9]{6}\\.pre-arc0\\.bak$ ]]; then
  EXPECTED_V="${BASH_REMATCH[1]}"
else
  echo "REJECT: not a canonical pre-arc0 backup name: $BASE" >&2
  exit 1
fi

# 0b. unique, private, no-clobber staging; all later steps consume ONLY staging.
STAGE="$(mktemp -d -- "$data_dir/arc0-restore-stage.XXXXXX")"
cp -- "$BACKUP" "$STAGE/candidate.bak"

# 0c. copy the whole current family; hot-journal recovery happens on the copy.
mkdir -- "$STAGE/current"
cp -- "$data_dir/galgame.sqlite3" "$STAGE/current/"
for s in "$data_dir/galgame.sqlite3-wal" "$data_dir/galgame.sqlite3-shm" "$data_dir/galgame.sqlite3-journal"; do
  if [ -e "$s" ]; then cp -- "$s" "$STAGE/current/"; fi
done

# 0d. automated preimage validation, before any mv.
python3 "$REPO_ROOT/scripts/arc0_restore_preflight.py" preflight \\
  "$STAGE/candidate.bak" "$STAGE/current/galgame.sqlite3" "$EXPECTED_V"
@POST_PREFLIGHT_HOOK@
# 1. quarantine the whole current v2 family (destructive phase starts here).
Q_ROOT="$data_dir/quarantine"
Q="$Q_ROOT/@Q_TS@"
mkdir -p -- "$Q_ROOT"
mkdir -- "$Q"
mv -- "$data_dir/galgame.sqlite3" "$Q"/
for s in "$data_dir/galgame.sqlite3-wal" "$data_dir/galgame.sqlite3-shm" "$data_dir/galgame.sqlite3-journal"; do
  if [ -e "$s" ]; then mv -- "$s" "$Q"/; fi
done

# 2. restore: consume the SAME staged copy preflight validated.
cp -- "$STAGE/candidate.bak" "$data_dir/galgame.sqlite3"

# 3. post-restore verification inside this unit (never re-read $BACKUP).
cmp -- "$STAGE/candidate.bak" "$data_dir/galgame.sqlite3"
python3 "$REPO_ROOT/scripts/arc0_restore_preflight.py" verify-restored \\
  "$data_dir/galgame.sqlite3" "$EXPECTED_V"

# 4. full success: the only deletion point for staging.
rm -rf -- "$STAGE"
trap - ERR
"""

Q_TS = "20260710-210000"


def plant_hot_journal(db_path: Path) -> None:
    """Leave a genuinely hot rollback journal: 1-page cache forces spilling
    uncommitted pages, which journals pre-images and finalizes the header."""
    conn = sqlite3.connect(db_path)
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA cache_size=1")
    conn.execute("BEGIN IMMEDIATE")
    for i in range(200):
        conn.execute("INSERT INTO game_profiles (game_id, data) VALUES (?, ?)",
                     (f"HOT{i}", json.dumps({"pad": "x" * 512})))
    journal = Path(str(db_path) + "-journal")
    assert journal.exists() and journal.stat().st_size > 0
    hot_db = db_path.read_bytes()
    hot_journal = journal.read_bytes()
    conn.execute("ROLLBACK")
    conn.close()
    db_path.write_bytes(hot_db)
    journal.write_bytes(hot_journal)


class RestoreRunbookDrillTest(MigrationTestBase):
    """§9 #21/#29/#30/#31/#39/#40/#43: the §10 unit end-to-end on temp DBs."""

    def setUp(self) -> None:
        super().setUp()
        self.unrelated_cwd = self.data_dir / "unrelated_cwd"
        self.unrelated_cwd.mkdir()

    def _migrate_with_backup(self) -> Path:
        build_legacy_db(
            self.db_path, user_version=1,
            relations=(make_relation("rel::A::B", "g1", summary="g1 relation"),),
            profiles=(GameProfile(game_id="g1", display_name="G",
                                  created_at="t", updated_at="t"),))
        GameMemorySqliteAdapter(self.db_path)
        return published_backups(self.data_dir)[0]

    def _run_unit(self, backup: Path, *, repo_root: Path = REPO_ROOT,
                  hook: str = "", path_prefix: Path | None = None
                  ) -> subprocess.CompletedProcess:
        script = (RESTORE_UNIT
                  .replace("@DATA_DIR@", str(self.data_dir))
                  .replace("@REPO_ROOT@", str(repo_root))
                  .replace("@BACKUP@", str(backup))
                  .replace("@Q_TS@", Q_TS)
                  .replace("@POST_PREFLIGHT_HOOK@", hook))
        script_path = self.data_dir / "restore_unit.sh"
        script_path.write_text(script, encoding="utf-8")
        path = os.environ.get("PATH", "")
        if path_prefix is not None:
            path = f"{path_prefix}{os.pathsep}{path}"
        env = {"PATH": path, "PYTHONPATH": ""}
        # Plain `bash` invocation: fail-fast must come from the unit itself.
        return subprocess.run(["bash", str(script_path)], capture_output=True,
                              text=True, env=env, cwd=self.unrelated_cwd)

    def _staging_dirs(self) -> list[Path]:
        return sorted(self.data_dir.glob("arc0-restore-stage.*"))

    def _assert_successful_restore(self, candidate: Path,
                                   result: subprocess.CompletedProcess) -> None:
        # #21 per-scenario assertions: byte-identical restore (cmp inside the
        # unit + re-hashed here) implies schema objects and full-table parity
        # with the verified candidate; version + integrity re-checked directly.
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sha256(self.db_path), sha256(candidate))
        conn = open_ro(self.db_path)
        try:
            self.assertEqual(int(conn.execute("PRAGMA user_version").fetchone()[0]), 1)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()
        quarantined = self.data_dir / "quarantine" / Q_TS / "galgame.sqlite3"
        self.assertTrue(quarantined.exists())
        self.assertEqual(self._staging_dirs(), [])  # #40: deleted only on success

    def test_drill_scenario_no_sidecars(self):
        candidate = self._migrate_with_backup()
        self.assertEqual(sidecar_state(self.db_path),
                         {"-wal": False, "-shm": False, "-journal": False})
        self._assert_successful_restore(candidate, self._run_unit(candidate))

    def test_drill_scenario_wal_sidecars(self):
        candidate = self._migrate_with_backup()
        # Materialize real -wal/-shm bytes WITHOUT any committed business write
        # (a committed write would rightly be rejected as post-migration data),
        # then re-plant them: closing the last connection checkpoints and
        # deletes the sidecars, so save and restore their bytes.
        wal = Path(str(self.db_path) + "-wal")
        shm = Path(str(self.db_path) + "-shm")
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("SELECT count(*) FROM game_profiles").fetchone()
        self.assertTrue(wal.exists())
        wal_bytes, shm_bytes = wal.read_bytes(), shm.read_bytes()
        conn.close()
        wal.write_bytes(wal_bytes)
        shm.write_bytes(shm_bytes)

        result = self._run_unit(candidate)
        self._assert_successful_restore(candidate, result)
        # The sidecars travelled into quarantine with the family.
        q_dir = self.data_dir / "quarantine" / Q_TS
        self.assertTrue((q_dir / "galgame.sqlite3-wal").exists())

    def test_drill_scenario_hot_journal(self):
        candidate = self._migrate_with_backup()
        plant_hot_journal(self.db_path)
        result = self._run_unit(candidate)
        self._assert_successful_restore(candidate, result)
        q_dir = self.data_dir / "quarantine" / Q_TS
        self.assertTrue((q_dir / "galgame.sqlite3-journal").exists())

    def test_sentinel_loss_proves_the_operator_gate(self):
        # §9 #21 sentinel + §9 #31 post-migration-write rejection + §9 #40
        # pre-destructive staging retention.
        candidate = self._migrate_with_backup()
        adapter = GameMemorySqliteAdapter(self.db_path)
        adapter.upsert_game_profile(GameProfile(
            game_id="SENTINEL", display_name="post-migration data",
            created_at="t", updated_at="t"))

        family_hash = sha256(self.db_path)
        result = self._run_unit(candidate)
        # Preflight rejects: unrelated-table inequality == post-migration write.
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("roll forward", result.stdout + result.stderr)
        self.assertEqual(sha256(self.db_path), family_hash)  # family untouched
        self.assertFalse((self.data_dir / "quarantine").exists())
        # Pre-destructive failure retains staging, trap names the real path.
        stages = self._staging_dirs()
        self.assertEqual(len(stages), 1)
        self.assertIn(str(stages[0]), result.stderr)
        self.assertEqual(stages[0].stat().st_mode & 0o777, 0o700)  # mktemp 0700

        # Operator overriding the gate = raw copy: the sentinel is LOST.
        self.db_path.write_bytes(candidate.read_bytes())
        for suffix in ("-wal", "-shm", "-journal"):
            side = Path(str(self.db_path) + suffix)
            if side.exists():
                side.unlink()
        conn = open_ro(self.db_path)
        try:
            hit = conn.execute("SELECT count(*) FROM game_profiles "
                               "WHERE game_id='SENTINEL'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(hit, 0)

    def test_older_valid_backup_rejected_before_any_mv(self):
        # §9 #31 head -- two equally *valid* v1 backups; only the true preimage
        # passes. The old one must be rejected before anything moves.
        build_legacy_db(
            self.db_path, user_version=1,
            relations=(make_relation("rel::A::B", "g1", summary="g1 relation"),))
        backups = self.data_dir / "backups"
        backups.mkdir()
        old_backup = backups / "galgame.sqlite3.v1.20250101-000000.pre-arc0.bak"
        old_backup.write_bytes(self.db_path.read_bytes())  # valid, but stale

        conn = sqlite3.connect(self.db_path)
        rel = make_relation("rel::E::F", "g2", summary="g2 relation")
        conn.execute(
            "INSERT INTO character_relations "
            "(relation_id, game_id, playthrough_id, updated_at, data) VALUES (?, ?, ?, ?, ?)",
            (rel.relation_id, rel.game_id, rel.playthrough_id, rel.updated_at,
             json.dumps(rel.to_dict(), ensure_ascii=False)))
        conn.commit()
        conn.close()
        GameMemorySqliteAdapter(self.db_path)  # true preimage published

        family_hash = sha256(self.db_path)
        result = self._run_unit(old_backup)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sha256(self.db_path), family_hash)
        self.assertFalse((self.data_dir / "quarantine").exists())

    def test_staging_consumption_survives_backup_replacement(self):
        # §9 #39 (+#31 same-source) -- replacing $BACKUP after preflight must
        # not affect the restore: the staged bytes are what gets consumed.
        candidate = self._migrate_with_backup()
        true_hash = sha256(candidate)
        hook = 'printf "not a database" > "$BACKUP"\n'
        result = self._run_unit(candidate, hook=hook)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sha256(self.db_path), true_hash)
        self.assertNotEqual(sha256(candidate), true_hash)  # original was junked

    def test_quarantine_leaf_never_reused(self):
        # §9 #29 -- a pre-existing leaf fail-fasts before any mv, even under a
        # plain (non fail-fast) bash caller: the unit carries set -euo pipefail.
        candidate = self._migrate_with_backup()
        leaf = self.data_dir / "quarantine" / Q_TS
        leaf.mkdir(parents=True)
        (leaf / "old-quarantined.sqlite3").write_bytes(b"previous quarantine")

        family_hash = sha256(self.db_path)
        result = self._run_unit(candidate)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.db_path.exists())
        self.assertEqual(sha256(self.db_path), family_hash)  # never moved
        self.assertEqual((leaf / "old-quarantined.sqlite3").read_bytes(),
                         b"previous quarantine")

    def test_naming_fail_closed_rejects_before_mktemp(self):
        # §9 #43 -- tmp members, junk names, v2+, malformed timestamps, partial
        # matches: all rejected before staging exists (sed-passthrough pin).
        candidate = self._migrate_with_backup()
        backups = self.data_dir / "backups"
        bad_names = (
            "galgame.sqlite3.v1.20260101-000000.pre-arc0.tmp",
            "candidate.bak",
            "galgame.sqlite3.v2.20260101-000000.pre-arc0.bak",
            "galgame.sqlite3.v1.2026-01-01.pre-arc0.bak",
            "xgalgame.sqlite3.v1.20260101-000000.pre-arc0.bak",
            "galgame.sqlite3.v1.20260101-000000.pre-arc0.bak.extra",
        )
        for name in bad_names:
            with self.subTest(name=name):
                bad = backups / name
                bad.write_bytes(candidate.read_bytes())  # only the NAME gates
                family_hash = sha256(self.db_path)
                result = self._run_unit(bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("REJECT", result.stderr)
                self.assertEqual(self._staging_dirs(), [])  # before mktemp
                self.assertFalse((self.data_dir / "quarantine").exists())
                self.assertEqual(sha256(self.db_path), family_hash)
                bad.unlink()

    def test_missing_helper_aborts_before_destruction(self):
        # §9 #40 -- helper unavailable: staging retained, family untouched.
        candidate = self._migrate_with_backup()
        empty_repo = self.data_dir / "empty_repo"
        empty_repo.mkdir()
        family_hash = sha256(self.db_path)
        result = self._run_unit(candidate, repo_root=empty_repo)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sha256(self.db_path), family_hash)
        self.assertFalse((self.data_dir / "quarantine").exists())
        stages = self._staging_dirs()
        self.assertEqual(len(stages), 1)
        self.assertIn(str(stages[0]), result.stderr)

    def test_destructive_phase_failure_retains_stage_and_quarantine(self):
        # §9 #40 -- failure after the mv: staging AND quarantine retained, the
        # ERR trap prints the accurate paths.
        candidate = self._migrate_with_backup()
        shim_repo = self.data_dir / "shim_repo"
        (shim_repo / "scripts").mkdir(parents=True)
        shim = shim_repo / "scripts" / "arc0_restore_preflight.py"
        shim.write_text(
            "#!/usr/bin/env python3\n"
            "import subprocess, sys\n"
            f"REAL = {str(PREFLIGHT_HELPER)!r}\n"
            "if sys.argv[1] == 'preflight':\n"
            "    sys.exit(subprocess.call([sys.executable, REAL] + sys.argv[1:]))\n"
            "sys.exit(1)  # verify-restored: injected destructive-phase failure\n",
            encoding="utf-8")

        result = self._run_unit(candidate, repo_root=shim_repo)
        self.assertNotEqual(result.returncode, 0)
        stages = self._staging_dirs()
        q_dir = self.data_dir / "quarantine" / Q_TS
        self.assertEqual(len(stages), 1)
        self.assertTrue(q_dir.exists())
        self.assertTrue((q_dir / "galgame.sqlite3").exists())  # family quarantined
        self.assertIn(str(stages[0]), result.stderr)  # trap paths are accurate
        self.assertIn(str(q_dir), result.stderr)

    def test_staging_delete_partial_failure_preserves_recovery_and_quarantine(self):
        # §9 #40: rm -rf may delete part of staging before failing. The contract
        # preserves the restored DB and quarantine, leaves the remaining stage
        # as-is, and reports the exact retained paths.
        candidate = self._migrate_with_backup()
        current_hash = sha256(self.db_path)
        candidate_hash = sha256(candidate)
        shim_bin = self.data_dir / "rm_shim_bin"
        shim_bin.mkdir()
        real_rm = shutil.which("rm")
        self.assertIsNotNone(real_rm)
        rm_shim = shim_bin / "rm"
        rm_shim.write_text(
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            "stage=\"${@: -1}\"\n"
            f"{real_rm!s} -rf -- \"$stage/current\"\n"
            "exit 73\n",
            encoding="utf-8")
        rm_shim.chmod(0o755)

        result = self._run_unit(candidate, path_prefix=shim_bin)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sha256(self.db_path), candidate_hash)
        q_dir = self.data_dir / "quarantine" / Q_TS
        self.assertEqual(sha256(q_dir / "galgame.sqlite3"), current_hash)
        stages = self._staging_dirs()
        self.assertEqual(len(stages), 1)
        self.assertTrue((stages[0] / "candidate.bak").exists())
        self.assertFalse((stages[0] / "current").exists())
        self.assertIn(str(stages[0]), result.stderr)
        self.assertIn(str(q_dir), result.stderr)

    def test_sqlite_prefixed_user_table_participates_in_parity(self):
        # §9 #30 -- GLOB enumeration: a legit 'sqliteX...' user table is swept.
        build_legacy_db(
            self.db_path, user_version=1,
            relations=(make_relation("rel::A::B", "g1"),))
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE sqliteXtra (id TEXT PRIMARY KEY, data TEXT)")
        conn.execute("INSERT INTO sqliteXtra VALUES ('k', 'v')")
        conn.commit()
        conn.close()
        GameMemorySqliteAdapter(self.db_path)
        candidate = published_backups(self.data_dir)[0]

        with self.subTest(case="untampered passes with the table included"):
            result = self._run_unit(candidate)
            self.assertEqual(result.returncode, 0, result.stderr)

        with self.subTest(case="tampering sqliteXtra is caught (LIKE would miss)"):
            # Restore drill already ran; rebuild the tampered current from
            # quarantine to keep this subtest self-contained.
            quarantined = self.data_dir / "quarantine" / Q_TS / "galgame.sqlite3"
            self.db_path.write_bytes(quarantined.read_bytes())
            conn = sqlite3.connect(self.db_path)
            conn.execute("UPDATE sqliteXtra SET data='tampered'")
            conn.commit()
            conn.close()
            shutil.rmtree(self.data_dir / "quarantine")
            result = self._run_unit(candidate)
            self.assertNotEqual(result.returncode, 0)


class IntrospectionParityTest(MigrationTestBase):
    """§9 #12 + 附录 A#2: fresh-created v2 and migrated v2 are shape-identical
    under all three introspection lenses (table_xinfo / index_list / index_xinfo).
    """

    @staticmethod
    def _introspect(db_path: Path) -> dict:
        conn = open_ro(db_path)
        try:
            table_xinfo = tuple(tuple(row) for row in conn.execute(
                "PRAGMA table_xinfo(character_relations)").fetchall())
            index_list = tuple(sorted(
                # (name, unique, origin, partial) -- drop the volatile seq column.
                (row[1], row[2], row[3], row[4]) for row in conn.execute(
                    "PRAGMA index_list(character_relations)").fetchall()))
            index_xinfo = {
                name: tuple(tuple(row) for row in conn.execute(
                    f"PRAGMA index_xinfo({name})").fetchall())
                for (name, _u, _o, _p) in index_list
            }
        finally:
            conn.close()
        return {"table_xinfo": table_xinfo, "index_list": index_list,
                "index_xinfo": index_xinfo}

    def test_v1_and_v2_endpoints_match_independent_spec_literals(self):
        # §9 #12/#42: complete expectations come from canonical v1.9 literals,
        # never from production constants or from comparing two produced DBs.
        legacy_path = self.data_dir / "literal_v1.sqlite3"
        current_path = self.data_dir / "literal_v2.sqlite3"
        build_legacy_db(legacy_path, user_version=1)
        GameMemorySqliteAdapter(current_path)

        self.assertEqual(
            self._introspect(legacy_path), EXPECTED_V1_RELATIONS_ENDPOINT)
        self.assertEqual(
            self._introspect(current_path), EXPECTED_V2_RELATIONS_ENDPOINT)

    def test_fresh_and_migrated_v2_are_introspection_identical(self):
        fresh_path = self.data_dir / "fresh.sqlite3"
        migrated_path = self.data_dir / "migrated.sqlite3"
        GameMemorySqliteAdapter(fresh_path)
        build_legacy_db(migrated_path, user_version=1,
                        relations=(make_relation("rel::A::B", "g1"),))
        GameMemorySqliteAdapter(migrated_path)

        fresh = self._introspect(fresh_path)
        migrated = self._introspect(migrated_path)
        self.assertEqual(fresh, migrated)

        # Pin the v2 essentials explicitly (independent source of truth, not a
        # snapshot of whatever the code created): composite PK order + both
        # indexes present, ascending, default collation.
        names = [n for (n, _u, _o, _p) in fresh["index_list"]]
        self.assertIn("idx_relations_lookup", names)
        self.assertIn("sqlite_autoindex_character_relations_1", names)
        autoindex = fresh["index_xinfo"]["sqlite_autoindex_character_relations_1"]
        # key columns (key=1) in PK order: game_id, playthrough_id, relation_id
        key_cols = [row[2] for row in autoindex if row[5] == 1]
        self.assertEqual(key_cols, ["game_id", "playthrough_id", "relation_id"])
        for row in autoindex:
            if row[5] == 1:
                self.assertEqual(row[3], 0)      # ASC, not DESC
                self.assertEqual(row[4], "BINARY")  # default collation
        lookup = fresh["index_xinfo"]["idx_relations_lookup"]
        self.assertEqual([row[2] for row in lookup if row[5] == 1],
                         ["game_id", "playthrough_id"])


if __name__ == "__main__":
    unittest.main()
