"""Stage-2 guard for the ONE memory_commit line on the run_turn hot path.

The long-term commit scope now uses ``effective_memory_conversation_id`` (§27①
write-side symmetry with the retrieve node): a galgame turn commits extracted
memories to the caller's ORIGINAL conversation scope (so they stay retrievable
in normal chat); a plain turn (memory_conversation_id unset) commits exactly as
before. The recent-memory append keeps the RAW conversation_id -- that isolation
is the design, not a bug.
"""

import unittest
from types import SimpleNamespace

from spica.adapters.memory.sqlite import scoped_conversation_id
from spica.config.schema import AppConfig, CharacterConfig
from spica.runtime.context import StreamedAnswer, TurnContext, TurnRequest
from spica.runtime.deps import TurnDeps
from spica.runtime.memory_commit import save_stream_memory
from spica.runtime.tools import RegistryToolSet


class _RecordingMemory:
    def __init__(self):
        self.scopes = []

    def commit_turn(self, scope, user_input, answer, meta=None):
        self.scopes.append(scope)
        return {}


class _RecordingRecent:
    def __init__(self):
        self.appended = []

    def append_turn(self, conversation_id, user_input, answer, **kwargs):
        self.appended.append(conversation_id)


def _run(request: TurnRequest):
    memory = _RecordingMemory()
    recent = _RecordingRecent()
    ctx = TurnContext(request)
    ctx.answer = StreamedAnswer(answer="好的")
    deps = TurnDeps(
        config=AppConfig(character=CharacterConfig(character_id="spica", interlocutor_name="麦")),
        llm=None,
        tts=None,
        visual=None,
        memory=memory,
        tools=RegistryToolSet.from_function_table([], {}),
    )  # jobs defaults to InlineJobRunner -> the commit runs synchronously here
    save_stream_memory(ctx, SimpleNamespace(recent_memory=recent), deps)
    return memory, recent


GALGAME_CID = "galgame::limelight::playthrough::default"


class CommitScopeTest(unittest.TestCase):
    def test_galgame_turn_commits_to_origin_scope(self):
        memory, _ = _run(
            TurnRequest(user_input="刚才剧情?", conversation_id=GALGAME_CID, memory_conversation_id="default")
        )
        self.assertEqual(len(memory.scopes), 1)
        scope = memory.scopes[0]
        self.assertEqual(scope.conversation_id, "default")  # the ORIGIN, not galgame::
        self.assertNotEqual(scope.conversation_id, GALGAME_CID)

    def test_plain_turn_scope_byte_identical(self):
        # memory_conversation_id unset -> effective == raw conversation_id: the
        # committed scope triple is exactly what the pre-stage-2 code produced.
        memory, _ = _run(TurnRequest(user_input="你好", conversation_id="default"))
        scope = memory.scopes[0]
        self.assertEqual(
            (scope.character_id, scope.user_id, scope.conversation_id),
            ("spica", "麦", "default"),
        )

    def test_recent_append_uses_character_scoped_galgame_conversation_id(self):
        # Phase 2 behaviour change (PR-declared): the recent bucket key is now
        # {character_id}::{conversation_id}. The galgame isolation this test always
        # pinned is PRESERVED -- the scoped galgame key still carries the galgame
        # namespace and still differs from the scoped origin bucket.
        _, recent = _run(
            TurnRequest(user_input="刚才剧情?", conversation_id=GALGAME_CID, memory_conversation_id="default")
        )
        scoped_galgame = scoped_conversation_id("spica", GALGAME_CID)
        self.assertEqual(recent.appended, [scoped_galgame])
        self.assertNotEqual(scoped_galgame, scoped_conversation_id("spica", "default"))


if __name__ == "__main__":
    unittest.main()
