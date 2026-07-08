"""Review #6: both sqlite stores open WAL connections with an explicit busy
timeout, pinned as a testable contract.

- WAL: UI turns / backgrounded long-term commits / galgame OCR-line writes and
  summaries hit these DBs concurrently; in the default DELETE journal mode a
  writer blocks readers. WAL is a PERSISTENT setting (lives in the DB header),
  so a raw connection with no pragmas must see it too (pinned below).
- busy_timeout=5000: Python's sqlite3.connect already defaults to a 5s busy
  timeout implicitly; the explicit PRAGMA turns it into a visible contract.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from memory.store import SQLiteMemoryStore
from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter


class SqlitePragmaTest(unittest.TestCase):
    def _assert_pragmas(self, conn: sqlite3.Connection) -> None:
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_memory_store_connections_use_wal_and_busy_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMemoryStore(Path(tmp) / "memory.sqlite3")
            conn = store._connect()
            try:
                self._assert_pragmas(conn)
            finally:
                conn.close()

    def test_game_memory_connections_use_wal_and_busy_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = GameMemorySqliteAdapter(Path(tmp) / "galgame.sqlite3")
            conn = adapter._connect()
            try:
                self._assert_pragmas(conn)
            finally:
                conn.close()

    def test_wal_is_persistent_on_the_db_file(self):
        # The real-machine conversion path: _init_db's first connection flips the
        # header; afterwards even a RAW pragma-less connection runs in WAL.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.sqlite3"
            SQLiteMemoryStore(path)
            raw = sqlite3.connect(path)
            try:
                self.assertEqual(raw.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            finally:
                raw.close()


if __name__ == "__main__":
    unittest.main()
