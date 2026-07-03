"""C6 / N4-memory: save_stream_memory backgrounds ONLY the long-term commit.

- recent_memory append is SYNCHRONOUS -- it runs before save_stream_memory returns
  (so the next turn's recent context has it before this turn's `done`).
- the long-term commit_turn is fire-and-forget via the injected JobRunner: it is
  *submitted*, not run inline; running the submitted job performs the commit with
  the character-namespaced MemoryScope.
- a commit failure lands in ctx.metadata + a WARNING log (review #6: silent loss
  is how memories vanish unnoticed) -- it never escapes to the caller.
"""

import unittest
from dataclasses import replace
from types import SimpleNamespace

from spica.adapters.memory.sqlite import scoped_conversation_id
from spica.config.schema import AppConfig, CharacterConfig, MemoryConfig
from spica.runtime.context import StreamedAnswer, TurnContext, TurnRequest
from spica.runtime.deps import TurnDeps
from spica.runtime.memory_commit import save_stream_memory


class _SpyMemory:
    def __init__(self):
        self.commits = []

    def commit_turn(self, scope, user_text, assistant_text, meta=None):
        self.commits.append((scope, user_text, assistant_text, meta))
        return {"committed": True}


class _BoomMemory:
    def commit_turn(self, *a, **k):
        raise RuntimeError("commit boom")


class _SpyRecent:
    def __init__(self):
        self.appends = []

    def append_turn(self, conversation_id, user_input, answer, **kw):
        self.appends.append((conversation_id, user_input, answer, kw))


class _DeferredJobs:
    """Records submitted jobs without running them, so a test can assert the
    long-term commit is deferred (not run inline on the hot path)."""

    def __init__(self):
        self.submitted = []

    def submit(self, fn):
        self.submitted.append(fn)

    def drain(self, timeout=None):
        return None


def _deps(memory, jobs):
    return TurnDeps(
        config=AppConfig(
            character=CharacterConfig(interlocutor_name="麦", character_id="spica"),
            memory=MemoryConfig(max_long_term_memories=200),
        ),
        llm=None,
        tts=None,
        visual=None,
        memory=memory,
        tools=None,
        jobs=jobs,
    )


def _ctx(user_input, answer):
    ctx = TurnContext(TurnRequest(conversation_id="c1", user_input=user_input))
    ctx.answer = StreamedAnswer(answer=answer)
    return ctx


class SaveStreamMemoryTest(unittest.TestCase):
    def test_recent_is_synchronous_and_long_term_is_submitted(self):
        recent, memory, jobs = _SpyRecent(), _SpyMemory(), _DeferredJobs()
        services = SimpleNamespace(recent_memory=recent)
        ctx = _ctx("你好", "こんにちは。")

        save_stream_memory(ctx, services, _deps(memory, jobs))

        # recent append already happened (synchronous, before `done`)
        self.assertEqual(len(recent.appends), 1)
        # Phase 2: the recent bucket key is character-scoped ({character_id}::{cid}).
        self.assertEqual(
            recent.appends[0][:3],
            (scoped_conversation_id("spica", "c1"), "你好", "こんにちは。"),
        )
        # long-term commit was SUBMITTED, not run inline
        self.assertEqual(len(jobs.submitted), 1)
        self.assertEqual(memory.commits, [])

    def test_submitted_job_commits_with_character_namespaced_scope(self):
        memory, jobs = _SpyMemory(), _DeferredJobs()
        ctx = _ctx("你好", "こんにちは。")
        save_stream_memory(ctx, SimpleNamespace(recent_memory=_SpyRecent()), _deps(memory, jobs))

        jobs.submitted[0]()  # run the backgrounded commit

        self.assertEqual(len(memory.commits), 1)
        scope, user_text, assistant_text, meta = memory.commits[0]
        self.assertEqual(
            (scope.character_id, scope.user_id, scope.conversation_id),
            ("spica", "麦", "c1"),
        )
        self.assertEqual((user_text, assistant_text), ("你好", "こんにちは。"))
        self.assertEqual(meta, {"interlocutor_name": "麦", "max_active_memories": 200})
        self.assertEqual(ctx.metadata.get("committed"), True)

    def test_commit_failure_only_lands_in_metadata(self):
        jobs = _DeferredJobs()
        ctx = _ctx("你好", "hi")
        save_stream_memory(ctx, SimpleNamespace(recent_memory=_SpyRecent()), _deps(_BoomMemory(), jobs))

        jobs.submitted[0]()  # the backgrounded commit raises internally

        self.assertEqual(ctx.metadata.get("memory_error"), "commit boom")

    def test_long_term_failure_logs_a_warning(self):
        # Review #6: a lost memory must leave a trace -- WARNING log AND the
        # original metadata behaviour, never silent.
        jobs = _DeferredJobs()
        ctx = _ctx("你好", "hi")
        save_stream_memory(ctx, SimpleNamespace(recent_memory=_SpyRecent()), _deps(_BoomMemory(), jobs))
        with self.assertLogs("spica.runtime.memory_commit", level="WARNING") as logs:
            jobs.submitted[0]()
        self.assertTrue(any("commit boom" in line for line in logs.output))
        self.assertEqual(ctx.metadata.get("memory_error"), "commit boom")

    def test_recent_failure_logs_a_warning(self):
        class _BoomRecent:
            def append_turn(self, *a, **k):
                raise RuntimeError("recent boom")

        jobs = _DeferredJobs()
        ctx = _ctx("你好", "hi")
        with self.assertLogs("spica.runtime.memory_commit", level="WARNING") as logs:
            save_stream_memory(ctx, SimpleNamespace(recent_memory=_BoomRecent()), _deps(_SpyMemory(), jobs))
        self.assertTrue(any("recent boom" in line for line in logs.output))
        self.assertEqual(ctx.metadata.get("memory_error"), "recent boom")
        self.assertEqual(len(jobs.submitted), 1)  # long-term path still proceeds


if __name__ == "__main__":
    unittest.main()
