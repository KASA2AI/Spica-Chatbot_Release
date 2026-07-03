"""Phase 0 characterization: recent-memory scoping baseline (OO migration).

(a) pins TODAY's isolation guarantee: different conversation_ids never see each
other's recent turns.
(b) pins TODAY's known hole as a strict xfail: the recent bucket key is the BARE
conversation_id (memory/recent.py keyed by whatever load/save pass through --
stages.py load_recent_context_node / memory_commit.py save_stream_memory), so two
different characters sharing a conversation_id silently share recent context.
Phase 2's MemoryScopeStrategy ({character_id}::{conversation_id} key) turns (b)
green, at which point the xfail marker is removed (by Phase 2, not here).

HARD RULES (migration plan Phase 0 #4): the write path goes through
``save_stream_memory`` (the production write point) and the read path through
``load_recent_context_node`` (the production read point); the recent deque is
never written directly; each role gets a FRESHLY CONSTRUCTED TurnDeps from its
own config (no in-place config mutation, no deps reuse) so the test holds for
both frozen and live scope implementations Phase 2 might choose.
"""

from types import SimpleNamespace

import pytest

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


@pytest.mark.xfail(
    strict=True,
    reason="recent key 未按 character 命名空间隔离；由 Phase 2 的 MemoryScopeStrategy 转绿",
)
def test_recent_isolated_across_characters():
    # (b) Cross-character pollution baseline: character A writes, character B
    # reads the SAME conversation_id through freshly constructed deps. Isolation
    # demands B sees nothing; today's bare-conversation_id key makes B see A's
    # turn, so this fails (xfail) until Phase 2 scopes the key by character.
    recent = RecentMemory()
    services = SimpleNamespace(recent_memory=recent)

    deps_a = _deps("spica")  # role A: its own config, its own deps
    _write_turn(services, deps_a, "shared-conversation")

    deps_b = _deps("second-chara")  # role B: RE-constructed deps, different config
    assert _read_recent(services, deps_b, "shared-conversation") == []
