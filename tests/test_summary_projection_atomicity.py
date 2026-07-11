"""AR-C1: summary projection atomicity -- fault-injection contract (§9).

``apply_summary_projection`` persists one summary projection (StorySummary insert
+ GameProgressState upsert + 0..N CharacterRelation upserts) in ONE transaction:
any failure leaves NOTHING durable from the attempt. ``_exec_p`` is the single
fault-injection seam (COMMIT included). Adapter-level matrix lives here
(Slice 1); the background/final session rewiring matrices join in Slices 2/3.
"""

import sqlite3
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.galgame.models import (
    CharacterRelation,
    GameProgressState,
    StorySummary,
    utc_now_iso,
)
from spica.galgame.session import GalgameCompanionSession, GalgameState, GalgameStateError
from spica.galgame.summarizer import SummaryError, SummaryResult, recover_dangling_sessions
from spica.runtime.jobs import InlineJobRunner, ThreadJobRunner

GAME = "ABC"


def _summary(summary_id="SUM1", *, line_ids=("L1", "L2")):
    now = utc_now_iso()
    return StorySummary(
        summary_id=summary_id, game_id=GAME, session_id="S1",
        source_line_ids=list(line_ids), summary_zh="一段剧情。",
        key_original_lines=["台词"], characters=["麦", "六花"],
        major_events=["事件"], unresolved_threads=["伏笔"],
        route_guess={"name": "六花线", "confidence": 0.6, "evidence": ["同行"]},
        created_at=now, updated_at=now, source="auto_summary",
    )


def _progress(**overrides):
    fields = dict(
        game_id=GAME, last_played_at=utc_now_iso(),
        route={"confirmed": False, "name": "六花线", "confidence": 0.6,
               "evidence": ["同行"], "source": "llm_guess"},
        chapter={"title": "Day 1", "confidence": 0.9, "source": "llm_guess"},
        major_events=["事件"], unresolved_threads=["伏笔"],
    )
    fields.update(overrides)
    return GameProgressState(**fields)


def _relation(a="麦", b="六花", *, summary="同行", playthrough_id="default"):
    return CharacterRelation(
        relation_id=f"rel::{a}::{b}", game_id=GAME, playthrough_id=playthrough_id,
        character_a=a, character_b=b, relation_summary=summary,
        evidence=["一起出发"], confidence=0.7, updated_at=utc_now_iso(),
        source="auto_summary",
    )


class _ExecPInjector:
    """Wraps the real ``_exec_p``: raises on the ``nth`` statement containing
    ``marker``, delegates everything else. ``before`` (optional) runs just before
    the marker statement executes -- the §9.4-30 mid-transaction observation hook."""

    def __init__(self, marker, *, nth=1, before=None):
        self.marker = marker
        self.nth = nth
        self.before = before
        self.armed = True  # set False to disarm without unpatching (retry tests)
        self.seen = 0
        self._real = GameMemorySqliteAdapter._exec_p

    def __call__(self, conn, sql, params=()):
        if self.armed and self.marker in sql:
            self.seen += 1
            if self.seen == self.nth:
                if self.before is not None:
                    self.before()
                    return self._real(conn, sql, params)
                raise sqlite3.OperationalError(f"injected failure at {self.marker!r}")
        return self._real(conn, sql, params)


class _AdapterMatrixBase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mem = GameMemorySqliteAdapter(Path(self._tmp.name) / "g.sqlite3")

    def _inject(self, marker, *, nth=1, before=None):
        injector = _ExecPInjector(marker, nth=nth, before=before)
        patcher = mock.patch.object(
            GameMemorySqliteAdapter, "_exec_p", staticmethod(injector)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return injector

    def _assert_begin_immediate_ok(self):
        # §9.3-19: no lingering transaction -- a fresh connection can take the
        # write lock IMMEDIATELY (tiny busy_timeout so a leak fails fast).
        conn = sqlite3.connect(self.mem.db_path)
        try:
            conn.isolation_level = None
            conn.execute("PRAGMA busy_timeout=100")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
        finally:
            conn.close()


class ApplySummaryProjectionSuccessTest(_AdapterMatrixBase):
    def test_returns_summary_id_and_lands_all_three_tables(self):
        returned = self.mem.apply_summary_projection(
            _summary(), _progress(), [_relation(), _relation("麦", "雪")]
        )
        self.assertEqual(returned, "SUM1")
        self.assertEqual([s.summary_id for s in self.mem.recent_summaries(GAME)], ["SUM1"])
        self.assertIsNotNone(self.mem.get_progress_state(GAME))
        self.assertEqual(len(self.mem.character_relations(GAME)), 2)

    def test_empty_relations_is_legal(self):
        # §9.4-27
        self.mem.apply_summary_projection(_summary(), _progress(), [])
        self.assertEqual(len(self.mem.recent_summaries(GAME)), 1)
        self.assertIsNotNone(self.mem.get_progress_state(GAME))
        self.assertEqual(self.mem.character_relations(GAME), [])

    def test_rows_identical_to_single_method_calls(self):
        # §9.4-26: aggregate vs the three single methods, field by field.
        summary, progress = _summary(), _progress()
        relations = [_relation(), _relation("麦", "雪", summary="旧识")]
        self.mem.apply_summary_projection(summary, progress, relations)

        other = GameMemorySqliteAdapter(Path(self._tmp.name) / "single.sqlite3")
        other.add_summary(summary)
        other.upsert_progress_state(progress)
        for relation in relations:
            other.upsert_character_relation(relation)

        self.assertEqual(self.mem.recent_summaries(GAME), other.recent_summaries(GAME))
        self.assertEqual(self.mem.get_progress_state(GAME), other.get_progress_state(GAME))
        self.assertEqual(
            sorted(self.mem.character_relations(GAME), key=lambda r: r.relation_id),
            sorted(other.character_relations(GAME), key=lambda r: r.relation_id),
        )

    def test_relation_conflict_semantics_scoped_rewrite_no_new_rows(self):
        # §9.4-26 (AR-C0 semantics re-verified through the new command): the same
        # scope rewrites its own row; another playthrough scope is never touched.
        self.mem.upsert_character_relation(
            _relation(summary="别本的关系", playthrough_id="other")
        )
        self.mem.apply_summary_projection(_summary(), _progress(), [_relation(summary="v1")])
        self.mem.apply_summary_projection(
            _summary("SUM2"), _progress(), [_relation(summary="v2")]
        )
        default_rows = self.mem.character_relations(GAME)
        self.assertEqual(len(default_rows), 1)  # same scope rewrote, no new row
        self.assertEqual(default_rows[0].relation_summary, "v2")
        other_rows = self.mem.character_relations(GAME, "other")
        self.assertEqual(len(other_rows), 1)
        self.assertEqual(other_rows[0].relation_summary, "别本的关系")  # untouched

    def test_retry_after_failure_succeeds_with_same_values(self):
        # §9.3-17a: adapter command retry -- same materialized values, exactly one
        # summary, rows equal to a first-time success.
        injector = self._inject("INSERT OR REPLACE INTO progress_states")
        summary, progress = _summary(), _progress()
        relations = [_relation()]
        with self.assertRaises(sqlite3.OperationalError):
            self.mem.apply_summary_projection(summary, progress, relations)
        injector.armed = False  # disarm -> retry runs clean

        self.mem.apply_summary_projection(summary, progress, relations)
        self.assertEqual([s.summary_id for s in self.mem.recent_summaries(GAME)], ["SUM1"])

        other = GameMemorySqliteAdapter(Path(self._tmp.name) / "retry.sqlite3")
        other.apply_summary_projection(summary, progress, relations)
        self.assertEqual(self.mem.recent_summaries(GAME), other.recent_summaries(GAME))
        self.assertEqual(self.mem.get_progress_state(GAME), other.get_progress_state(GAME))
        self.assertEqual(self.mem.character_relations(GAME), other.character_relations(GAME))

    def test_reader_never_blocks_and_never_sees_a_partial_projection(self):
        # §9.4-30 (EXP-4a at adapter level): observed right before COMMIT executes,
        # a separate reader connection sees the OLD snapshot (zero rows) without
        # blocking; after the call, all three tables are visible atomically.
        observed = {}

        def _read_mid_transaction():
            reader = sqlite3.connect(self.mem.db_path)
            try:
                reader.execute("PRAGMA busy_timeout=100")  # non-blocking or bust
                observed["summaries"] = reader.execute(
                    "SELECT COUNT(*) FROM story_summaries").fetchone()[0]
                observed["progress"] = reader.execute(
                    "SELECT COUNT(*) FROM progress_states").fetchone()[0]
                observed["relations"] = reader.execute(
                    "SELECT COUNT(*) FROM character_relations").fetchone()[0]
            finally:
                reader.close()

        self._inject("COMMIT", before=_read_mid_transaction)
        self.mem.apply_summary_projection(_summary(), _progress(), [_relation()])
        self.assertEqual(observed, {"summaries": 0, "progress": 0, "relations": 0})
        self.assertEqual(len(self.mem.recent_summaries(GAME)), 1)
        self.assertIsNotNone(self.mem.get_progress_state(GAME))
        self.assertEqual(len(self.mem.character_relations(GAME)), 1)


class ApplySummaryProjectionFaultMatrixTest(_AdapterMatrixBase):
    """§9.3 #12-16 at the adapter level: every injection point -> nothing durable
    from the attempt, pre-existing rows untouched, no lingering transaction, the
    primary (injected) exception propagates unmasked."""

    PRIOR_RELATION_SUMMARY = "既有关系"

    def setUp(self):
        super().setUp()
        # pre-existing durable state -- "unchanged" must mean UNCHANGED, not empty
        self.prior_progress = _progress(last_played_at="2026-01-01T00:00:00+00:00")
        self.mem.upsert_progress_state(self.prior_progress)
        self.mem.upsert_character_relation(
            _relation(summary=self.PRIOR_RELATION_SUMMARY)
        )

    def _assert_attempt_left_nothing(self):
        self.assertEqual(self.mem.recent_summaries(GAME), [])
        self.assertEqual(self.mem.get_progress_state(GAME), self.prior_progress)
        relations = self.mem.character_relations(GAME)
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0].relation_summary, self.PRIOR_RELATION_SUMMARY)
        self._assert_begin_immediate_ok()

    def _run_injected(self, marker, *, nth=1, relations=None):
        self._inject(marker, nth=nth)
        if relations is None:
            relations = [_relation(summary="新1"), _relation("麦", "雪", summary="新2")]
        with self.assertRaises(sqlite3.OperationalError) as ctx:
            self.mem.apply_summary_projection(_summary(), _progress(), relations)
        # §9.3-19: primary exception unmasked by the ROLLBACK path
        self.assertIn("injected failure", str(ctx.exception))
        self._assert_attempt_left_nothing()

    def test_12_summary_insert_failure(self):
        self._run_injected("INSERT OR REPLACE INTO story_summaries")

    def test_13_progress_upsert_failure(self):
        self._run_injected("INSERT OR REPLACE INTO progress_states")

    def test_14_relation_1_failure(self):
        self._run_injected("INSERT INTO character_relations", nth=1)

    def test_15_relation_n_failure(self):
        # EXP-3 reversal at adapter level: relation 2 of 2 fails -> relation 1 must
        # NOT survive (the old per-commit path leaked it).
        self._run_injected("INSERT INTO character_relations", nth=2)

    def test_16_commit_failure(self):
        self._run_injected("COMMIT")

if __name__ == "__main__":
    unittest.main()
