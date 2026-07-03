"""Phase 0 characterization: recent-memory scoping (OO migration).

(a) pins the isolation guarantee: different conversation_ids never see each
other's recent turns.
(b) pins cross-CHARACTER isolation: written as a strict xfail in Phase 0 (the
recent bucket key was the BARE conversation_id, so two characters sharing a
conversation_id silently shared recent context); Phase 2's MemoryScopeStrategy
({character_id}::{conversation_id} key) turned it green and the xfail marker
was removed -- it now guards the fix.

HARD RULES (migration plan Phase 0 #4): the write path goes through
``save_stream_memory`` (the production write point) and the read path through
``load_recent_context_node`` (the production read point); the recent deque is
never written directly; each role gets a FRESHLY CONSTRUCTED TurnDeps from its
own config (no in-place config mutation, no deps reuse) so the test holds for
both frozen and live scope implementations.
"""

from types import SimpleNamespace

from memory.recent import RecentMemory
from spica.config.schema import AppConfig, CharacterConfig
from spica.runtime.context import StreamedAnswer, TurnContext, TurnRequest
from spica.runtime.deps import TurnDeps
from spica.runtime.memory_commit import save_stream_memory
from spica.runtime.stages import load_recent_context_node
from spica.runtime.tools import RegistryToolSet


class _NoopLongTermMemory:
    """Absorbs the long-term commit_turn so save_stream_memory's background half
    is a clean no-op (this file only characterizes the RECENT half)."""

    def commit_turn(self, scope, user_input, answer, meta=None):
        return {}


def _deps(character_id: str) -> TurnDeps:
    # A fresh TurnDeps per role, built from that role's OWN config object.
    return TurnDeps(
        config=AppConfig(
            character=CharacterConfig(character_id=character_id, interlocutor_name="麦")
        ),
        llm=None,
        tts=None,
        visual=None,
        memory=_NoopLongTermMemory(),
        tools=RegistryToolSet.from_function_table([], {}),
    )  # jobs defaults to InlineJobRunner -> the long-term no-op runs inline


def _write_turn(services, deps, conversation_id: str, user_text: str = "早上好") -> None:
    ctx = TurnContext(
        TurnRequest(
            user_input=user_text,
            conversation_id=conversation_id,
            include_user_time_context=False,
        )
    )
    ctx.answer = StreamedAnswer(answer="嗯，早。")  # non-empty -> the append really happens
    save_stream_memory(ctx, services, deps)


def _read_recent(services, deps, conversation_id: str):
    ctx = TurnContext(TurnRequest(user_input="刚才聊了什么", conversation_id=conversation_id))
    load_recent_context_node(ctx, services, deps)
    return ctx.recent.recent_context if ctx.recent else None


def test_recent_isolated_across_conversation_ids():
    # (a) Today's guarantee: conversation_ids partition recent memory.
    recent = RecentMemory()
    services = SimpleNamespace(recent_memory=recent)
    _write_turn(services, _deps("spica"), "c1")

    assert _read_recent(services, _deps("spica"), "c2") == []
    got = _read_recent(services, _deps("spica"), "c1")
    assert len(got) == 1
    assert got[0]["user_text"] == "早上好"


def test_recent_isolated_across_characters():
    # (b) Cross-character isolation: character A writes, character B reads the
    # SAME conversation_id through freshly constructed deps -- B must see
    # nothing. Was a strict xfail against the bare-conversation_id key; green
    # since Phase 2's MemoryScopeStrategy scoped the bucket by character.
    recent = RecentMemory()
    services = SimpleNamespace(recent_memory=recent)

    deps_a = _deps("spica")  # role A: its own config, its own deps
    _write_turn(services, deps_a, "shared-conversation")

    deps_b = _deps("second-chara")  # role B: RE-constructed deps, different config
    assert _read_recent(services, deps_b, "shared-conversation") == []
