"""Memory scheduling against durable records, with a local model double."""
import json
import threading
import time
import pytest

from memory.store import SQLiteMemoryStore
from spica.adapters.memory.sqlite import SqliteMemoryAdapter
from spica.config.schema import MemoryConfig
from spica.ports.memory import MemoryScope
from spica.ports.model import BoundModel

SCOPE = MemoryScope('sana', 'owner', 'default')


class Model:
    supports_bounded_completion = True
    def __init__(self):
        self.started = threading.Event()
        self.batches = []

    def complete(self, prompt, *, model, options=None):
        payload = json.loads(prompt[-1]['content'])
        self.started.set()
        if 'candidate' in payload:
            return json.dumps(dict(schema_version='memory.support.v1', supported=True, reason='无新事实'))
        self.batches.append(payload['covered_ids'])
        return json.dumps(dict(schema_version='memory.consolidation.v1', covered_ids=payload['covered_ids'],
            entries=[], working_summary=[], no_new_episode_reason='普通交谈'))


def adapter(path, *, turns=2, tokens=100000, budget=12000):
    return SqliteMemoryAdapter(SQLiteMemoryStore(path), memory_config=MemoryConfig(
        consolidation_enabled=True,
        consolidation_min_user_turns=turns, consolidation_min_tokens=tokens,
        context_token_budget=budget))


def add(memory, name, *, kind='user', content='普通交谈。'):
    return memory.record_evidence(SCOPE, event_id=name + ':' + kind, kind=kind,
        source='desktop', content=content, metadata={'turn_id': name})


def test_small_exchange_does_not_trigger_on_idle_or_shutdown_and_accumulates_across_restart(tmp_path):
    path = tmp_path / 'memory.sqlite3'
    memory, first = adapter(path), Model()
    memory.start_maintenance(BoundModel(first, 'test'), idle_seconds=.01)
    try:
        add(memory, 'one')
        add(memory, 'one')  # Transport replay must not count as another turn.
        add(memory, 'one', kind='assistant_generated')
        add(memory, 'one', kind='playback')
        assert not first.started.wait(.15)
        assert memory.maintenance_status(SCOPE)['pending'] == 3
    finally:
        assert memory.shutdown(1)
    assert not first.batches
    restored, second = adapter(path), Model()
    restored.start_maintenance(BoundModel(second, 'test'), idle_seconds=.01)
    try:
        assert not second.started.wait(.15), 'startup must respect the same accumulation threshold'
        add(restored, 'two')
        assert restored.wait_for_maintenance(timeout=2)
        assert second.batches == [[1, 2, 3, 4]]
        second.started.clear()
        add(restored, 'three')
        assert not second.started.wait(.15), 'an old admitted batch cannot authorize later tiny batches'
    finally:
        restored.shutdown(1)


def test_reading_with_no_prompt_space_does_not_spend_a_small_pending_batch(tmp_path):
    memory, model = adapter(tmp_path / 'read-only.sqlite3', turns=10), Model()
    memory.start_maintenance(BoundModel(model, 'test'), idle_seconds=.01)
    try:
        add(memory, 'only-one', content='今天聊了一句。')
        assert memory.working_context(SCOPE, '晚安', token_budget=0)['messages'] == []
        assert not model.started.wait(.15), 'prompt space cannot bypass the accumulation threshold'
    finally:
        memory.shutdown(1)


def test_disabled_consolidation_still_runs_retention_without_model_calls(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import memory.consolidation as maintenance

    elapsed = [0.]
    real_time, real_monotonic = time.time, time.monotonic
    monkeypatch.setattr(maintenance, 'time', SimpleNamespace(
        time=lambda: real_time()+elapsed[0], monotonic=lambda: real_monotonic()+elapsed[0]))
    memory = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path/'disabled-retention.sqlite3'),
        memory_config=MemoryConfig(consolidation_enabled=False, raw_retention_days=1))
    model = Model()
    memory.expire_raw()
    memory.start_maintenance(BoundModel(model, 'test'), idle_seconds=0)
    try:
        fact = memory.remember(SCOPE, '本人喜欢雨天听音乐。')
        assert memory.evidence(SCOPE)[0]['content']
        elapsed[0] = 3*86400
        # Wake the real lifecycle loop after advancing its clock; this is a
        # normal notification, not a direct call to the expiry implementation.
        memory.run_maintenance(SCOPE, 'idle')
        deadline = real_monotonic()+1
        while memory.evidence(SCOPE)[0]['status'] != 'expired' and real_monotonic() < deadline:
            time.sleep(.01)
        assert memory.evidence(SCOPE)[0]['status'] == 'expired'
        assert not memory.evidence(SCOPE)[0]['content']
        entry = next(row for row in memory.list_memory(SCOPE) if row['id'] == fact)
        assert entry['content'] and 'quote' not in entry['sources'][0]
        memory.run_maintenance(SCOPE, 'resume')
        assert not model.started.wait(.05)
        assert memory.maintenance_status(SCOPE)['auto_enabled'] is False
    finally:
        assert memory.shutdown(1)


def test_failed_material_stays_held_across_restart_while_new_material_can_finish(tmp_path):
    class Broken(Model):
        def complete(self, prompt, *, model, options=None):
            self.batches.append(json.loads(prompt[-1]['content'])['covered_ids'])
            raise OSError('temporary provider outage')
    path = tmp_path / 'held.sqlite3'
    memory, failed = adapter(path, turns=1), Broken()
    old = add(memory, 'old')
    memory.start_maintenance(BoundModel(failed, 'test'), idle_seconds=0)
    try:
        deadline = time.monotonic() + 4
        while not memory.maintenance_status(SCOPE).get('held') and time.monotonic() < deadline:
            time.sleep(.01)
        assert memory.maintenance_status(SCOPE).get('held') == 1
        assert failed.batches == [[old], [old], [old]]
    finally:
        memory.shutdown(1)
    restored, good = adapter(path, turns=1), Model()
    restored.start_maintenance(BoundModel(good, 'test'), idle_seconds=0)
    try:
        assert not good.started.wait(.1), 'restart cannot replenish the old material budget'
        fresh = add(restored, 'new')
        assert good.started.wait(2)
        deadline = time.monotonic() + 2
        while restored.maintenance_status(SCOPE)['pending'] > 1 and time.monotonic() < deadline:
            time.sleep(.01)
        assert good.batches == [[fresh]]
        assert restored.maintenance_status(SCOPE)['pending'] == 1
        restored.run_maintenance(SCOPE, 'resume')
        assert restored.wait_for_maintenance(timeout=2)
        assert good.batches == [[fresh], [old]]
    finally:
        restored.shutdown(1)


def test_oversized_first_exchange_is_held_without_call_and_later_exchange_can_finish(tmp_path):
    memory, model = adapter(tmp_path / 'oversized.sqlite3', turns=1), Model()
    old = add(memory, 'long', content='不能截掉这条完整原文。' * 3000)
    memory.start_maintenance(BoundModel(model, 'test'), idle_seconds=0)
    try:
        memory.wait_for_maintenance(timeout=.5)
        assert model.batches == []
        assert memory.maintenance_status(SCOPE)['held'] == 1
        fresh = add(memory, 'small')
        assert memory.wait_for_maintenance(timeout=2)
        assert model.batches == [[fresh]]
        assert memory.maintenance_status(SCOPE)['held'] == 1
        assert any(row['id'] == old and len(row['content']) > 12000 for row in memory.evidence(SCOPE))
    finally:
        memory.shutdown(1)


def test_oversized_exchange_with_interleaved_receipt_does_not_hold_independent_turn(tmp_path):
    memory, model = adapter(tmp_path / 'interleaved.sqlite3', turns=1), Model()
    add(memory, 'long', content='不能截掉完整原文。' * 3000)
    fresh = add(memory, 'small')
    add(memory, 'long', kind='playback')
    memory.start_maintenance(BoundModel(model, 'test'), idle_seconds=0)
    try:
        memory.wait_for_maintenance(timeout=2)
        assert model.batches == [[fresh]]
        assert memory.maintenance_status(SCOPE)['held'] == 2
    finally:
        memory.shutdown(1)


def test_provider_configuration_failure_pauses_new_batches_until_explicit_resume(tmp_path):
    class Unauthorized(Exception):
        status_code = 401
    class Provider(Model):
        denied = True
        requests = 0
        def complete(self, prompt, *, model, options=None):
            self.requests += 1
            if self.denied:
                raise Unauthorized('private response body must not be persisted')
            return super().complete(prompt, model=model, options=options)
    memory, provider = adapter(tmp_path / 'provider.sqlite3', turns=1), Provider()
    add(memory, 'one')
    memory.start_maintenance(BoundModel(provider, 'test'), idle_seconds=0)
    try:
        deadline = time.monotonic() + 1
        while not memory.maintenance_status(SCOPE).get('provider_hold') and time.monotonic() < deadline:
            time.sleep(.01)
        assert memory.maintenance_status(SCOPE)['provider_hold']
        add(memory, 'two')
        time.sleep(.1)
        assert provider.requests == 1
        provider.denied = False
        memory.run_maintenance(SCOPE, 'resume')
        assert memory.wait_for_maintenance(timeout=2)
        assert memory.maintenance_status(SCOPE)['pending'] == 0
    finally:
        memory.shutdown(1)


def test_sparse_user_material_wakes_scheduler_at_its_deadline_without_another_message(tmp_path):
    memory, model = adapter(tmp_path / 'sparse.sqlite3', turns=10), Model()
    memory.config.consolidation_sparse_seconds = .15
    add(memory, 'one')
    memory.start_maintenance(BoundModel(model, 'test'), idle_seconds=.01)
    try:
        assert not model.started.wait(.05)
        assert model.started.wait(.7), 'sparse material must not depend on another notification'
        assert memory.wait_for_maintenance(timeout=1)
    finally:
        memory.shutdown(1)


def test_disabling_automatic_maintenance_keeps_raw_storage_and_queries(tmp_path):
    memory, model = adapter(tmp_path / 'disabled.sqlite3', turns=1), Model()
    memory.config.consolidation_enabled = False
    memory.start_maintenance(BoundModel(model, 'test'), idle_seconds=0)
    original = add(memory, 'one')
    assert not model.started.wait(.05)
    assert any(row['id'] == original for row in memory.evidence(SCOPE))
    assert memory.maintenance_status(SCOPE)['pending'] == 1


def test_expired_held_original_stops_being_schedulable_without_erasing_spent_attempts(tmp_path):
    memory = adapter(tmp_path / 'expired-hold.sqlite3')
    add(memory, 'one')
    snapshot = memory.journal.snapshot(SCOPE)
    receipt = memory.journal.reserve_processing(SCOPE, snapshot)
    memory.journal.finish_processing(SCOPE, receipt, reason='input_budget', pause=True)
    assert memory.maintenance_status(SCOPE)['held'] == 1
    memory.journal.expire_raw(time.time()+1)
    status = memory.maintenance_status(SCOPE)
    assert status['held'] == 0
    assert status['schedulable'] == 0
    assert status['total_attempts'] == 1
    memory.journal.resume_processing(SCOPE)
    assert memory.journal.snapshot(SCOPE) is None


def test_temporary_verifier_failure_reuses_valid_candidate_within_attempt_limit(tmp_path):
    class VerifierOutage(Model):
        verifications = 0
        def complete(self, prompt, *, model, options=None):
            if 'candidate' in json.loads(prompt[-1]['content']):
                self.verifications += 1
                if self.verifications == 1:
                    raise OSError('temporary verification outage')
            return super().complete(prompt, model=model, options=options)
    memory, provider = adapter(tmp_path / 'reuse.sqlite3', turns=1), VerifierOutage()
    original = add(memory, 'one')
    memory.start_maintenance(BoundModel(provider, 'test'), idle_seconds=0)
    try:
        assert memory.wait_for_maintenance(timeout=3)
        assert provider.batches == [[original]], 'paid extraction should not repeat for a transport-only verification failure'
        assert provider.verifications == 2
        assert memory.maintenance_status(SCOPE)['total_attempts'] == 2
    finally:
        memory.shutdown(1)


def test_rate_limit_retry_honors_provider_wait_and_remains_bounded(tmp_path):
    from types import SimpleNamespace
    class Limited(Exception):
        status_code = 429
        response = SimpleNamespace(headers={'retry-after': '2'})
    class Provider(Model):
        times = []
        def complete(self, prompt, *, model, options=None):
            self.times.append(time.monotonic())
            if len(self.times) == 1:
                raise Limited()
            return super().complete(prompt, model=model, options=options)
    memory, provider = adapter(tmp_path / 'retry-after.sqlite3', turns=1), Provider()
    add(memory, 'one')
    memory.start_maintenance(BoundModel(provider, 'test'), idle_seconds=0)
    try:
        assert memory.wait_for_maintenance(timeout=4)
        assert len(provider.times) == 3
        assert provider.times[1]-provider.times[0] >= 1.9
        assert memory.maintenance_status(SCOPE)['total_attempts'] == 2
    finally:
        memory.shutdown(1)


def test_large_new_material_triggers_without_enough_user_turns(tmp_path):
    memory, model = adapter(tmp_path / 'memory.sqlite3', turns=10, tokens=100), Model()
    memory.start_maintenance(BoundModel(model, 'test'), idle_seconds=.01)
    try:
        add(memory, 'small', kind='system_event', content='叫醒。')
        assert not model.started.wait(.15)
        add(memory, 'large', kind='system_event', content='中文材料' * 30)
        assert memory.wait_for_maintenance(timeout=2)
        assert model.batches == [[1, 2]]
    finally:
        memory.shutdown(1)


def test_admitted_batch_drains_its_remainder_below_threshold(tmp_path):
    memory, model = adapter(tmp_path / 'memory.sqlite3', turns=3, budget=512), Model()
    for index in range(3):
        add(memory, str(index), content='一段完整的对话。' * 20)
    memory.start_maintenance(BoundModel(model, 'test'), idle_seconds=0)
    try:
        assert memory.wait_for_maintenance(timeout=2)
        assert model.batches == [[1], [2], [3]]
    finally:
        memory.shutdown(1)


@pytest.mark.parametrize('during_extract', [False, True])
def test_late_receipt_retains_exchange_admission_without_admitting_new_user(tmp_path, during_extract):
    path = tmp_path/'late-receipt.sqlite3'
    memory = adapter(path, turns=10, tokens=4000)
    original = [add(memory, str(index)) for index in range(10)]

    class LateReceipt(Model):
        def complete(self, prompt, *, model, options=None):
            payload = json.loads(prompt[-1]['content'])
            if during_extract and not self.batches and 'candidate' not in payload:
                add(memory, '9', kind='playback')
            return super().complete(prompt, model=model, options=options)

    first = LateReceipt()
    memory.start_maintenance(BoundModel(first, 'test'), idle_seconds=0)
    try:
        assert memory.wait_for_maintenance(timeout=2)
        assert first.batches == [original] + ([[11]] if during_extract else [])
    finally:
        assert memory.shutdown(1)

    restored, second = adapter(path, turns=10, tokens=4000), Model()
    receipt = add(restored, '9', kind='delivery')
    fresh = add(restored, 'independent')
    restored.start_maintenance(BoundModel(second, 'test'), idle_seconds=0)
    try:
        assert second.started.wait(1), 'late receipt of an admitted exchange must drain after restart'
        deadline = time.monotonic()+1
        while restored.maintenance_status(SCOPE)['pending'] > 1 and time.monotonic() < deadline:
            time.sleep(.01)
        assert second.batches == [[receipt]]
        assert restored.maintenance_status(SCOPE)['total_attempts'] == 11 + int(during_extract)
        assert any(row['id'] == fresh and not row['organized'] for row in restored.evidence(SCOPE))
    finally:
        assert restored.shutdown(1)


def test_frozen_batch_does_not_count_toward_new_admission_after_restart(tmp_path):
    path = tmp_path/'frozen-admission.sqlite3'
    memory = adapter(path, turns=10)
    old = [add(memory, str(index)) for index in range(10)]
    memory.journal.admit_processing(SCOPE, set(memory.journal.pending_work(SCOPE)['turns']))
    fresh = add(memory, 'fresh')
    restored, model = adapter(path, turns=10), Model()
    restored.start_maintenance(BoundModel(model, 'test'), idle_seconds=0)
    try:
        assert model.started.wait(1)
        deadline = time.monotonic()+1
        while restored.maintenance_status(SCOPE)['pending'] > 1 and time.monotonic() < deadline:
            time.sleep(.01)
        assert model.batches == [old]
        assert any(row['id'] == fresh and not row['organized'] for row in restored.evidence(SCOPE))
    finally:
        restored.shutdown(1)


def test_startup_does_not_skip_the_remaining_quiet_window(tmp_path):
    memory, model = adapter(tmp_path / 'memory.sqlite3'), Model()
    add(memory, 'one')
    add(memory, 'two')
    memory.start_maintenance(BoundModel(model, 'test'), idle_seconds=.5)
    try:
        assert not model.started.wait(.1)
        assert memory.wait_for_maintenance(timeout=2)
    finally:
        memory.shutdown(1)


@pytest.mark.parametrize('correction', ['纠正刚才的说法。', '不对，我喝咖啡从不加糖。',
                                       '違います、コーヒーに砂糖は入れません。'])
def test_correction_bypasses_accumulation_and_ordinary_receipt_cannot_postpone_it(tmp_path, correction):
    memory, model = adapter(tmp_path / 'memory.sqlite3', turns=10), Model()
    memory.start_maintenance(BoundModel(model, 'test'), idle_seconds=60)
    try:
        # Deliver both notifications before the scheduler can consume either.
        with memory._maintenance._condition:
            add(memory, 'correction', content=correction)
            add(memory, 'correction', kind='playback')
        assert model.started.wait(1)
        assert memory.wait_for_maintenance(timeout=2)
        assert model.batches == [[1, 2]]
    finally:
        memory.shutdown(1)


def test_short_local_correction_does_not_reopen_all_organized_ancestors(tmp_path):
    from spica.config.schema import AppConfig
    from spica.runtime.context import TurnContext, TurnRequest
    from spica.runtime.deps import TurnDeps
    from spica.runtime.memory_commit import record_turn_input, record_turn_result
    from spica.runtime.stages import load_recent_context_node
    from spica.runtime.tools import RegistryToolSet

    memory, model = adapter(tmp_path/'correction-ancestors.sqlite3', turns=10), Model()
    config = AppConfig()
    config.character.character_id = SCOPE.character_id
    deps = TurnDeps(config, None, None, None, memory,
        RegistryToolSet.from_function_table([], {}), personal_owner_id=SCOPE.user_id)
    for index in range(50):
        text = f'排练第{index}段台词：' + '春风经过窗台，月光慢慢照进屋子，读完这一段再继续。' * 12
        ctx = TurnContext(TurnRequest(text, evidence_turn_id=f'rehearsal-{index}'), user_input=text)
        record_turn_input(ctx, deps)
        load_recent_context_node(ctx, None, deps)
        record_turn_result(ctx, deps, kind='assistant_generated', source='model',
            event_suffix='assistant-generated', content='这一段可以，继续。')
        assert memory.journal.apply(SCOPE, memory.journal.snapshot(SCOPE), [], notes=[])
    assert memory.maintenance_status(SCOPE)['pending'] == 0
    correction = add(memory, 'correct', content='不对，最后一句是雨，不是风。')
    memory.start_maintenance(BoundModel(model, 'test'), idle_seconds=0)
    try:
        assert model.started.wait(1), 'old organized context must not exhaust this correction budget'
        assert memory.wait_for_maintenance(timeout=2)
        assert model.batches == [[correction]]
        assert memory.maintenance_status(SCOPE)['held'] == 0
    finally:
        memory.shutdown(1)


@pytest.mark.parametrize('kind,content,metadata', [
    ('environment', '叫醒已结束。', {'actor':'business','event_binding':dict(
        kind='home.wake', event_id='morning', revision=2, phase='completed', facts=[])}),
    ('user', '纠正刚才的说法。', {}),
])
def test_replaying_held_priority_source_cannot_admit_unrelated_small_batch(tmp_path, kind, content, metadata):
    memory, model = adapter(tmp_path/'priority-replay.sqlite3', turns=10), Model()
    event = dict(event_id='old:priority', kind=kind, source='home.wake' if kind == 'environment' else 'desktop',
        content=content, metadata=metadata)
    old = memory.record_evidence(SCOPE, **event)
    memory.journal.hold_processing(SCOPE, memory.journal.snapshot(SCOPE), 'attempts_exhausted')
    memory.start_maintenance(BoundModel(model, 'test'), idle_seconds=.01)
    try:
        add(memory, 'fresh')
        assert not model.started.wait(.05)
        assert memory.record_evidence(SCOPE, **event) == old
        assert not model.started.wait(.2)
        assert memory.maintenance_status(SCOPE)['schedulable'] == 1
    finally:
        memory.shutdown(1)


def test_interrupted_terminal_resumes_its_original_coverage_after_restart(tmp_path):
    class Blocking(Model):
        def __init__(self):
            super().__init__()
            self.release = threading.Event()
        def complete(self, prompt, *, model, options=None):
            self.started.set()
            self.release.wait(2)
            return super().complete(prompt, model=model, options=options)
    path = tmp_path/'terminal-restart.sqlite3'
    memory, first = adapter(path, turns=10), Blocking()
    memory.start_maintenance(BoundModel(first, 'test'), idle_seconds=60)
    terminal = memory.record_evidence(SCOPE, event_id='morning:terminal', kind='environment', source='home.wake',
        content='叫醒已结束。', metadata={'actor':'business','event_binding':dict(
            kind='home.wake', event_id='morning', revision=2, phase='completed', facts=[])})
    try:
        assert first.started.wait(1)
        memory.shutdown(.01)
    finally:
        first.release.set()
        memory.shutdown(1)
    restored, second = adapter(path, turns=10), Model()
    fresh = add(restored, 'later')
    restored.start_maintenance(BoundModel(second, 'test'), idle_seconds=60)
    try:
        assert second.started.wait(1)
        deadline = time.monotonic() + 1
        while restored.maintenance_status(SCOPE)['pending'] > 1 and time.monotonic() < deadline:
            time.sleep(.01)
        assert second.batches == [[terminal]]
        assert restored.maintenance_status(SCOPE)['pending'] == 1
        assert restored.maintenance_status(SCOPE)['total_attempts'] == 2
        assert any(row['id'] == fresh and not row['organized'] for row in restored.evidence(SCOPE))
    finally:
        restored.shutdown(1)


def test_late_receipt_does_not_admit_interleaved_new_exchange_after_restart(tmp_path):
    path = tmp_path/'interleaved-admission.sqlite3'
    memory = adapter(path, turns=10)
    old = memory.commit_turn(SCOPE, '纠正一下，这只是测试。', '明白。',
        {'evidence_turn_id':'old'})['memory_evidence_ids']
    assert memory.journal.reserve_processing(SCOPE, memory.journal.snapshot(SCOPE))
    fresh = add(memory, 'fresh')
    receipt = add(memory, 'old', kind='playback', content='播放完成。')
    restored, model = adapter(path, turns=10), Model()
    restored.start_maintenance(BoundModel(model, 'test'), idle_seconds=60)
    try:
        assert model.started.wait(1)
        deadline = time.monotonic()+1
        while restored.maintenance_status(SCOPE)['pending'] > 1 and time.monotonic() < deadline:
            time.sleep(.01)
        assert model.batches == [[*old, receipt]]
        assert any(row['id'] == fresh and not row['organized'] for row in restored.evidence(SCOPE))
    finally:
        restored.shutdown(1)


def test_failed_settlement_survives_sqlite_lock_and_resumes_without_losing_attempt(tmp_path):
    from contextlib import closing
    class Outage(Model):
        def __init__(self):
            super().__init__()
            self.release, self.calls = threading.Event(), 0
        def complete(self, prompt, *, model, options=None):
            self.calls += 1
            if self.calls == 1:
                self.started.set()
                self.release.wait(2)
                raise OSError('temporary provider outage')
            return super().complete(prompt, model=model, options=options)
    memory, provider = adapter(tmp_path/'settlement-lock.sqlite3', turns=1), Outage()
    original = add(memory, 'first')
    memory.start_maintenance(BoundModel(provider, 'test'), idle_seconds=0)
    try:
        assert provider.started.wait(1)
        with closing(memory.store._connect()) as locked:
            locked.execute('BEGIN IMMEDIATE')
            provider.release.set()
            try:
                deadline = time.monotonic() + 7
                while time.monotonic() < deadline:
                    error = memory.maintenance_status(SCOPE)['error'] or ''
                    if 'settlement' in error:
                        break
                    time.sleep(.01)
                assert 'settlement' in error, 'report the failed durable settlement while it is retried'
                assert provider.calls == 1, 'do not call the provider before the old attempt is settled'
            finally:
                locked.rollback()
        assert memory.wait_for_maintenance(timeout=3)
        assert provider.batches == [[original]]
        assert memory.maintenance_status(SCOPE)['total_attempts'] == 2
        assert memory.maintenance_status(SCOPE)['error'] is None
    finally:
        provider.release.set()
        memory.shutdown(1)
