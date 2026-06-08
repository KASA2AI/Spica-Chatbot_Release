"""C5 unit tests for TurnObserver (spica/runtime/observer.py).

Locks the semantics the stage layer relies on: span stores ``{name}_ms`` + logs,
mark/mark_once/bump store (set / keep-first / accumulate, no log), event logs
without storing, snapshot reflects the sink, and the sink is the caller's dict
(so done.timing stays the same object). Noop records nothing.
"""

import threading
import unittest

from spica.runtime.observer import DefaultTurnObserver, NoopTurnObserver


class _SpyLogger:
    def __init__(self):
        self.calls = []

    def __call__(self, step, value, **fields):
        self.calls.append((step, value, fields))


class DefaultTurnObserverTest(unittest.TestCase):
    def test_writes_into_the_provided_sink(self):
        sink = {}
        obs = DefaultTurnObserver(sink, logger=_SpyLogger())
        obs.mark("agent_model", "fake-model")
        # The sink IS the caller's dict -> done.timing / response_payload unchanged.
        self.assertEqual(sink["agent_model"], "fake-model")
        self.assertEqual(obs.snapshot()["agent_model"], "fake-model")

    def test_span_stores_ms_and_logs(self):
        sink = {}
        logger = _SpyLogger()
        obs = DefaultTurnObserver(sink, logger=logger)
        with obs.span("validate_input_node", conversation_id="c1"):
            pass
        self.assertIn("validate_input_node_ms", sink)
        self.assertIsInstance(sink["validate_input_node_ms"], (int, float))
        self.assertEqual(len(logger.calls), 1)
        step, value, fields = logger.calls[0]
        self.assertEqual(step, "validate_input_node")
        self.assertEqual(fields, {"conversation_id": "c1"})

    def test_span_records_even_on_exception(self):
        sink = {}
        obs = DefaultTurnObserver(sink, logger=_SpyLogger())
        with self.assertRaises(ValueError):
            with obs.span("boom"):
                raise ValueError("x")
        self.assertIn("boom_ms", sink)

    def test_mark_once_keeps_first(self):
        sink = {}
        obs = DefaultTurnObserver(sink, logger=_SpyLogger())
        obs.mark_once("first_unit_created_ms", 12.0)
        obs.mark_once("first_unit_created_ms", 99.0)
        self.assertEqual(sink["first_unit_created_ms"], 12.0)

    def test_mark_overwrites(self):
        sink = {}
        obs = DefaultTurnObserver(sink, logger=_SpyLogger())
        obs.mark("agent_rounds", 1)
        obs.mark("agent_rounds", 2)
        self.assertEqual(sink["agent_rounds"], 2)

    def test_bump_accumulates_from_zero(self):
        sink = {}
        obs = DefaultTurnObserver(sink, logger=_SpyLogger())
        obs.bump("agent_function_calls", 1)
        obs.bump("agent_function_calls", 1)
        obs.bump("agent_function_calls", 1)
        self.assertEqual(sink["agent_function_calls"], 3)

    def test_event_logs_without_storing(self):
        sink = {}
        logger = _SpyLogger()
        obs = DefaultTurnObserver(sink, logger=logger)
        obs.event("tool_schema_gate", 0.0, use_tools=False, user_chars=2)
        self.assertEqual(sink, {})  # log-only: never enters the timing snapshot
        self.assertEqual(logger.calls, [("tool_schema_gate", 0.0, {"use_tools": False, "user_chars": 2})])

    def test_snapshot_is_a_copy(self):
        sink = {"a": 1}
        obs = DefaultTurnObserver(sink, logger=_SpyLogger())
        snap = obs.snapshot()
        snap["a"] = 999
        self.assertEqual(sink["a"], 1)

    def test_concurrent_marks_are_threadsafe(self):
        sink = {}
        obs = DefaultTurnObserver(sink, logger=_SpyLogger())

        def worker():
            for _ in range(1000):
                obs.bump("n", 1)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sink["n"], 4000)


class NoopTurnObserverTest(unittest.TestCase):
    def test_records_nothing(self):
        obs = NoopTurnObserver()
        with obs.span("x", foo=1):
            obs.mark("a", 1)
            obs.mark_once("b", 2)
            obs.bump("c", 3)
            obs.event("e", 0.0, k=1)
        self.assertEqual(obs.snapshot(), {})


if __name__ == "__main__":
    unittest.main()
