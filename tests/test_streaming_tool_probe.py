"""Streaming chat tool probe (Plan: stream the probe so a no-tool answer plays as
it generates instead of waiting for the whole non-streamed reply).

Adapter-level pins for iter_chat_with_tools (tool_call delta accumulation + live
content) and the NO_COMMENT-under-chunking pin (edge 2). The end-to-end RESET /
preamble-dropped behaviour is pinned in test_chat_tool_round.py on the real chain.
"""

import unittest
from types import SimpleNamespace

from spica.adapters.llm.openai_compatible import OpenAICompatibleAdapter
from spica.core.proactive import is_no_comment_answer, may_become_no_comment
from spica.runtime.play_unit_splitter import JsonAnswerExtractor


def _chunk(content=None, tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=content, tool_calls=tool_calls))])


def _tc(index, name=None, arguments=""):
    return SimpleNamespace(index=index, function=SimpleNamespace(name=name, arguments=arguments))


class _FakeChat:
    def __init__(self, chunks):
        self._chunks = chunks
        self.completions = self
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return iter(self._chunks)


class _FakeClient:
    def __init__(self, chunks):
        self.base_url = "https://api.deepseek.com/v1"
        self.chat = _FakeChat(chunks)


def _adapter(chunks):
    return OpenAICompatibleAdapter(_FakeClient(chunks))


class IterChatWithToolsTest(unittest.TestCase):
    def test_tool_call_arguments_accumulate_across_chunks(self):
        # ①: the streamed function.arguments arrive in fragments keyed by index.
        chunks = [
            _chunk(tool_calls=[_tc(0, name="watch_game_screen", arguments='{"qu')]),
            _chunk(tool_calls=[_tc(0, arguments='estion":')]),
            _chunk(tool_calls=[_tc(0, arguments=' "穿什么"}')]),
        ]
        adapter = _adapter(chunks)
        sink = []
        text = "".join(adapter.iter_chat_with_tools(
            model="m", prompt="p", tools=[], state=SimpleNamespace(timing={}), tool_calls_sink=sink))
        self.assertEqual(text, "")  # no content -> nothing to play
        self.assertEqual(sink, [{"name": "watch_game_screen", "arguments": '{"question": "穿什么"}'}])
        self.assertTrue(adapter.client.chat.last_kwargs["stream"])  # streamed request

    def test_content_streams_live_and_no_tool(self):
        # ②: a no-tool answer yields content deltas live; sink stays empty.
        chunks = [_chunk(content='{"answer":"你好'), _chunk(content='世界"}')]
        adapter = _adapter(chunks)
        sink = []
        deltas = list(adapter.iter_chat_with_tools(
            model="m", prompt="p", tools=[], state=SimpleNamespace(timing={}), tool_calls_sink=sink))
        self.assertEqual(deltas, ['{"answer":"你好', '世界"}'])  # yielded as they arrive
        self.assertEqual(sink, [])  # no tool

    def test_plain_preamble_then_tool_call(self):
        # The empirically-observed shape: plain preamble content, THEN a tool_call.
        # The adapter yields the preamble (the caller's JsonAnswerExtractor drops it
        # because it has no "answer" field) and still surfaces the tool call.
        chunks = [
            _chunk(content="让我先看看屏幕"),
            _chunk(tool_calls=[_tc(0, name="watch_game_screen", arguments="{}")]),
        ]
        adapter = _adapter(chunks)
        sink = []
        deltas = list(adapter.iter_chat_with_tools(
            model="m", prompt="p", tools=[], state=SimpleNamespace(timing={}), tool_calls_sink=sink))
        self.assertEqual(deltas, ["让我先看看屏幕"])
        self.assertEqual(sink, [{"name": "watch_game_screen", "arguments": "{}"}])
        # And that plain preamble carries no "answer" -> extractor yields nothing.
        self.assertEqual(JsonAnswerExtractor().feed("让我先看看屏幕"), "")

    def test_two_tool_calls_distinct_indexes(self):
        chunks = [
            _chunk(tool_calls=[_tc(0, name="a", arguments="{}"), _tc(1, name="b", arguments='{"x":1}')]),
        ]
        sink = []
        list(_adapter(chunks).iter_chat_with_tools(
            model="m", prompt="p", tools=[], state=SimpleNamespace(timing={}), tool_calls_sink=sink))
        self.assertEqual(sink, [{"name": "a", "arguments": "{}"}, {"name": "b", "arguments": '{"x":1}'}])


class NoCommentChunkedTest(unittest.TestCase):
    """edge 2: NO_COMMENT (system-turn sentinel) survives chunked delivery. System
    turns don't even use the probe, but the sentinel check is accumulative + prefix-
    tolerant, so streamed deltas never split it unrecognizably."""

    def test_sentinel_recognized_across_chunked_deltas(self):
        extractor = JsonAnswerExtractor()
        raw = ""
        for delta in ['{"answer":"NO', '_COMM', 'ENT"}']:
            raw += delta
            extractor.feed(raw)
            # while still a prefix of the sentinel, the system-turn hold holds.
            self.assertTrue(may_become_no_comment(extractor.answer) or is_no_comment_answer(extractor.answer))
        self.assertTrue(is_no_comment_answer(extractor.answer))  # complete -> swallowed

    def test_diverging_answer_releases_hold(self):
        extractor = JsonAnswerExtractor()
        extractor.feed('{"answer":"NO problem 这是真回答"}')
        self.assertFalse(may_become_no_comment(extractor.answer))  # diverged -> play it


if __name__ == "__main__":
    unittest.main()
