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


# ---------------------------------------------------------------------------
# Session-level matrices: background (Slice 2) / final (Slice 3) rewiring.
# ---------------------------------------------------------------------------

RESULT = SummaryResult(
    summary_zh="一段剧情。", characters=["麦", "六花"], major_events=["事件"],
    unresolved_threads=["伏笔"], key_lines=["台词"], emotional_tone="日常",
    route_guess={"name": "六花线", "confidence": 0.6, "evidence": ["同行"]},
    chapter_guess={"title": "Day 1", "confidence": 0.9},
    relations=[
        {"character_a": "麦", "character_b": "六花", "relation_summary": "同行",
         "confidence": 0.7, "evidence": ["一起出发"]},
        {"character_a": "麦", "character_b": "雪", "relation_summary": "旧识",
         "confidence": 0.5, "evidence": ["回忆"]},
    ],
)


class _StubSummarizer:
    def __init__(self, result=RESULT):
        self.result = result
        self.calls = []

    def summarize(self, lines, *, recent_summaries=None, progress=None):
        self.calls.append([l.line_id for l in lines])
        return self.result


class _FailSummarizer:
    def __init__(self):
        self.calls = []

    def summarize(self, lines, *, recent_summaries=None, progress=None):
        self.calls.append([l.line_id for l in lines])
        raise SummaryError("llm boom")


# #35 malformed results -- each trips exactly ONE build stage (shared by the
# background and final matrices).
class _NoSummaryZh:
    """Missing summary_zh -> AttributeError in _build_summary (stage 1)."""
    relations = []
    route_guess = {}
    chapter_guess = {}
    major_events = []
    unresolved_threads = []


class _BadChapterGuess(SummaryResult):
    @property
    def chapter_guess(self):  # only the progress build touches chapter_guess
        raise RuntimeError("malformed chapter guess")

    @chapter_guess.setter
    def chapter_guess(self, value):
        pass  # dataclass __init__ assigns it; the getter still raises


class _BadRelations(SummaryResult):
    @property
    def relations(self):  # only _build_relations touches relations
        raise RuntimeError("malformed relations")

    @relations.setter
    def relations(self, value):
        pass


class _RecordingSink:
    """Companion event sink: records every event; ``raise_when(event)`` (optional)
    makes it throw AFTER recording; ``on_event(event)`` (optional) observes state
    at emit time (the #36 flag-before-emit probe)."""

    def __init__(self, raise_when=None, on_event=None):
        self.events = []
        self.raise_when = raise_when
        self.on_event = on_event

    def __call__(self, event):
        self.events.append(event)
        if self.on_event is not None:
            self.on_event(event)
        if self.raise_when is not None and self.raise_when(event):
            raise RuntimeError(f"sink boom at {event.kind}")

    def kinds(self):
        return [e.kind for e in self.events]

    def of(self, kind):
        return [e for e in self.events if e.kind == kind]


class _ArmedReadMemory:
    """Delegating port fake for #34: ``get_progress_state`` is called twice per
    cycle -- the first call (the summarize() argument, inside the LLM try) must
    PASS; only the ``fail_call``-th call raises. armed-after-summarize, so the
    injection hits the projection read, not the already-safe LLM lane."""

    def __init__(self, real, *, fail_call):
        self._real = real
        self.fail_call = fail_call
        self.calls = 0

    def __getattr__(self, name):
        return getattr(self._real, name)

    def get_progress_state(self, *args, **kwargs):
        self.calls += 1
        if self.calls == self.fail_call:
            raise RuntimeError("injected projection read failure")
        return self._real.get_progress_state(*args, **kwargs)


class _SessionMatrixBase(_AdapterMatrixBase):
    def _session(self, *, jobs=None, sink=None, memory=None, summarizer=None, trigger=2):
        self.sink = sink if sink is not None else _RecordingSink()
        session = GalgameCompanionSession(
            memory if memory is not None else self.mem,
            emit=self.sink,
            jobs=jobs if jobs is not None else InlineJobRunner(),
            summarizer=summarizer if summarizer is not None else _StubSummarizer(),
            summary_trigger_chars=trigger,
        )
        session.bind_game(GAME)
        session.start()
        return session

    def _feed(self, session, *texts):
        for text in texts:
            session.on_ocr_result(text)

    def _committed_ids(self):
        return {l.text: l.line_id for l in self.mem.committed_story_lines(GAME)}


class BackgroundProjectionMatrixTest(_SessionMatrixBase):
    """§9.2-6/7/8 (EXP-1 reversal) + §9.3 #12-16 background side + #17b + #22
    failure event order. Injection through the _exec_p seam; assertions cover the
    full §9.3 background set."""

    def setUp(self):
        super().setUp()
        # pre-existing durable rows: "unchanged" must mean unchanged, not empty
        self.prior_progress = _progress(last_played_at="2026-01-01T00:00:00+00:00")
        self.mem.upsert_progress_state(self.prior_progress)
        self.mem.upsert_character_relation(_relation(summary="既有关系"))

    def _assert_background_failure_shape(self, session):
        # durable: nothing from this attempt
        self.assertEqual(self.mem.recent_summaries(GAME), [])
        self.assertEqual(self.mem.get_progress_state(GAME), self.prior_progress)
        relations = self.mem.character_relations(GAME)
        self.assertEqual([r.relation_summary for r in relations], ["既有关系"])
        # buffer retains ALL source lines + reverse-lookup still unsummarized
        aa_id = self._committed_ids()["AA"]
        self.assertIn(aa_id, session.unsummarized_line_ids)
        self.assertIn(
            aa_id,
            [l.line_id for l in self.mem.unsummarized_committed_story_lines(GAME)],
        )
        # in-memory: flag cleared, FSM back to PLAYING
        self.assertFalse(session._summary_in_flight)
        self.assertEqual(session.state, GalgameState.PLAYING)
        # failure observable: error event with the pinned fields
        errors = self.sink.of("galgame_error")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, "SUMMARY_PERSIST_FAILED")
        self.assertEqual(errors[0].session_id, session.session_id)
        self.assertTrue(errors[0].session_id)
        self.assertEqual(errors[0].target_state, "background_summarizing")
        # §9.2-6/#22: failure event order -- error -> status(playing) -> done(None)
        tail = self.sink.kinds()[-3:]
        self.assertEqual(
            tail, ["galgame_error", "galgame_status_changed", "galgame_summary_done"]
        )
        self.assertIsNone(self.sink.of("galgame_summary_done")[-1].summary_id)

    def _run_background_injected(self, marker, *, nth=1):
        self._last_injector = self._inject(marker, nth=nth)
        session = self._session()
        self._feed(session, "AA", "AA", "BB", "BB")  # commit AA -> trigger -> inline job
        self._assert_background_failure_shape(session)
        return session

    def test_6_background_progress_failure_threadrunner_production_shape(self):
        # §9.2-6: EXP-1 reversal in the PRODUCTION job-runner shape.
        self._inject("INSERT OR REPLACE INTO progress_states")
        jobs = ThreadJobRunner()
        session = self._session(jobs=jobs)
        self._feed(session, "AA", "AA", "BB", "BB")
        jobs.drain(timeout=5.0)
        deadline = time.time() + 3.0
        while session.state == GalgameState.BACKGROUND_SUMMARIZING and time.time() < deadline:
            time.sleep(0.02)
        self._assert_background_failure_shape(session)

    def test_12_background_summary_insert_failure(self):
        self._run_background_injected("INSERT OR REPLACE INTO story_summaries")

    def test_13_background_progress_upsert_failure(self):
        self._run_background_injected("INSERT OR REPLACE INTO progress_states")

    def test_14_background_relation_1_failure(self):
        self._run_background_injected("INSERT INTO character_relations", nth=1)

    def test_15_background_relation_n_failure(self):
        self._run_background_injected("INSERT INTO character_relations", nth=2)

    def test_16_background_commit_failure(self):
        self._run_background_injected("COMMIT")

    def test_7_8_17b_failed_batch_folds_into_next_trigger_exactly_once(self):
        # §9.2-7 (duplicate-coverage reversal) + §9.2-8 (no stuck flag) + #17b
        # (live retry): the failed batch folds into the NEXT trigger and the
        # buffer advances exactly once.
        session = self._run_background_injected("INSERT OR REPLACE INTO progress_states")
        self._last_injector.armed = False  # heal the store
        started_before = len(self.sink.of("galgame_summary_started"))
        self._feed(session, "CC", "CC")  # commit BB -> trigger again (flag not stuck)
        self.assertEqual(
            len(self.sink.of("galgame_summary_started")), started_before + 1
        )  # §9.2-8: a new background summary DID start
        summaries = self.mem.recent_summaries(GAME)
        self.assertEqual(len(summaries), 1)  # the SAME batch landed exactly once
        ids = self._committed_ids()
        self.assertEqual(set(summaries[0].source_line_ids), {ids["AA"], ids["BB"]})
        # buffer advanced exactly once: nothing left unsummarized
        self.assertEqual(session.unsummarized_line_ids, ())
        self.assertEqual(session.state, GalgameState.PLAYING)
        self.assertFalse(session._summary_in_flight)


class BackgroundFoldExpansionTest(_SessionMatrixBase):
    """#34 (armed projection read) / #35 (three build stages) / #36 (failure-stage
    armed sink) / #18a (post-commit sink boundary) -- background side."""

    def test_34_second_progress_read_failure_folds_with_persist_code(self):
        armed = _ArmedReadMemory(self.mem, fail_call=2)
        session = self._session(memory=armed)
        self._feed(session, "AA", "AA", "BB", "BB")
        # durable: trivially zero (no transaction was ever opened)
        self.assertEqual(self.mem.recent_summaries(GAME), [])
        self.assertIsNone(self.mem.get_progress_state(GAME))
        aa_id = self._committed_ids()["AA"]
        self.assertIn(aa_id, session.unsummarized_line_ids)
        self.assertFalse(session._summary_in_flight)
        self.assertEqual(session.state, GalgameState.PLAYING)
        errors = self.sink.of("galgame_error")
        self.assertEqual([e.code for e in errors], ["SUMMARY_PERSIST_FAILED"])
        self.assertIn("progress read", errors[0].message)

    def test_34_first_progress_read_failure_stays_on_llm_lane(self):
        # #34 对照钉: the FIRST get_progress_state (the summarize() argument) is
        # inside the LLM try -- its failure folds on the EXISTING lane: no
        # SUMMARY_PERSIST_FAILED, just Done(None) + PLAYING.
        armed = _ArmedReadMemory(self.mem, fail_call=1)
        session = self._session(memory=armed)
        self._feed(session, "AA", "AA", "BB", "BB")
        self.assertEqual(self.sink.of("galgame_error"), [])
        self.assertIsNone(self.sink.of("galgame_summary_done")[-1].summary_id)
        self.assertEqual(session.state, GalgameState.PLAYING)
        self.assertFalse(session._summary_in_flight)
        self.assertEqual(self.mem.recent_summaries(GAME), [])

    def _run_build_failure(self, result, expected_stage):
        session = self._session(summarizer=_StubSummarizer(result))
        self._feed(session, "AA", "AA", "BB", "BB")
        self.assertEqual(self.mem.recent_summaries(GAME), [])
        self.assertIsNone(self.mem.get_progress_state(GAME))
        self.assertEqual(self.mem.character_relations(GAME), [])
        aa_id = self._committed_ids()["AA"]
        self.assertIn(aa_id, session.unsummarized_line_ids)
        self.assertFalse(session._summary_in_flight)
        self.assertEqual(session.state, GalgameState.PLAYING)
        errors = self.sink.of("galgame_error")
        self.assertEqual([e.code for e in errors], ["SUMMARY_PERSIST_FAILED"])
        self.assertIn(expected_stage, errors[0].message)

    def test_35_summary_build_failure_folds(self):
        self._run_build_failure(_NoSummaryZh(), "summary build")

    def test_35_progress_build_failure_folds(self):
        self._run_build_failure(
            _BadChapterGuess(summary_zh="S", route_guess={"name": "X"}), "progress build"
        )

    def test_35_relations_build_failure_folds(self):
        self._run_build_failure(_BadRelations(summary_zh="S"), "relations build")

    def test_36_failure_stage_armed_sink_cannot_truncate_the_fold(self):
        # sink raises from the durable-apply failure fold onward (galgame_error and
        # every later emit); the fold must still restore flag+FSM and stay durable-
        # clean, and the flag must ALREADY be False when the first emit fires.
        state = {"armed": False, "flag_at_error": None}

        def _arm(event):
            if event.kind == "galgame_error":
                state["armed"] = True

        sink = _RecordingSink(raise_when=lambda e: state["armed"] or e.kind == "galgame_error")
        injector = self._inject("INSERT OR REPLACE INTO progress_states")
        session_holder = {}

        def _observe(event):
            if event.kind == "galgame_error":
                state["flag_at_error"] = session_holder["s"]._summary_in_flight
            _arm(event)

        sink.on_event = _observe
        session = self._session(sink=sink)
        session_holder["s"] = session
        # InlineJobRunner: the LAST sink raise propagates out of the job into the
        # feed -- the production ThreadJobRunner would swallow it (jobs.py).
        with self.assertRaises(RuntimeError):
            self._feed(session, "AA", "AA", "BB", "BB")
        self.assertIs(state["flag_at_error"], False)  # ① flag before ALL emits
        self.assertFalse(session._summary_in_flight)
        self.assertEqual(session.state, GalgameState.PLAYING)  # mutation before emit
        self.assertEqual(self.mem.recent_summaries(GAME), [])  # durable clean
        # a later trigger can start a fresh summary (nothing stuck)
        injector.armed = False
        sink.raise_when = None
        started_before = len(sink.of("galgame_summary_started"))
        self._feed(session, "CC", "CC")
        self.assertEqual(len(sink.of("galgame_summary_started")), started_before + 1)
        self.assertEqual(len(self.mem.recent_summaries(GAME)), 1)

    def _run_post_commit_sink(self, raise_when):
        sink = _RecordingSink(raise_when=raise_when)
        session = self._session(sink=sink)
        with self.assertRaises(RuntimeError):
            self._feed(session, "AA", "AA", "BB", "BB")
        # commit succeeded -> NOT a failure: no persist-failed event, durable kept,
        # buffer advanced, flag cleared, FSM at PLAYING (mutation precedes emit).
        self.assertEqual(sink.of("galgame_error"), [])
        self.assertEqual(len(self.mem.recent_summaries(GAME)), 1)
        self.assertNotIn(self._committed_ids()["AA"], session.unsummarized_line_ids)
        self.assertFalse(session._summary_in_flight)
        self.assertEqual(session.state, GalgameState.PLAYING)

    def test_18a_post_commit_status_sink_failure_is_not_a_rollback(self):
        # armed only after the summary started -- start()'s own status(playing)
        # emit must NOT trip the sink (prelude raising is out of scope, v1.2).
        seen = {"started": False}

        def _pred(event):
            if event.kind == "galgame_summary_started":
                seen["started"] = True
                return False
            return (
                seen["started"]
                and event.kind == "galgame_status_changed"
                and event.state == "playing"
            )

        self._run_post_commit_sink(_pred)

    def test_18a_post_commit_done_sink_failure_is_not_a_rollback(self):
        self._run_post_commit_sink(
            lambda e: e.kind == "galgame_summary_done" and e.summary_id is not None
        )

if __name__ == "__main__":
    unittest.main()
