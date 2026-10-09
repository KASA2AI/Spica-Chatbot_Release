"""Local multilingual retrieval; the SQLite vectors are disposable derivatives."""

from __future__ import annotations

from contextlib import closing
from concurrent.futures import CancelledError
from pathlib import Path
import threading
from typing import Callable

MODEL_REPO = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_REVISION = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
MODEL_FILE = "onnx/model_quint8_avx2.onnx"
DEFAULT_MODEL_DIR = "models/memory/paraphrase-multilingual-MiniLM-L12-v2"


class MemorySemanticIndex:
    def __init__(self, store, model_dir: str | Path) -> None:
        self.store = store
        path = Path(model_dir)
        self.model_dir = path if path.is_absolute() else Path(__file__).resolve().parents[1] / path
        self._lock = threading.Lock()
        self._session = None
        self._tokenizer = None

    def _load(self) -> None:
        if self._session is not None:
            return
        # Runtime is offline. Setup downloads pinned public weights explicitly;
        # missing files simply make semantic recall temporarily unavailable.
        if (self.model_dir / "revision.txt").read_text().strip() != MODEL_REVISION:
            raise ValueError("memory embedding model revision mismatch")
        import onnxruntime as ort
        from tokenizers import Tokenizer
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(str(self.model_dir / MODEL_FILE), sess_options=options,
                                       providers=["CPUExecutionProvider"])
        tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
        self._pad_id = tokenizer.token_to_id("<pad>")
        tokenizer.enable_padding(pad_id=self._pad_id, pad_token="<pad>")
        tokenizer.enable_truncation(max_length=128, stride=24)
        self._session, self._tokenizer = session, tokenizer

    def _encode(self, text: str):
        import numpy as np
        encoded = self._tokenizer.encode(text)
        # Index every token window of a long record. Selection still returns
        # whole source-backed records, never a truncated embedding window.
        windows = [encoded, *encoded.overflowing]
        vectors = []
        for start in range(0, len(windows), 16):
            batch = windows[start:start + 16]
            width = max(len(item.ids) for item in batch)
            inputs = {
                "input_ids": np.array([item.ids + [self._pad_id] * (width - len(item.ids)) for item in batch], dtype=np.int64),
                "attention_mask": np.array([item.attention_mask + [0] * (width - len(item.ids)) for item in batch], dtype=np.int64),
                "token_type_ids": np.array([item.type_ids + [0] * (width - len(item.ids)) for item in batch], dtype=np.int64),
            }
            hidden = self._session.run(None, {item.name: inputs[item.name] for item in self._session.get_inputs()})[0]
            mask = inputs["attention_mask"][:, :, None]
            pooled = (hidden * mask).sum(1) / mask.sum(1)
            normalized = pooled / np.linalg.norm(pooled, axis=1, keepdims=True)
            if not np.isfinite(normalized).all():
                raise ValueError("invalid memory embedding")
            vectors.extend(normalized.astype("<f4"))
        return np.stack(vectors)

    @staticmethod
    def _schema(db) -> None:
        # execute(), unlike executescript(), keeps DDL inside the caller's
        # transaction so a stopped maintenance worker can roll it back too.
        for statement in ("""
            CREATE TABLE IF NOT EXISTS memory_vectors (
                entry_id INTEGER PRIMARY KEY, model_revision TEXT NOT NULL,
                vectors BLOB NOT NULL
            )""", """
            CREATE TRIGGER IF NOT EXISTS memory_vectors_withdraw
                AFTER UPDATE OF status ON memory_entries
                WHEN NEW.status IN ('deleted','retracted')
                BEGIN DELETE FROM memory_vectors WHERE entry_id=NEW.id; END""", """
            CREATE TRIGGER IF NOT EXISTS memory_vectors_erase
                AFTER DELETE ON memory_entries
                BEGIN DELETE FROM memory_vectors WHERE entry_id=OLD.id; END"""):
            db.execute(statement)

    def scores(self, query: str, entries: list[dict], *, publish=None) -> dict[int, float]:
        if not entries:
            return {}
        import numpy as np
        with self._lock:
            self._load()
            with closing(self.store._connect()) as db, db:
                db.execute("BEGIN")
                self._schema(db)
                saved = {row["entry_id"]: row["vectors"] for row in db.execute(
                    "SELECT * FROM memory_vectors WHERE model_revision=?", (MODEL_REVISION,))}
                if publish is not None and not publish(db):
                    return {}
            query_vectors = self._encode(query)
            results = {}
            for entry in entries:
                raw = saved.get(entry["id"])
                vector = np.frombuffer(raw, dtype="<f4").reshape(-1, 384) if raw else self._encode(entry["content"])
                if not len(vector) or not np.isfinite(vector).all():
                    raise ValueError("invalid memory vector index")
                if raw is None:
                    with closing(self.store._connect()) as db, db:
                        # A concurrent correction/delete cannot republish its
                        # obsolete vector after the withdrawal trigger ran.
                        db.execute("""INSERT OR REPLACE INTO memory_vectors SELECT id,?,?
                            FROM memory_entries WHERE id=? AND content=? AND status IN ('active','superseded')""",
                            (MODEL_REVISION, vector.tobytes(), entry["id"], entry["content"]))
                        if publish is not None and not publish(db):
                            return {}
                results[entry["id"]] = float((query_vectors @ vector.T).max())
            return results

    def similarity(self, query: str, content: str) -> float:
        """Compare transient source text without persisting it as a memory vector."""
        return self.similarity_for(query)(content)

    def similarity_for(self, query: str, *, cancelled: threading.Event | None = None) -> Callable[[str], float]:
        """One read owns the query vector and duplicate-text scores, never the index."""
        query_vectors = None
        scores: dict[str, float] = {}

        def check_cancelled():
            if cancelled is not None and cancelled.is_set():
                raise CancelledError('memory retrieval cancelled')

        def compare(content: str) -> float:
            nonlocal query_vectors
            check_cancelled()
            if content in scores:
                return scores[content]
            if cancelled is None:
                self._lock.acquire()
            else:
                while not self._lock.acquire(timeout=.05):
                    check_cancelled()
            try:
                check_cancelled()
                self._load()
                check_cancelled()
                if query_vectors is None:
                    query_vectors = self._encode(query)
                check_cancelled()
                vectors = query_vectors if content == query else self._encode(content)
                check_cancelled()
                score = float((query_vectors @ vectors.T).max())
                scores[content] = score
                return score
            finally:
                self._lock.release()

        return compare

    def rebuild(self) -> int:
        with self._lock, closing(self.store._connect()) as db, db:
            self._schema(db)
            db.execute("DELETE FROM memory_vectors")
            entries = [dict(row) for row in db.execute(
                "SELECT id,content FROM memory_entries WHERE status IN ('active','superseded')")]
        if entries:
            self.scores("索引重建", entries)
        return len(entries)
