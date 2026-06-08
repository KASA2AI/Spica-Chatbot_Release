"""C6 unit tests for the JobRunner implementations (spica/runtime/jobs.py).

InlineJobRunner runs synchronously in the calling thread (so the sync path / tests
see the long-term commit immediately). ThreadJobRunner runs off-thread and drain
waits, so the streaming `done` is not blocked but no commit thread leaks the turn.
"""

import threading
import unittest

from spica.runtime.jobs import InlineJobRunner, ThreadJobRunner


class InlineJobRunnerTest(unittest.TestCase):
    def test_runs_synchronously_on_the_calling_thread(self):
        runner = InlineJobRunner()
        ran_on = []
        runner.submit(lambda: ran_on.append(threading.get_ident()))
        # Already executed by the time submit returns, on this thread.
        self.assertEqual(ran_on, [threading.get_ident()])

    def test_drain_is_a_noop(self):
        runner = InlineJobRunner()
        runner.drain()  # must not raise


class ThreadJobRunnerTest(unittest.TestCase):
    def test_runs_off_the_calling_thread(self):
        runner = ThreadJobRunner()
        ran_on = []
        runner.submit(lambda: ran_on.append(threading.get_ident()))
        runner.drain()
        self.assertEqual(len(ran_on), 1)
        self.assertNotEqual(ran_on[0], threading.get_ident())

    def test_drain_waits_for_completion(self):
        runner = ThreadJobRunner()
        gate = threading.Event()
        committed = []

        def job():
            gate.wait(timeout=2)
            committed.append("done")

        runner.submit(job)
        self.assertEqual(committed, [])  # not blocked: submit returned before the job finished
        gate.set()
        runner.drain()
        self.assertEqual(committed, ["done"])  # drain joined the worker

    def test_drain_clears_finished_threads(self):
        runner = ThreadJobRunner()
        runner.submit(lambda: None)
        runner.drain()
        self.assertEqual(runner._threads, [])


if __name__ == "__main__":
    unittest.main()
