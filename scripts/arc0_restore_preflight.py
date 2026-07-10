#!/usr/bin/env python3
"""AR-C0 restore preflight/verify helper (runbook §10 contract).

Self-contained and stdlib-only -- this file must NEVER ``import spica`` (it
runs from any cwd with a clean ``PYTHONPATH`` and must not drag in production
config/env wiring). It is the machine check behind the restore-first operator
gate: a candidate backup is only consumable if it is the AR-C0 preimage of the
CURRENT v2 database.

This is the narrow AR-C0 exception to the application-entry ``load_secrets``
rule: the offline verifier constructs no app, config, provider, or service
object, consumes no environment-backed setting, and must remain runnable when
the production package and dotenv dependency are unavailable. Application and
service entry points remain subject to the rule without exception.

Subcommands (exit code 0 = pass, 1 = contract failure, 2 = usage):

  preflight CANDIDATE CURRENT_COPY EXPECTED_V
      CANDIDATE     staged copy of the ``*.pre-arc0.bak`` backup (read-only)
      CURRENT_COPY  staged copy of the current v2 DB family; opened read-write
                    so SQLite may recover a hot journal ON THE COPY -- the real
                    family must never be handed to this tool
      EXPECTED_V    source version from the canonical backup name: 0 or 1

  verify-restored RESTORED EXPECTED_V
      RESTORED      the restored main DB (read-only); byte equality with the
                    staged candidate is the caller's ``cmp`` step

Connection discipline: CANDIDATE / RESTORED open ``mode=ro`` with no PRAGMAs
whatsoever and never ``immutable=1``; only CURRENT_COPY opens read-write.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections import Counter
from urllib.parse import quote

# Helper-local canonical endpoint literals. They intentionally do not import or
# derive from production constants: restore verification must independently
# validate table_xinfo, index_list, and index_xinfo on each endpoint.
V1_RELATIONS_TABLE_XINFO = (
    (0, "relation_id", "TEXT", 0, None, 1, 0),
    (1, "game_id", "TEXT", 1, None, 0, 0),
    (2, "playthrough_id", "TEXT", 1, None, 0, 0),
    (3, "updated_at", "TEXT", 0, None, 0, 0),
    (4, "data", "TEXT", 1, None, 0, 0),
)
V2_RELATIONS_TABLE_XINFO = (
    (0, "relation_id", "TEXT", 1, None, 3, 0),
    (1, "game_id", "TEXT", 1, None, 1, 0),
    (2, "playthrough_id", "TEXT", 1, None, 2, 0),
    (3, "updated_at", "TEXT", 0, None, 0, 0),
    (4, "data", "TEXT", 1, None, 0, 0),
)
RELATIONS_INDEX_LIST = (
    ("idx_relations_lookup", 0, "c", 0),
    ("sqlite_autoindex_character_relations_1", 1, "pk", 0),
)
V1_RELATIONS_INDEX_XINFO = {
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
V2_RELATIONS_INDEX_XINFO = {
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

# Helper-local full table DDL contracts. They duplicate neither imports nor
# generated production data: restore validation remains an independent endpoint
# check even when candidate and current share the same noncanonical constraint.
V1_TABLE_DDL = {
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
V2_RELATIONS_DDL = """CREATE TABLE character_relations (
    relation_id TEXT NOT NULL,
    game_id TEXT NOT NULL,
    playthrough_id TEXT NOT NULL,
    updated_at TEXT,
    data TEXT NOT NULL,
    PRIMARY KEY (game_id, playthrough_id, relation_id)
)"""

SQLITE_DDL_WHITESPACE = " \t\n\f\r"
CANONICAL_DDL_KEYWORDS = frozenset({
    "CREATE", "TABLE", "IF", "NOT", "EXISTS", "PRIMARY", "KEY", "NULL",
})

RELATIONS_KEY = ("table", "character_relations", "character_relations")
RELATIONS_LOOKUP_KEY = (
    "index", "idx_relations_lookup", "character_relations")
RELATIONS_SQL_DIFFERENCE_KEYS = frozenset({
    RELATIONS_KEY,
    RELATIONS_LOOKUP_KEY,
})
PROJECTION_SQL = ("SELECT relation_id, game_id, playthrough_id, updated_at, data "
                  "FROM character_relations")


class Reject(Exception):
    """Any violated preflight/verify contract."""


class _ConnectionCleanup:
    """Close every registered connection without masking a primary error."""

    def __init__(self) -> None:
        self._connections = []

    def __enter__(self):
        return self

    def add(self, conn):
        self._connections.append(conn)
        return conn

    def __exit__(self, exc_type, exc, traceback) -> bool:
        close_failure = None
        for conn in reversed(self._connections):
            try:
                conn.close()
            except BaseException as close_exc:
                if close_failure is None:
                    close_failure = close_exc
        if exc_type is None and close_failure is not None:
            raise close_failure
        return False


def sqlite_ascii_upper(value: str) -> str:
    """SQLite identifier comparison folds ASCII case, not Unicode case."""
    return "".join(
        chr(ord(char) - 32) if "a" <= char <= "z" else char
        for char in value)


def quote_sqlite_identifier(identifier: str) -> str:
    """Quote a sqlite_master name for use in SQL identifier position."""
    return '"' + identifier.replace('"', '""') + '"'


def sql_ddl_fingerprint(sql: str) -> tuple:
    """Token-aware DDL fingerprint preserving every literal/constraint token."""
    tokens = []
    length = len(sql)
    index = 0

    while index < length:
        char = sql[index]
        if char in SQLITE_DDL_WHITESPACE:
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
            tokens.append(("identifier", sqlite_ascii_upper("".join(value))))
            continue
        if char.isalnum() or char in "_$":
            start = index
            index += 1
            while index < length and (
                    sql[index].isalnum() or sql[index] in "_$"):
                index += 1
            value = sqlite_ascii_upper(sql[start:index])
            kind = (
                "keyword" if value in CANONICAL_DDL_KEYWORDS
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


def reject(msg: str) -> None:
    raise Reject(msg)


def open_ro(path: str) -> sqlite3.Connection:
    # No write-capable PRAGMAs, no immutable=1 (it would skip un-checkpointed
    # WAL content). A journal_mode-flipping helper here would rewrite the
    # restored file after the caller's final cmp -- that is the exact failure
    # form this discipline pins down.
    uri = f"file:{quote(os.path.abspath(path), safe='/')}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def open_rw(path: str) -> sqlite3.Connection:
    # ONLY for the staged current copy: hot-journal recovery needs write access
    # and must happen on the copy, never on the real family.
    return sqlite3.connect(path)


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def check_relations_endpoint(conn: sqlite3.Connection, label: str,
                             expected_table_xinfo: tuple,
                             expected_index_xinfo: dict) -> None:
    table_xinfo = tuple(tuple(row) for row in conn.execute(
        "PRAGMA table_xinfo(character_relations)").fetchall())
    if table_xinfo != expected_table_xinfo:
        reject(f"{label}: character_relations table_xinfo is not canonical")

    index_list = tuple(sorted(
        (row[1], int(row[2]), row[3], int(row[4]))
        for row in conn.execute(
            "PRAGMA index_list(character_relations)").fetchall()))
    if index_list != RELATIONS_INDEX_LIST:
        reject(f"{label}: character_relations index_list is not canonical")

    for name, expected in expected_index_xinfo.items():
        actual = tuple(tuple(row) for row in conn.execute(
            f'PRAGMA index_xinfo("{name}")').fetchall())
        if actual != expected:
            reject(f"{label}: {name} index_xinfo is not canonical")


def check_table_ddls(conn: sqlite3.Connection, label: str,
                     *, relations_v2: bool) -> None:
    """Validate all eight canonical tables independently on this endpoint."""
    expected_ddls = dict(V1_TABLE_DDL)
    if relations_v2:
        expected_ddls["character_relations"] = V2_RELATIONS_DDL
    for table, expected_sql in expected_ddls.items():
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        if row is None or row[0] is None:
            reject(f"{label}: required table {table} is missing")
        try:
            matches = (
                sql_ddl_fingerprint(row[0]) ==
                sql_ddl_fingerprint(expected_sql))
        except (TypeError, ValueError):
            matches = False
        if not matches:
            reject(f"{label}: table {table} DDL/constraints are not canonical")


def business_tables(conn: sqlite3.Connection) -> set:
    # GLOB, not LIKE: LIKE's '_' is a single-char wildcard and would also drop
    # legitimate 'sqliteX...' user tables from the parity sweep.
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT GLOB 'sqlite_*'").fetchall()
    return {r[0] for r in rows}


def schema_objects(conn: sqlite3.Connection) -> dict:
    return {(r[0], r[1], r[2]): r[3] for r in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master")}


def check_integrity(conn: sqlite3.Connection, label: str) -> None:
    result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        reject(f"{label}: integrity_check returned {result!r}")


def parse_expected_v(raw: str) -> int:
    if raw not in ("0", "1"):
        reject(f"EXPECTED_V must be 0 or 1, got {raw!r} (v2+ backups are never "
               "restore candidates for AR-C0)")
    return int(raw)


def preflight(candidate_path: str, current_path: str, raw_expected: str) -> None:
    expected_v = parse_expected_v(raw_expected)

    with _ConnectionCleanup() as connections:
        candidate = connections.add(open_ro(candidate_path))
        current = connections.add(open_rw(current_path))

        # -- endpoint contract (v1.9): both ends are pinned, not just the delta.
        got = user_version(candidate)
        if got != expected_v:
            reject(f"candidate user_version={got} != EXPECTED_V={expected_v}")
        check_table_ddls(candidate, "candidate", relations_v2=False)
        check_relations_endpoint(
            candidate, "candidate", V1_RELATIONS_TABLE_XINFO,
            V1_RELATIONS_INDEX_XINFO)
        journal = candidate.execute("PRAGMA journal_mode").fetchone()[0]
        if str(journal).lower() != "delete":
            reject(f"candidate persistent journal mode is {journal!r}, expected "
                   "'delete' (publication protocol normalizes backups)")
        check_integrity(candidate, "candidate")

        # First access recovers a hot journal on the staged copy only.
        cur_version = user_version(current)
        if cur_version != 2:
            reject(f"current user_version={cur_version} != 2 (restoring over a "
                   "non-v2 DB is either a misfire or a destructive downgrade)")
        check_table_ddls(current, "current", relations_v2=True)
        check_relations_endpoint(
            current, "current", V2_RELATIONS_TABLE_XINFO,
            V2_RELATIONS_INDEX_XINFO)
        check_integrity(current, "current")

        # -- schema objects: identical except the expected relations delta,
        #    and that delta MUST actually exist.
        cand_schema = schema_objects(candidate)
        cur_schema = schema_objects(current)
        if set(cand_schema) != set(cur_schema):
            extra_c = sorted(set(cand_schema) - set(cur_schema))
            extra_k = sorted(set(cur_schema) - set(cand_schema))
            reject(f"schema object sets differ (candidate-only={extra_c}, "
                   f"current-only={extra_k})")
        for key in cand_schema:
            # Both relation endpoints already passed exact table_xinfo,
            # index_list and index_xinfo gates above. Raw CREATE spelling may
            # therefore differ for the rebuilt table and its canonical lookup
            # index without representing an additional schema delta.
            if (cand_schema[key] != cur_schema[key]
                    and key not in RELATIONS_SQL_DIFFERENCE_KEYS):
                reject(f"unexpected schema difference in {key}")
        if cand_schema[RELATIONS_KEY] == cur_schema[RELATIONS_KEY]:
            reject("expected v1->v2 character_relations schema delta is missing")

        # -- dynamic table enumeration + full-column full-row parity.
        cand_tables = business_tables(candidate)
        cur_tables = business_tables(current)
        if cand_tables != cur_tables:
            reject(f"business table sets differ: {sorted(cand_tables ^ cur_tables)}")
        for table in sorted(cand_tables):
            if table == "character_relations":
                continue
            quoted_table = quote_sqlite_identifier(table)
            select = f"SELECT * FROM {quoted_table}"
            cand_rows = Counter(tuple(r) for r in candidate.execute(select))
            cur_rows = Counter(tuple(r) for r in current.execute(select))
            if cand_rows != cur_rows:
                reject(f"table {table} differs between candidate and current -- "
                       "post-migration writes or a wrong candidate; roll forward "
                       "instead of restoring")

        # -- surviving-row projection: migration copies rows verbatim.
        cand_proj = Counter(tuple(r) for r in candidate.execute(PROJECTION_SQL))
        cur_proj = Counter(tuple(r) for r in current.execute(PROJECTION_SQL))
        if cand_proj != cur_proj:
            reject("character_relations surviving-row projection differs -- the "
                   "candidate is not this v2 database's preimage")

    print("PREFLIGHT PASS: candidate is the AR-C0 preimage of the current v2 DB")


def verify_restored(restored_path: str, raw_expected: str) -> None:
    expected_v = parse_expected_v(raw_expected)
    with _ConnectionCleanup() as connections:
        conn = connections.add(open_ro(restored_path))
        got = user_version(conn)
        if got != expected_v:
            reject(f"restored user_version={got} != EXPECTED_V={expected_v}")
        check_integrity(conn, "restored")
    print("VERIFY-RESTORED PASS")


def main(argv: list) -> int:
    if len(argv) == 4 and argv[0] == "preflight":
        checks = lambda: preflight(argv[1], argv[2], argv[3])  # noqa: E731
    elif len(argv) == 3 and argv[0] == "verify-restored":
        checks = lambda: verify_restored(argv[1], argv[2])  # noqa: E731
    else:
        print("usage: arc0_restore_preflight.py preflight CANDIDATE CURRENT_COPY "
              "EXPECTED_V | verify-restored RESTORED EXPECTED_V", file=sys.stderr)
        return 2
    try:
        checks()
    except Reject as exc:
        print(f"PREFLIGHT REJECT: {exc}", file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError) as exc:
        print(f"PREFLIGHT REJECT: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
