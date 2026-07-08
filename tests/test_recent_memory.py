import unittest

from memory.recent import RecentMemory


class RecentMemoryTest(unittest.TestCase):
    def test_keeps_latest_three_turns(self):
        memory = RecentMemory(max_turns=3)
        for index in range(5):
            memory.append_turn("c1", f"user-{index}", f"assistant-{index}")

        turns = memory.get_recent("c1", limit=3)
        self.assertEqual([turn["user_text"] for turn in turns], ["user-2", "user-3", "user-4"])

    def test_clear(self):
        memory = RecentMemory(max_turns=3)
        memory.append_turn("c1", "hello", "hi")
        memory.clear("c1")
        self.assertEqual(memory.get_recent("c1"), [])


if __name__ == "__main__":
    unittest.main()
