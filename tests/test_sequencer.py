"""Unit tests for the C1 ordered-release primitive (spica/runtime/sequencer.py).

Covers the cases the orchestrator's reorder buffer used to handle inline:
out-of-order completion, gaps/holes, a single element, custom start index, and
the duplicate/late-index defence that the single-consumer contract relies on.
"""

import unittest

from spica.runtime.sequencer import Sequencer


class SequencerTest(unittest.TestCase):
    def test_in_order_completion_releases_immediately(self):
        seq = Sequencer()
        self.assertEqual(seq.complete(0, "a"), ["a"])
        self.assertEqual(seq.complete(1, "b"), ["b"])
        self.assertEqual(seq.complete(2, "c"), ["c"])
        self.assertEqual(seq.pending, 0)

    def test_out_of_order_completion_buffers_then_flushes(self):
        seq = Sequencer()
        self.assertEqual(seq.complete(2, "c"), [])
        self.assertEqual(seq.pending, 1)
        self.assertEqual(seq.complete(1, "b"), [])
        self.assertEqual(seq.pending, 2)
        # Filling the 0 gap flushes the whole contiguous run in order.
        self.assertEqual(seq.complete(0, "a"), ["a", "b", "c"])
        self.assertEqual(seq.pending, 0)

    def test_single_gap_then_fill(self):
        seq = Sequencer()
        self.assertEqual(seq.complete(1, "b"), [])
        self.assertEqual(seq.complete(0, "a"), ["a", "b"])
        self.assertEqual(seq.next_index, 2)

    def test_single_element(self):
        seq = Sequencer()
        self.assertEqual(seq.complete(0, "only"), ["only"])
        self.assertEqual(seq.pending, 0)
        self.assertEqual(seq.next_index, 1)

    def test_partial_release_with_trailing_gap(self):
        seq = Sequencer()
        # 0 releases; 2 is ahead of the still-missing 1 and stays buffered.
        self.assertEqual(seq.complete(0, "a"), ["a"])
        self.assertEqual(seq.complete(2, "c"), [])
        self.assertEqual(seq.pending, 1)
        self.assertEqual(seq.complete(1, "b"), ["b", "c"])
        self.assertEqual(seq.pending, 0)

    def test_custom_start_index(self):
        seq = Sequencer(start=5)
        self.assertEqual(seq.next_index, 5)
        self.assertEqual(seq.complete(6, "g"), [])
        self.assertEqual(seq.complete(5, "f"), ["f", "g"])

    def test_duplicate_index_raises(self):
        seq = Sequencer()
        seq.complete(0, "a")
        with self.assertRaises(ValueError):
            seq.complete(0, "a-again")

    def test_late_index_raises(self):
        seq = Sequencer()
        self.assertEqual(seq.complete(0, "a"), ["a"])  # next advances to 1
        with self.assertRaises(ValueError):
            seq.complete(0, "stale")  # 0 < next -> rejected

    def test_duplicate_buffered_index_raises(self):
        seq = Sequencer()
        seq.complete(2, "c")  # buffered, not yet released
        with self.assertRaises(ValueError):
            seq.complete(2, "c-again")


if __name__ == "__main__":
    unittest.main()
