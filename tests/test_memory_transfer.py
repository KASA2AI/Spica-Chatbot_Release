from contextlib import closing

import pytest

from memory.store import SQLiteMemoryStore
from memory.transfer import export_archive, merge_archive, prepare_restore
from spica.adapters.memory.sqlite import SqliteMemoryAdapter
from spica.ports.memory import MemoryScope


@pytest.mark.parametrize('archive_character', [None, 'spica'])
def test_old_archive_cannot_restore_a_dispute_resolved_by_verified_processing(tmp_path, archive_character):
    from spica.config.schema import MemoryConfig

    config = MemoryConfig(shared_fact_characters=('spica', 'sana'), consolidation_enabled=False)
    spica, sana = MemoryScope('spica', 'owner'), MemoryScope('sana', 'owner')
    source = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path/'resolved-source.sqlite3'), memory_config=config)
    fact = source.remember(spica, '我喜欢喝红茶。')
    assert source.journal.apply(spica, source.journal.snapshot(spica), [], notes=[])
    source.record_evidence(sana, event_id='reaffirm:user', kind='user', source='desktop', content='我喜欢喝红茶。')
    assert not source.retrieve(spica, '我喜欢喝红茶吗？', 5)
    old = source.export_archive(character_id=archive_character)
    # Valid zero-addition processing has checked that this reaffirms the
    # existing fact, rather than contradicting it.
    assert source.journal.apply(sana, source.journal.snapshot(sana), [], notes=[])
    assert [item.id for item in source.retrieve(spica, '我喜欢喝红茶吗？', 5)] == [fact]
    candidate = tmp_path/'merged-current.sqlite3'
    prepare_restore(old, candidate, current=source.store.db_path)
    restored = SqliteMemoryAdapter(SQLiteMemoryStore(candidate), memory_config=config)
    assert [item.text for item in restored.retrieve(spica, '我喜欢喝红茶吗？', 5)] == ['我喜欢喝红茶。']
    assert not restored.journal.pending_scopes()

    # The resolution itself must survive a full or character-only backup,
    # even when the source role's private conversation is not exported.
    fresh = source.export_archive(character_id=archive_character)
    portable = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path/'portable.sqlite3'), memory_config=config)
    merge_archive(portable.journal, fresh, character_id=archive_character)
    merge_archive(portable.journal, old, character_id=archive_character)
    assert [item.text for item in portable.retrieve(spica, '我喜欢喝红茶吗？', 5)] == ['我喜欢喝红茶。']


@pytest.mark.parametrize('processing_version', [1, 2])
def test_processing_hold_and_cost_survive_import_and_old_archive_cannot_undo_resume(tmp_path, processing_version):
    scope = MemoryScope('spica', 'owner', 'desktop')
    source = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / 'source-cost.sqlite3'))
    source.record_evidence(scope, event_id='one:user', kind='user', source='desktop', content='保留这条原话。')
    snapshot = source.journal.snapshot(scope)
    for _ in range(3):
        receipt = source.journal.reserve_processing(scope, snapshot)
        source.journal.finish_processing(scope, receipt, reason='provider_unavailable')
    assert source.maintenance_status(scope)['held'] == 1
    archive = source.export_archive(character_id='spica')
    if processing_version == 1:
        archive['processing']['version'] = 1
        for item in archive['processing']['scopes']:
            item['state'].pop('resolved_disputes', None)
    target = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / 'target-cost.sqlite3'))
    target.restore_archive(archive, character_id='spica')
    assert target.maintenance_status(scope)['held'] == 1
    assert target.maintenance_status(scope)['total_attempts'] == 3
    target.journal.resume_processing(scope)
    merge_archive(target.journal, archive, character_id='spica')
    assert target.maintenance_status(scope)['held'] == 0
    assert target.maintenance_status(scope)['total_attempts'] == 3
    assert target.journal.reserve_processing(scope, target.journal.snapshot(scope))['attempt'] == 4


def test_legacy_archive_unknown_cost_is_held_only_for_new_unprocessed_material(tmp_path):
    scope = MemoryScope('spica', 'owner')
    source = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / 'legacy-source.sqlite3'))
    source.record_evidence(scope, event_id='pending:user', kind='user', source='desktop', content='尚未整理。')
    archive = source.export_archive(character_id='spica')
    archive.pop('processing', None)
    target = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / 'legacy-target.sqlite3'))
    target.restore_archive(archive, character_id='spica')
    assert target.maintenance_status(scope)['held_reasons'] == ['cost_history_unknown']
    target.journal.resume_processing(scope)
    source.record_evidence(scope, event_id='pending:playback', kind='playback', source='desktop', content='completed')
    archive = source.export_archive(character_id='spica')
    archive.pop('processing', None)
    merge_archive(target.journal, archive, character_id='spica')
    assert target.maintenance_status(scope)['held'] == 0


def test_imported_recovery_cannot_grant_new_attempts_to_existing_target_material(tmp_path):
    scope = MemoryScope('spica', 'owner')
    source = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / 'source-recovery.sqlite3'))
    source.record_evidence(scope, event_id='same:user', kind='user', source='desktop', content='同一份材料。')
    initial = source.export_archive()
    target = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / 'target-recovery.sqlite3'))
    merge_archive(target.journal, initial)
    for memory, count in ((source, 3), (target, 2)):
        for _ in range(count):
            snap = memory.journal.snapshot(scope)
            receipt = memory.journal.reserve_processing(scope, snap)
            memory.journal.finish_processing(scope, receipt, reason='temporary_error')
    source.journal.resume_processing(scope)
    merge_archive(target.journal, source.export_archive())
    assert target.maintenance_status(scope)['total_attempts'] == 3
    assert target.maintenance_status(scope)['held'] == 1


@pytest.mark.parametrize('first_character', ['spica', 'sana'])
def test_native_role_restore_rejects_stale_or_missing_cross_role_revision(tmp_path, first_character):
    from spica.config.schema import MemoryConfig

    config = MemoryConfig(shared_fact_characters=('spica', 'sana'))
    spica, sana = MemoryScope('spica', 'owner'), MemoryScope('sana', 'owner')
    source = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / 'source.sqlite3'), memory_config=config)
    old = source.remember(spica, '我喜欢吃香菜。')
    spica_old = source.export_archive(character_id='spica')
    source.revise(sana, old, '我不喜欢吃香菜，之前记反了。', reason='correction')
    sana_new = source.export_archive(character_id='sana')
    target = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / 'target.sqlite3'), memory_config=config)
    if first_character == 'spica':
        target.restore_archive(spica_old, character_id='spica')
    before = target.export_archive()['tables']

    with pytest.raises(ValueError, match='revision dependency'):
        target.restore_archive(sana_new, character_id='sana')

    assert target.export_archive()['tables'] == before
    assert all(row['character_id'] != 'sana' for row in target.list_memory(spica, include_history=True))


@pytest.mark.parametrize('restore_mode', ['roles', 'database'])
@pytest.mark.parametrize('reason', ['correction', 'change'])
def test_matching_cross_role_archives_preserve_revision_and_later_deletion(tmp_path, restore_mode, reason):
    from spica.config.schema import MemoryConfig

    config = MemoryConfig(shared_fact_characters=('spica', 'sana'))
    spica, sana = MemoryScope('spica', 'owner'), MemoryScope('sana', 'owner')
    source = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / 'source.sqlite3'), memory_config=config)
    old = source.remember(spica, '我喜欢吃香菜。')
    source.revise(sana, old, '我不喜欢吃香菜。', reason=reason)
    target_path = tmp_path / 'target.sqlite3'
    if restore_mode == 'database':
        prepare_restore(source.export_archive(), target_path)
    target = SqliteMemoryAdapter(SQLiteMemoryStore(target_path), memory_config=config)
    if restore_mode == 'roles':
        for character in ('spica', 'sana'):
            target.restore_archive(source.export_archive(character_id=character), character_id=character)

    for memory in (source, target):
        assert [row['content'] for row in memory.list_memory(spica)] == ['我不喜欢吃香菜。']
        memory.forget(sana, memory.list_memory(sana)[0]['id'])
        assert memory.list_memory(spica) == []
        past = memory.list_memory(spica, include_history=True)
        assert [row['content'] for row in past] == (['我喜欢吃香菜。'] if reason == 'change' else [])
        assert all(row['status'] == 'superseded' for row in past)


def test_historical_correction_cannot_accept_a_superseded_false_predecessor(tmp_path):
    from spica.config.schema import MemoryConfig

    config = MemoryConfig(shared_fact_characters=('spica', 'sana'))
    spica, sana = MemoryScope('spica', 'owner'), MemoryScope('sana', 'owner')
    source = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / 'source.sqlite3'), memory_config=config)
    old = source.remember(spica, '我以前喜欢吃香菜。')
    source.revise(spica, old, '我现在不喜欢香菜。', reason='change')
    before_correction = source.export_archive(character_id='spica')
    source.revise(sana, old, '我以前喜欢的是茴香，不是香菜。', reason='correction')
    target = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / 'target.sqlite3'), memory_config=config)
    target.restore_archive(before_correction, character_id='spica')
    before = target.export_archive()['tables']

    with pytest.raises(ValueError, match='revision dependency is incompatible'):
        target.restore_archive(source.export_archive(character_id='sana'), character_id='sana')

    assert target.export_archive()['tables'] == before


def test_imported_large_exclusion_preserves_unrelated_working_context(tmp_path):
    import json
    import time

    from spica.config.schema import MemoryConfig
    from spica.core.character_memory import read_character_save

    scope = MemoryScope('spica', 'owner')
    source = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / 'source.sqlite3'))
    source_id = source.record_evidence(scope, event_id='old:user', kind='user',
        source='desktop', content='旧资料')
    archive = export_archive(source.journal, character_id='spica')
    # SQLite's largest integer cannot be allocated as a Unicode string. A
    # portable withdrawal may outlive its raw text; reading it must stay bounded
    # by the available content, without losing other valid exchanges.
    archive['tables']['memory_exclusions'].append(dict(evidence_id=source_id,
        start=0, end=2**63 - 1, reason='deleted', recorded_at=time.time()))
    path = tmp_path / 'personal.json'
    path.write_text(json.dumps(dict(format=2, character_id='spica', personal_memory=archive)))
    save = read_character_save(path, 'spica')
    memory = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / 'target.sqlite3'),
        memory_config=MemoryConfig(embedding_model_dir=str(tmp_path / 'missing-index')))
    memory.restore_archive(save.personal_memory, character_id='spica')
    memory.record_evidence(scope, event_id='new:user', kind='user', source='desktop', content='接着画画')
    memory.record_evidence(scope, event_id='new:assistant', kind='assistant_generated',
        source='model', content='画一只猫吧')

    context = memory.working_context(scope, '画画')
    assert '接着画画' in str(context['messages'])
    assert '画一只猫吧' in str(context['messages'])
    assert '旧资料' not in str(context['messages'])
    assert memory.evidence(scope, ids=[source_id])[0]['content'] == '███'


def test_repeated_withdrawal_archive_roundtrip_and_legacy_empty_fences(tmp_path):
    memory = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / 'live.sqlite3'))
    scope = MemoryScope('spica', 'owner')
    text = '我喜欢雨天。我喜欢猫。'
    sid = memory.record_evidence(scope, event_id='prefs:user', kind='user', source='desktop', content=text)
    drafts = [dict(kind='preference', key=key, text=quote, origin='direct', replaces=[],
                   revision_reason=None, sources=[dict(id=sid, quote=quote)])
              for key, quote in [('rain', '我喜欢雨天。'), ('cat', '我喜欢猫。')]]
    assert memory.journal.apply(scope, memory.journal.snapshot(scope), drafts)
    memory.record_evidence(scope, event_id='reply:assistant', kind='assistant_generated',
        source='model', content='记住这两个偏好了。', metadata={'input_evidence_id': sid})
    for entry in memory.list_memory(scope):
        memory.forget(scope, entry['id'])
    archive = export_archive(memory.journal)
    assert all(row['start'] < row['end'] for row in archive['tables']['memory_exclusions'])
    candidate = tmp_path / 'candidate.sqlite3'
    prepare_restore(archive, candidate)
    restored = SqliteMemoryAdapter(SQLiteMemoryStore(candidate))
    assert restored.list_memory(scope) == []
    assert '喜欢' not in str(restored.evidence(scope))
    # Existing deployed exports can contain harmless (0,0) ranges. They do
    # not withdraw any character and must not block real deletion fences.
    legacy = dict(archive['tables']['memory_exclusions'][-1], start=0, end=0)
    archive['tables']['memory_exclusions'].append(legacy)
    legacy_candidate = tmp_path / 'legacy.sqlite3'
    prepare_restore(archive, legacy_candidate)
    legacy_memory = SqliteMemoryAdapter(SQLiteMemoryStore(legacy_candidate))
    assert legacy_memory.list_memory(scope) == []
    assert all(row['start'] < row['end'] for row in export_archive(legacy_memory.journal)['tables']['memory_exclusions'])
    archive['tables']['memory_exclusions'][-1]['end'] = -1
    with pytest.raises(ValueError, match='withdrawal fence'):
        prepare_restore(archive, tmp_path / 'invalid.sqlite3')


def test_restore_keeps_current_corrections_deletions_and_context_reset(tmp_path):
    live = tmp_path / 'live.sqlite3'
    memory = SqliteMemoryAdapter(SQLiteMemoryStore(live))
    scope = MemoryScope('spica', 'owner', 'desktop')
    wrong = memory.remember(scope, '雨天喜欢听夏影。')
    erase = memory.remember(scope, '咖啡喜欢加糖。')
    memory.record_evidence(scope,event_id='chat:user',kind='user',content='今天画了一只橘猫。',source='desktop')
    old = export_archive(memory.journal)
    revised = memory.revise(scope,wrong,'雨天喜欢听鳥の詩。',reason='correction')
    memory.forget(scope,erase)
    memory.clear_context(scope)
    candidate = tmp_path / 'candidate.sqlite3'
    report = prepare_restore(old,candidate,current=live)
    assert report['activated'] is False
    restored = SqliteMemoryAdapter(SQLiteMemoryStore(candidate))
    assert [r['content'] for r in restored.list_memory(scope)] == ['雨天喜欢听鳥の詩。']
    assert not restored.working_context(scope,'刚才橘猫')['messages']
    assert '咖啡喜欢加糖' not in str(restored.evidence(scope))
    assert '夏影' not in str(restored.evidence(scope))
    assert memory.list_memory(scope)[0]['id'] == revised
    # Recovering into an older state also imports newer deletion fences.
    fresh = tmp_path / 'fresh.sqlite3'
    prepare_restore(old,fresh)
    current_backup = export_archive(memory.journal)
    forward = tmp_path / 'forward.sqlite3'
    prepare_restore(current_backup,forward,current=fresh)
    forward_memory = SqliteMemoryAdapter(SQLiteMemoryStore(forward))
    assert [r['content'] for r in forward_memory.list_memory(scope)] == ['雨天喜欢听鳥の詩。']
    assert '咖啡喜欢加糖' not in str(forward_memory.evidence(scope))


def test_portable_role_save_remaps_ids_and_zero_addition_progress(tmp_path):
    scope = MemoryScope('sana','owner','qq')
    source = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path/'source.sqlite3'))
    entry = source.remember(scope,'喜欢钢琴。')
    snapshot = source.journal.snapshot(scope)
    assert source.journal.apply(scope,snapshot,[],notes=[])
    source.record_evidence(scope,event_id='late:user',kind='user',content='这次不用提醒。',source='qq')
    archive = export_archive(source.journal,character_id='sana')
    target = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path/'target.sqlite3'))
    target.remember(MemoryScope('spica','owner'),'喜欢写代码。')
    merge_archive(target.journal,archive,character_id='sana',empty_only=True)
    record = target.list_memory(scope)[0]
    assert record['id'] != entry
    assert target.evidence(scope,ids=[record['sources'][0]['id']])[0]['content'] == '喜欢钢琴。'
    assert target.maintenance_status(scope)['pending'] == 1
    snapshot = target.journal.snapshot(scope)
    assert [row['content'] for row in snapshot['evidence']] == ['这次不用提醒。']
    assert target.journal.apply(scope,snapshot,[],notes=[])
    assert target.maintenance_status(scope)['pending'] == 0
    merge_archive(target.journal,archive,character_id='sana',empty_only=True)
    assert len(target.list_memory(scope)) == 1
    assert target.maintenance_status(scope)['pending'] == 0
    with pytest.raises(ValueError,match='another character'):
        merge_archive(target.journal,archive,character_id='spica')


def test_expired_original_stays_expired_and_corrupt_restore_cannot_touch_live(tmp_path):
    path = tmp_path/'live.sqlite3'
    memory = SqliteMemoryAdapter(SQLiteMemoryStore(path))
    scope = MemoryScope('spica','owner')
    memory.remember(scope,'喜欢雨天练琴。')
    old = export_archive(memory.journal)
    memory.expire_raw(now=old['created_at'] + 366 * 86400)
    destination = tmp_path/'restored.sqlite3'
    prepare_restore(old,destination,current=path)
    restored = SqliteMemoryAdapter(SQLiteMemoryStore(destination))
    assert restored.list_memory(scope)[0]['content'] == '喜欢雨天练琴。'
    assert restored.list_memory(scope)[0]['sources'][0].get('quote') is None
    assert restored.evidence(scope)[0]['content'] == ''
    old['tables']['memory_evidence'][0]['bad_column'] = 'bad'
    with pytest.raises(ValueError,match='columns'):
        prepare_restore(old,tmp_path/'invalid.sqlite3',current=path)
    assert not (tmp_path/'invalid.sqlite3').exists()
    with pytest.raises(FileExistsError):
        prepare_restore(old,path)
    with closing(memory.store._connect()) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_imported_deletion_cascades_into_newer_local_replies_but_true_change_does_not(tmp_path):
    scope = MemoryScope('spica','owner')
    source = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path/'source.sqlite3'))
    entry = source.remember(scope,'喜欢战斗番。')
    target = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path/'target.sqlite3'))
    merge_archive(target.journal,export_archive(source.journal))
    target.record_evidence(scope,event_id='later:assistant',kind='assistant_generated',content='你说过喜欢战斗番。',source='model',
                           metadata={'memory_entry_ids':[entry]})
    source.revise(scope,entry,'现在不喜欢战斗番了。',reason='change')
    merge_archive(target.journal,export_archive(source.journal))
    assert target.evidence(scope)[-1]['content'] == '现在不喜欢战斗番了。'
    assert any(row['content']=='你说过喜欢战斗番。' for row in target.evidence(scope))
    source.forget(scope,entry)
    merge_archive(target.journal,export_archive(source.journal))
    assert not any(row['content']=='你说过喜欢战斗番。' for row in target.evidence(scope))


def test_old_role_save_cannot_revive_a_deleted_shared_fact(tmp_path):
    source = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path/'source.sqlite3'))
    spica, sana = MemoryScope('spica','owner'), MemoryScope('sana','owner')
    entry = source.remember(spica,'喜欢雨天听夏影。')
    source.record_evidence(sana,event_id='old:assistant',kind='assistant_generated',content='你喜欢雨天听夏影。',source='model',
                           metadata={'memory_entry_ids':[entry]})
    old_sana = export_archive(source.journal,character_id='sana')
    assert '喜欢雨天听夏影' not in str(old_sana['external_entries'])
    source.forget(spica,entry)
    target = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path/'target.sqlite3'))
    merge_archive(target.journal,export_archive(source.journal,character_id='spica'),character_id='spica')
    merge_archive(target.journal,old_sana,character_id='sana')
    assert not target.working_context(sana,'刚才雨天')['messages']
    assert target.evidence(sana)[0]['content'] == ''


def test_role_archive_cannot_withdraw_an_external_characters_original(tmp_path):
    source = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / 'sana.sqlite3'))
    source.remember(MemoryScope('sana','owner'), 'Sana 的独有经历。', category='relationship')
    archive = export_archive(source.journal, character_id='sana')
    target = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / 'target.sqlite3'))
    spica = MemoryScope('spica','owner')
    target.remember(spica, 'Spica 的独有关系经历。', category='relationship')
    original = target.evidence(spica)[0]
    before = export_archive(target.journal)
    archive['external_evidence'] = [{'id':99,'character_id':'spica','user_id':'owner','event_id':original['event_id']}]
    archive['tables']['memory_exclusions'] = [{'evidence_id':99,'start':0,'end':len(original['content']),
                                              'reason':'deleted','recorded_at':archive['created_at']}]
    with pytest.raises(ValueError, match='withdrawal fence'):
        merge_archive(target.journal, archive, character_id='sana', empty_only=True)
    assert export_archive(target.journal)['tables'] == before['tables']


def test_deleting_an_imported_external_dependency_keeps_its_source_role(tmp_path):
    source = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / 'sana.sqlite3'))
    sana, spica = MemoryScope('sana','owner'), MemoryScope('spica','owner')
    source.remember(sana, 'Sana 的经历。', category='relationship')
    archive = export_archive(source.journal, character_id='sana')
    target = SqliteMemoryAdapter(SQLiteMemoryStore(tmp_path / 'target.sqlite3'))
    target.remember(spica, 'Spica 的独有关系经历。', category='relationship')
    original = target.evidence(spica)[0]
    before = export_archive(target.journal, character_id='spica')['tables']
    archive['external_evidence'] = [{'id':99,'character_id':'spica','user_id':'owner','event_id':original['event_id']}]
    archive['tables']['memory_entries'][0]['sources'] = [{'id':99,'start':0,'end':len(original['content']),'quote':original['content']}]
    merge_archive(target.journal, archive, character_id='sana', empty_only=True)
    target._semantic.scores = lambda query, rows: {row['id']: .9 for row in rows}
    assert original['content'] not in str(target.list_memory(sana))
    assert target.retrieve(sana, 'Sana 的经历', 5)[0].source_quotes == ()
    target.forget(sana, target.list_memory(sana)[0]['id'])
    assert export_archive(target.journal, character_id='spica')['tables'] == before


def test_confirmed_legacy_has_present_day_evidence_and_cannot_replay_after_delete(tmp_path):
    from scripts.migrate_memory import legacy_candidates
    from spica.ports.memory import EvidenceAdmissionError
    store = SQLiteMemoryStore(tmp_path/'legacy.sqlite3')
    store.add_memory('spica::default','user','以前的含糊归纳。')
    row = legacy_candidates(store.db_path)[0]
    memory = SqliteMemoryAdapter(store)
    scope = MemoryScope('spica','owner')
    assert memory.list_memory(scope) == []
    provenance = {'fingerprint':row['fingerprint'],'legacy_id':row['id']}
    entry = memory.journal.remember(scope,'本人现在确认喜欢雨天。',provenance=provenance)
    original = memory.evidence(scope)[0]
    assert original['source'] == 'memory_editor'
    assert original['metadata']['original_chat_available'] is False
    assert original['content'] == '本人现在确认喜欢雨天。'
    memory.forget(scope,entry)
    with pytest.raises(EvidenceAdmissionError,match='already handled'):
        memory.journal.remember(scope,'本人现在确认喜欢雨天。',provenance=provenance)
