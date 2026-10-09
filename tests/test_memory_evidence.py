"""Standalone regressions for durable input, budgets and withdrawal."""
import pytest


from memory.store import SQLiteMemoryStore


from spica.adapters.memory.sqlite import SqliteMemoryAdapter


from spica.ports.memory import MemoryScope


def test_real_turn_retains_input_when_the_model_fails(tmp_path):
    from types import SimpleNamespace
    from memory.recent import RecentMemory
    from spica.config.schema import AppConfig, CharacterConfig
    from spica.core.chat_engine import ChatEngine
    from spica.runtime.services import AgentServices

    class BrokenAPI:
        def create(self, **kwargs):
            raise RuntimeError("injected model outage")

    store = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    memory = SqliteMemoryAdapter(store)
    services = AgentServices(
        llm_client=SimpleNamespace(responses=BrokenAPI()), tts_adapter=None,
        visual_tool=None, memory_store=store, recent_memory=RecentMemory(),
        memory_adapter=memory, config={"model": "test"},
    )
    engine = ChatEngine(services, AppConfig(character=CharacterConfig(
        character_id="spica", interlocutor_name="会变化的显示名",
    )))
    list(engine.stream_voice_runtime("刚才是识别错误，我说的是喜欢。", want_audio=False, evidence_turn_id="failed-turn", input_modality="speech"))
    records = memory.evidence(MemoryScope("spica", "owner", "default"))
    assert [(row["kind"], row["content"]) for row in records] == [
        ("user", "刚才是识别错误，我说的是喜欢。"),
    ]
    assert records[0]["modality"] == "speech"
    engine.record_presentation("failed-turn", outcome="failed")
    receipt = memory.evidence(MemoryScope("spica", "owner", "default"))[-1]
    assert receipt["kind"] == "playback"
    assert receipt["metadata"]["outcome"] == "failed"
    assert receipt["metadata"]["perceived_by_user"] == "unconfirmed"



def test_real_change_keeps_history_but_correction_withdraws_false_support(tmp_path):
    memory = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / "memory.sqlite3"))
    scope = MemoryScope("spica", "owner", "default")
    old = memory.remember(scope, "以前喜欢喝甜咖啡。")
    changed = memory.revise(scope, old, "现在喜欢不加糖的咖啡。", reason="change")
    history = memory.list_memory(scope, include_history=True)
    assert {row["id"]: row["status"] for row in history} == {old: "superseded", changed: "active"}
    corrected = memory.revise(scope, changed, "刚才转写错了，现在喜欢少糖咖啡。", reason="correction")
    assert {row["id"] for row in memory.list_memory(scope, include_history=True)} == {old, corrected}
    correction = memory.list_memory(scope)[0]
    assert correction["revision_reason"] == "correction" and correction["replaces"] == [changed]
    wrong_original = next(row for row in memory.evidence(scope) if row["exclusions"])
    assert "不加糖" not in wrong_original["content"]
    assert wrong_original["exclusions"] == [{"start": 0, "end": len(wrong_original["content"]), "reason": "correction"}]


def test_retention_expires_raw_quotes_but_preserves_independent_long_term_facts(tmp_path):
    import time
    memory = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / "memory.sqlite3"))
    scope = MemoryScope("spica", "owner")
    memory.remember(scope, "喜欢雨天听夏影。")
    assert memory.expire_raw(now=time.time() + 364 * 86400) == 0
    assert memory.expire_raw(now=time.time() + 366 * 86400) == 1
    assert memory.evidence(scope)[0]["content"] == ""
    entry = memory.list_memory(scope)[0]
    assert entry["content"] == "喜欢雨天听夏影。"
    assert "quote" not in entry["sources"][0]
    assert entry["sources"][0]["availability"] == "expired"
    memory.forget(scope, entry["id"])
    assert memory.list_memory(scope) == []


def test_deleted_or_corrected_raw_cannot_reenter_working_context_after_restart(tmp_path):
    path = tmp_path / "memory.sqlite3"
    memory = SqliteMemoryAdapter(SQLiteMemoryStore(path))
    scope = MemoryScope("spica", "owner", "default")
    source_id = memory.record_evidence(scope, event_id="r1:user", kind="user", source="desktop",
                                     content="我不喜欢咖啡。")
    snapshot = memory.journal.snapshot(scope)
    memory.journal.apply(scope, snapshot, [{"kind": "preference", "key": "coffee", "text": "不喜欢咖啡。",
        "origin": "direct", "sources": [{"id": source_id, "quote": "我不喜欢咖啡。"}],
        "replaces": [], "revision_reason": None}], notes=[{"text": "本人说不喜欢咖啡。",
        "sources": [{"id": source_id, "quote": "我不喜欢咖啡。"}]}])
    old = memory.list_memory(scope)[0]["id"]
    new = memory.revise(scope, old, "转写错误，其实喜欢咖啡。")
    assert "不喜欢咖啡" not in str(memory.working_context(scope, "刚才咖啡"))
    memory.forget(scope, new)
    restored = SqliteMemoryAdapter(SQLiteMemoryStore(path))
    assert "咖啡" not in str(restored.working_context(scope, "刚才咖啡"))
    assert restored.list_memory(scope) == []


def test_context_reset_keeps_facts_and_tools_keep_real_call_pairs(tmp_path):
    memory = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / "memory.sqlite3"))
    scope = MemoryScope("spica", "owner", "default")
    memory.remember(scope, "喜欢雨天。")
    memory.record_evidence(scope, event_id="r1:user", kind="user", content="查询提醒。", source="desktop")
    memory.record_evidence(scope, event_id="r1:tool:provider-123", kind="tool", content='{"reminders": []}',
                           source="list_reminders", modality="tool", metadata={"turn_id": "r1", "model_call_id": "provider-123", "arguments": "{}", "batch": 1})
    messages = memory.working_context(scope, "好")["messages"]
    assert [row["role"] for row in messages] == ["user", "assistant", "tool"]
    assert messages[1]["tool_calls"][0]["id"] == messages[2]["tool_call_id"] == "provider-123"
    memory.clear_context(scope)
    assert memory.working_context(scope, "刚才提醒")["messages"] == []
    assert memory.list_memory(scope)[0]["content"] == "喜欢雨天。"


def test_late_organizer_cannot_publish_after_deletion_or_shutdown(tmp_path):
    import json
    import threading
    from spica.ports.model import BoundModel
    from spica.config.schema import MemoryConfig

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    class LateAPI:
        supports_bounded_completion = True
        def complete(self, prompt, *, model, options=None):
            snapshot = json.loads(prompt[-1]["content"])
            entered.set()
            assert release.wait(2)
            finished.set()
            return json.dumps({"schema_version":"memory.consolidation.v1", "covered_ids":snapshot["covered_ids"],
                               "entries":[], "no_new_episode_reason":"已有人工作成的同源事实"})
    path = tmp_path / "memory.sqlite3"
    memory = SqliteMemoryAdapter(SQLiteMemoryStore(path), memory_config=MemoryConfig(consolidation_enabled=True, consolidation_min_user_turns=0, consolidation_min_tokens=0))
    scope = MemoryScope("spica", "owner")
    entry = memory.remember(scope, "喜欢夏影。")
    memory.start_maintenance(BoundModel(LateAPI(), "organizer"), idle_seconds=0)
    try:
        assert entered.wait(5)
        memory.forget(scope, entry)
        assert memory.shutdown(timeout=.01)  # cannot hold the core writer until HTTP finishes
        release.set()
        assert finished.wait(1)
    finally:
        release.set()
        memory.shutdown(timeout=2)
    restored = SqliteMemoryAdapter(SQLiteMemoryStore(path))
    assert restored.list_memory(scope) == []
    assert restored.maintenance_status(scope)["processed_id"] == 0
    assert restored.maintenance_status(scope)["pending"] == 1
    assert "夏影" not in str(restored.working_context(scope, "夏影"))


def test_withdrawn_replay_cannot_reach_model_after_restart(tmp_path):
    from spica.config.schema import AppConfig
    from spica.runtime.context import TurnContext, TurnRequest
    from spica.runtime.memory_commit import record_turn_input
    from types import SimpleNamespace

    path = tmp_path / 'memory.sqlite3'
    memory = SqliteMemoryAdapter(SQLiteMemoryStore(path))
    config = AppConfig()
    request = TurnRequest(user_input='喜欢雨天听夏影。', evidence_turn_id='replay')
    context = TurnContext(request=request, user_input=request.user_input)
    deps = SimpleNamespace(memory=memory, config=config, personal_owner_id='owner')
    record_turn_input(context, deps)
    from spica.runtime.scope import MemoryScopeStrategy
    scope = MemoryScopeStrategy(config).evidence_scope(request, 'owner')
    snapshot = memory.journal.snapshot(scope)
    memory.journal.apply(scope, snapshot, [dict(kind='preference',key='rain',text=request.user_input,
        origin='direct',sources=[{'id':context.metadata['input_evidence_id'],'quote':request.user_input}],
        replaces=[],revision_reason=None)])
    memory.forget(scope,memory.list_memory(scope)[0]['id'])
    deps.memory = SqliteMemoryAdapter(SQLiteMemoryStore(path))
    replay = TurnContext(request=request, user_input=request.user_input)
    record_turn_input(replay,deps)
    assert replay.error.code == 'STALE_INPUT'
    assert not replay.metadata.get('input_evidence_id')


def test_total_prompt_budget_preserves_current_business_result_and_rejects_oversize(tmp_path):
    from spica.config.schema import AppConfig, CharacterConfig, MemoryConfig
    from spica.runtime.context import TurnContext, TurnRequest
    from spica.runtime.deps import TurnDeps
    from spica.runtime.stages import load_recent_context_node, build_prompt_node
    from spica.runtime.tools import RegistryToolSet
    memory = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / 'prompt-budget.sqlite3'))
    scope = MemoryScope('spica', 'owner', 'default')
    for i in range(8):
        memory.record_evidence(scope,event_id=f'old-{i}:user',kind='user',source='desktop',content='历史讨论。' * 300)
    config = AppConfig(character=CharacterConfig(profile_override='直率而温柔。'), memory=MemoryConfig(context_token_budget=4000))
    deps = TurnDeps(config=config,llm=None,tts=None,visual=None,memory=memory,tools=RegistryToolSet.from_function_table([],{}))
    ctx = TurnContext(TurnRequest(user_input='那集今天先不看。'))
    ctx.metadata['media_reply_result'] = {'state':'deferred','until':'today','task_id':'actual-current-task'}
    load_recent_context_node(ctx,None,deps)
    build_prompt_node(ctx,None,deps)
    assert ctx.error is None
    assert ctx.metadata['prompt_estimated_tokens'] <= 4000
    assert any('actual-current-task' in m['content'] for m in ctx.prompt.prompt_input)
    config.memory.context_token_budget = 512
    oversized = TurnContext(TurnRequest(user_input='完整且不能被默默截断的当前输入。' * 120))
    load_recent_context_node(oversized,None,deps)
    build_prompt_node(oversized,None,deps)
    assert oversized.error and oversized.error.code == 'CONTEXT_BUDGET_EXCEEDED'


def test_budget_releases_optional_character_material_before_recent_dialogue(tmp_path):
    import json
    from spica.config.schema import AppConfig, CharacterConfig
    from spica.runtime.context import TurnContext, TurnRequest
    from spica.runtime.deps import TurnDeps
    from spica.runtime.stages import load_recent_context_node, build_prompt_node
    from spica.runtime.tools import RegistryToolSet
    memory = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / 'character-budget.sqlite3'))
    scope = MemoryScope('spica', 'owner', 'default')
    memory.record_evidence(scope, event_id='last:user', kind='user', source='desktop', content='我今天请假了，不去公司。')
    config = AppConfig(character=CharacterConfig(character_profile='简洁的人物核心。'))
    deps = TurnDeps(config, None, None, None, memory, RegistryToolSet.from_function_table([], {}))
    request = TurnRequest(user_input='原作先不聊，明早再说。')
    baseline = TurnContext(request, user_input=request.user_input)
    load_recent_context_node(baseline, None, deps)
    build_prompt_node(baseline, None, deps)
    assert '请假' in str(baseline.prompt.prompt_input)
    config.memory.context_token_budget = baseline.metadata['prompt_estimated_tokens']
    config.character.character_profile = '# SPICA_RUNTIME_MATERIAL v1\n' + json.dumps(dict(
        core='简洁的人物核心。', expressions=[], background=[dict(triggers=['原作'], text='可选剧情。' * 80)]))
    ctx = TurnContext(request, user_input=request.user_input)
    load_recent_context_node(ctx, None, deps)
    build_prompt_node(ctx, None, deps)
    assert ctx.error is None
    assert '请假' in str(ctx.prompt.prompt_input) and '可选剧情' not in str(ctx.prompt.prompt_input)
    assert ctx.metadata['context_evidence_ids'] == baseline.metadata['context_evidence_ids']



def test_shutdown_during_reservation_cannot_start_a_new_model_request(tmp_path, monkeypatch):
    import threading
    from unittest.mock import Mock
    from memory.consolidation import MemoryConsolidation
    from spica.ports.model import BoundModel
    memory = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / 'reservation.sqlite3'))
    scope = MemoryScope('spica', 'owner')
    memory.record_evidence(scope, event_id='one:user', kind='user', source='desktop', content='保留这条输入。')
    journal = memory.journal
    entered, release = threading.Event(), threading.Event()
    reserve = journal.reserve_processing
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return reserve(*args, **kwargs)
    monkeypatch.setattr(journal, 'reserve_processing', delayed)
    model = Mock()
    model.supports_bounded_completion = True
    model.complete.side_effect = AssertionError('no paid request after clean shutdown')
    worker = MemoryConsolidation(journal, BoundModel(model, 'fake'), idle_seconds=0)
    try:
        assert entered.wait(3)
        assert worker.shutdown(.05)
        release.set()
        worker._thread.join(2)
        assert not worker._thread.is_alive()
        model.complete.assert_not_called()
    finally:
        release.set()
        worker.shutdown(1)
