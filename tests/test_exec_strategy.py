"""Unit tests for the C2 concurrency strategy (spica/runtime/exec_strategy.py).

Locks the two behaviours the orchestrator and fold rely on:
- ``Inline`` runs each lane synchronously in the caller's thread and propagates
  exceptions via the Future (so .result() raises), never swallowing them;
- ``Threaded`` runs lanes on worker threads, keeps TTS serial, and shuts down.
"""

import threading
import unittest

from spica.runtime.exec_strategy import Inline, Threaded


class InlineStrategyTest(unittest.TestCase):
    def test_lanes_run_synchronously_in_calling_thread(self):
        inline = Inline()
        caller = threading.get_ident()
        for submit in (inline.submit_visual, inline.submit_tts, inline.submit_finalize):
            f = submit(lambda: threading.get_ident())
            self.assertTrue(f.done(), "Inline future must be resolved immediately")
            self.assertEqual(f.result(), caller, "Inline runs in the calling thread")

    def test_exception_is_propagated_via_future_not_swallowed(self):
        def boom():
            raise ValueError("kaboom")

        f = Inline().submit_tts(boom)
        self.assertTrue(f.done())
        self.assertIsInstance(f.exception(), ValueError)
        with self.assertRaises(ValueError):
            f.result()

    def test_shutdown_is_a_noop(self):
        Inline().shutdown()  # must not raise


class ThreadedStrategyTest(unittest.TestCase):
    def test_lanes_run_on_worker_threads_and_return_results(self):
        t = Threaded(visual_workers=2)
        try:
            caller = threading.get_ident()
            visual = t.submit_visual(lambda: ("visual", threading.get_ident()))
            tts = t.submit_tts(lambda: ("tts", threading.get_ident()))
            finalize = t.submit_finalize(lambda: ("finalize", threading.get_ident()))
            for f, label in ((visual, "visual"), (tts, "tts"), (finalize, "finalize")):
                kind, ident = f.result(timeout=5)
                self.assertEqual(kind, label)
                self.assertNotEqual(ident, caller, "Threaded lane runs off the calling thread")
        finally:
            t.shutdown()

    def test_tts_lane_is_serial(self):
        # Product invariant (pinned into the protocol): one voice at a time.
        t = Threaded()
        try:
            self.assertEqual(t._tts._max_workers, 1)
        finally:
            t.shutdown()

    def test_exception_surfaces_on_result(self):
        t = Threaded()
        try:
            f = t.submit_finalize(lambda: 1 / 0)
            with self.assertRaises(ZeroDivisionError):
                f.result(timeout=5)
        finally:
            t.shutdown()


if __name__ == "__main__":
    unittest.main()
