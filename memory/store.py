from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_MEMORY_TYPE = "fact"
DEFAULT_MEMORY_SOURCE = "user"
DEFAULT_MEMORY_STATUS = "active"
_CJK_STOP_CHARS = set("我你他她它的是了啊吗呢吧么什么一个这个那个请把和与或在有也都就说")


class SQLiteMemoryStore:
    def __init__(self, db_path: str | Path = "spica_data/memory.sqlite3"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # Review #6: UI turns / backgrounded commits / galgame summaries write
        # concurrently -- WAL keeps readers unblocked by writers; busy_timeout
        # pins Python's implicit 5s default as an explicit, testable contract.
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    content TEXT NOT NULL,
                    importance REAL NOT NULL DEFAULT 0.5,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT,
                    use_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._ensure_column(conn, "memory_key", "TEXT")
            self._ensure_column(conn, "memory_type", f"TEXT NOT NULL DEFAULT '{DEFAULT_MEMORY_TYPE}'")
            self._ensure_column(conn, "source", f"TEXT NOT NULL DEFAULT '{DEFAULT_MEMORY_SOURCE}'")
            self._ensure_column(conn, "confidence", "REAL NOT NULL DEFAULT 1.0")
            self._ensure_column(conn, "pinned", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "status", f"TEXT NOT NULL DEFAULT '{DEFAULT_MEMORY_STATUS}'")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_conversation ON memories(conversation_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_lookup "
                "ON memories(conversation_id, scope, memory_key, status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_rank "
                "ON memories(conversation_id, status, pinned, importance, updated_at)"
            )

    def _ensure_column(self, conn: sqlite3.Connection, column_name: str, column_sql: str) -> None:
        rows = conn.execute("PRAGMA table_info(memories)").fetchall()
        existing = {str(row["name"]) for row in rows}
        if column_name not in existing:
            conn.execute(f"ALTER TABLE memories ADD COLUMN {column_name} {column_sql}")

    def add_memory(
        self,
        conversation_id: str,
        scope: str,
        content: str,
        importance: float = 0.5,
        memory_key: str | None = None,
        memory_type: str = DEFAULT_MEMORY_TYPE,
        source: str = DEFAULT_MEMORY_SOURCE,
        confidence: float = 1.0,
        pinned: bool = False,
    ) -> int:
        content = self._clean_content(content)
        if not content:
            raise ValueError("memory content cannot be empty")
        now = datetime.utcnow().isoformat(timespec="seconds")
        key = memory_key or self._memory_key(scope, content)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO memories (
                    conversation_id, scope, content, importance, memory_key,
                    memory_type, source, confidence, pinned, status,
                    created_at, updated_at, last_used_at, use_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)
                """,
                (
                    conversation_id,
                    scope,
                    content,
                    self._clamp_importance(importance),
                    key,
                    memory_type or DEFAULT_MEMORY_TYPE,
                    source or DEFAULT_MEMORY_SOURCE,
                    self._clamp_confidence(confidence),
                    1 if pinned else 0,
                    DEFAULT_MEMORY_STATUS,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def upsert_memory(
        self,
        conversation_id: str,
        scope: str,
        content: str,
        importance: float = 0.5,
        memory_key: str | None = None,
        memory_type: str = DEFAULT_MEMORY_TYPE,
        source: str = DEFAULT_MEMORY_SOURCE,
        confidence: float = 1.0,
        pinned: bool = False,
    ) -> int:
        content = self._clean_content(content)
        if not content:
            raise ValueError("memory content cannot be empty")
        key = memory_key or self._memory_key(scope, content)
        now = datetime.utcnow().isoformat(timespec="seconds")
        importance = self._clamp_importance(importance)
        confidence = self._clamp_confidence(confidence)

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM memories
                WHERE conversation_id = ?
                  AND scope = ?
                  AND memory_key = ?
                  AND status = ?
                ORDER BY pinned DESC, importance DESC, updated_at DESC
                LIMIT 1
                """,
                (conversation_id, scope, key, DEFAULT_MEMORY_STATUS),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE memories
                    SET content = ?,
                        importance = MAX(importance, ?),
                        memory_type = ?,
                        source = ?,
                        confidence = MAX(confidence, ?),
                        pinned = MAX(pinned, ?),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        content,
                        importance,
                        memory_type or DEFAULT_MEMORY_TYPE,
                        source or DEFAULT_MEMORY_SOURCE,
                        confidence,
                        1 if pinned else 0,
                        now,
                        int(row["id"]),
                    ),
                )
                return int(row["id"])

            return self.add_memory(
                conversation_id=conversation_id,
                scope=scope,
                content=content,
                importance=importance,
                memory_key=key,
                memory_type=memory_type,
                source=source,
                confidence=confidence,
                pinned=pinned,
            )

    def search_memories(self, conversation_id: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
        limit = min(20, max(1, int(limit)))
        keywords = self._keywords(query)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memories
                WHERE conversation_id = ?
                  AND status = ?
                ORDER BY pinned DESC, importance DESC, updated_at DESC
                LIMIT 200
                """,
                (conversation_id, DEFAULT_MEMORY_STATUS),
            ).fetchall()

            scored = []
            for row in rows:
                content = str(row["content"])
                score = float(row["importance"])
                if int(row["pinned"] or 0):
                    score += 2.0
                if keywords:
                    haystack = self._normalize_for_search(content)
                    hits = sum(1 for keyword in keywords if keyword and keyword in haystack)
                    if hits == 0:
                        if not int(row["pinned"] or 0):
                            continue
                    else:
                        score += hits * 0.35
                score += min(int(row["use_count"] or 0), 5) * 0.03
                scored.append((score, row))

            scored.sort(key=lambda item: (item[0], item[1]["updated_at"]), reverse=True)
            selected = [self._row_to_dict(row) for _, row in scored[:limit]]

            if selected:
                now = datetime.utcnow().isoformat(timespec="seconds")
                conn.executemany(
                    "UPDATE memories SET last_used_at = ?, use_count = use_count + 1 WHERE id = ?",
                    [(now, item["id"]) for item in selected],
                )
        return selected

    def list_memories(
        self,
        conversation_id: str,
        limit: int = 50,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            status_filter = "" if include_inactive else "AND status = ?"
            params: tuple[Any, ...]
            if include_inactive:
                params = (conversation_id, max(1, int(limit)))
            else:
                params = (conversation_id, DEFAULT_MEMORY_STATUS, max(1, int(limit)))
            rows = conn.execute(
                f"""
                SELECT * FROM memories
                WHERE conversation_id = ?
                {status_filter}
                ORDER BY pinned DESC, importance DESC, updated_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def delete_memory(self, memory_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))

    def clear_memories(self, conversation_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM memories WHERE conversation_id = ?", (conversation_id,))

    def prune_memories(self, conversation_id: str, max_active: int = 200) -> int:
        max_active = max(1, int(max_active))
        with self._connect() as conn:
            active_count = conn.execute(
                """
                SELECT COUNT(*) AS count FROM memories
                WHERE conversation_id = ?
                  AND status = ?
                """,
                (conversation_id, DEFAULT_MEMORY_STATUS),
            ).fetchone()["count"]
            excess = int(active_count) - max_active
            if excess <= 0:
                return 0
            rows = conn.execute(
                """
                SELECT id FROM memories
                WHERE conversation_id = ?
                  AND status = ?
                  AND pinned = 0
                ORDER BY importance ASC, updated_at ASC
                LIMIT ?
                """,
                (conversation_id, DEFAULT_MEMORY_STATUS, excess),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if not ids:
                return 0
            conn.executemany("DELETE FROM memories WHERE id = ?", [(memory_id,) for memory_id in ids])
            return len(ids)

    def _keywords(self, query: str) -> list[str]:
        text = self._normalize_for_search(query)
        if not text:
            return []
        parts = re.findall(r"[a-z0-9_]+|[\u3040-\u30ff]{2,}", text)
        cjk_chars = [ch for ch in re.findall(r"[\u4e00-\u9fff]", text) if ch not in _CJK_STOP_CHARS]
        cjk_bigrams = [
            "".join(cjk_chars[index:index + 2])
            for index in range(0, max(0, len(cjk_chars) - 1))
        ]
        keywords = parts + cjk_bigrams + cjk_chars
        return [keyword for keyword in dict.fromkeys(keywords) if keyword]

    def _memory_key(self, scope: str, content: str) -> str:
        normalized = self._normalize_for_search(content)
        normalized = re.sub(r"\s+", "", normalized)
        if len(normalized) > 120:
            normalized = normalized[:120]
        return f"{scope}:{normalized}"

    def _normalize_for_search(self, text: str) -> str:
        text = (text or "").strip().lower()
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"[，。！？、；：,.!?;:\"'`~@#$%^&*()\[\]{}<>《》「」『』【】（）]", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _clean_content(self, content: str) -> str:
        return re.sub(r"\s+", " ", content or "").strip()

    def _clamp_importance(self, value: float) -> float:
        return min(1.0, max(0.0, float(value)))

    def _clamp_confidence(self, value: float) -> float:
        return min(1.0, max(0.0, float(value)))

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "scope": row["scope"],
            "content": row["content"],
            "importance": row["importance"],
            "memory_key": row["memory_key"],
            "memory_type": row["memory_type"],
            "source": row["source"],
            "confidence": row["confidence"],
            "pinned": bool(row["pinned"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_used_at": row["last_used_at"],
            "use_count": row["use_count"],
        }
