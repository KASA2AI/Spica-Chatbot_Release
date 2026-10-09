"""Portable personal memory and restoration into a separate, reviewable database.

Business task stores are deliberately outside this archive. Restore merges with
the current memory state: an old backup cannot undo a correction or deletion.
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

from memory.evidence import EvidenceJournal
from memory.store import SQLiteMemoryStore
from memory import processing
from spica.ports.memory import MemoryScope

_TABLES = ('memory_evidence', 'memory_entries', 'memory_exclusions', 'memory_context', 'memory_context_resets')
_JSON = {'metadata', 'sources', 'replaces', 'notes'}
_ENTRY_STATUS = {'active': 0, 'superseded': 1, 'retracted': 2, 'deleted': 3}
_RAW_STATUS = {'active': 0, 'redacted': 1, 'retracted': 2, 'deleted': 3, 'expired': 4}


def export_archive(journal: EvidenceJournal, *, character_id: str | None = None) -> dict:
    with closing(journal.store._connect()) as db, db:
        db.execute('BEGIN')
        tables = {name: [dict(row) for row in db.execute('SELECT * FROM ' + name)] for name in _TABLES}
        work_state = processing.archive_state(db, character_id)
        if character_id is not None:
            for name in _TABLES:
                if name != 'memory_exclusions':
                    tables[name] = [row for row in tables[name] if row['character_id'] == character_id]
            ids = {row['id'] for row in tables['memory_evidence']}
            tables['memory_exclusions'] = [row for row in tables['memory_exclusions'] if row['evidence_id'] in ids]
        for row in tables['memory_evidence']:
            # Even a correction whose original has not expired is exported
            # through the same withdrawn-fragment projection as normal reads.
            row['content'] = journal._with_exclusions(db, [row])[0]['content']
        for rows in tables.values():
            for row in rows:
                for column in _JSON & row.keys():
                    row[column] = json.loads(row[column])
        # Foreign dependency identities contain no foreign chat or memory text.
        evidence_refs = {item for row in tables['memory_entries'] for item in (s['id'] for s in row['sources'])}
        entry_refs = set()
        for row in tables['memory_evidence']:
            meta = row['metadata']
            evidence_refs.update(meta.get('context_evidence_ids', []))
            evidence_refs.update(i for i in (meta.get('input_evidence_id'), meta.get('generated_evidence_id')) if i is not None)
            entry_refs.update(meta.get('memory_entry_ids', []))
        for row in tables['memory_entries']:
            entry_refs.update(row['replaces'])
        own_evidence = {row['id'] for row in tables['memory_evidence']}
        own_entries = {row['id'] for row in tables['memory_entries']}
        external_evidence = [dict(row) for row in db.execute('SELECT id,character_id,user_id,event_id FROM memory_evidence')
                             if row['id'] in evidence_refs - own_evidence]
        external_entries = [dict(row) for row in db.execute('SELECT id,uid,character_id,user_id FROM memory_entries')
                            if row['id'] in entry_refs - own_entries]
    return {'format': 'memory.archive.v1', 'created_at': time.time(), 'character_id': character_id,
            'tables': tables, 'external_evidence': external_evidence, 'external_entries': external_entries,
            'processing': work_state}


def _insert(db, table, row):
    columns = tuple(row)
    db.execute('INSERT INTO ' + table + ' (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')',
               tuple(json.dumps(row[key], ensure_ascii=False) if key in _JSON else row[key] for key in columns))


def _sources(db, sources, evidence_ids):
    result = []
    for source in sources:
        source_id = evidence_ids.get(source['id'])
        if source_id is None:
            raise ValueError('memory archive is missing an original source; restore the complete backup first')
        item = {**source, 'id': source_id}
        original = db.execute('SELECT * FROM memory_evidence WHERE id=?', (source_id,)).fetchone()
        if original['status'] == 'expired':
            item.pop('quote', None)
            item['availability'] = 'expired'
        result.append(item)
    return result


def _supported(db, sources):
    for source in sources:
        original = db.execute('SELECT * FROM memory_evidence WHERE id=?', (source['id'],)).fetchone()
        if original is None or original['status'] in {'deleted', 'retracted'}:
            return False
        start, end = source.get('start'), source.get('end')
        if start is None or end is None or not 0 <= start < end:
            return False
        if db.execute('SELECT 1 FROM memory_exclusions WHERE evidence_id=? AND start<? AND end>?',
                      (source['id'], end, start)).fetchone():
            return False
        if original['status'] == 'expired':
            continue  # verified facts survive retention, never a deletion fence
        if end > len(original['content']):
            return False
        if source.get('quote') != original['content'][start:end]:
            return False
    return bool(sources)


def merge_archive(journal: EvidenceJournal, archive: dict, *, character_id: str | None = None,
                  empty_only: bool = False) -> dict:
    """Merge one validated save transactionally; use prepare_restore for rollback.

All SQL identifiers come from the current schema, never from the archive. Import
into an existing store preserves current content and unions withdrawal fences.
"""
    if archive.get('format') != 'memory.archive.v1' or set(archive.get('tables', {})) != set(_TABLES):
        raise ValueError('unsupported personal memory archive')
    if character_id is not None and archive.get('character_id') != character_id:
        raise ValueError('personal memory archive belongs to another character')
    tables = archive['tables']
    with closing(journal.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        for name, rows in tables.items():
            columns = {row['name'] for row in db.execute('PRAGMA table_info(' + name + ')')}
            if not isinstance(rows, list):
                raise ValueError('invalid archive table')
            for row in rows:
                if not isinstance(row, dict) or not set(row) <= columns:
                    raise ValueError('invalid archive columns')
                if 'character_id' in row and (not row['character_id'] or not row['user_id']
                        or character_id is not None and row['character_id'] != character_id):
                    raise ValueError('archive scope mismatch')
        if empty_only and db.execute('SELECT 1 FROM memory_evidence WHERE character_id=? LIMIT 1', (character_id,)).fetchone():
            return {'imported_evidence': 0, 'imported_entries': 0, 'existing_scope': True}
        evidence_ids, entry_ids, new_evidence = {}, {}, []
        existing_exchanges = {tuple(row) for row in db.execute(
            'SELECT DISTINCT character_id,user_id,conversation_id,turn_id FROM memory_evidence')}
        remap_evidence = []
        current_receipts = {}
        changed_evidence, changed_entries = set(), set()
        evidence_before = {row['id']: row for row in tables['memory_evidence']}
        entry_before = {row['id']: row for row in tables['memory_entries']}
        if len(evidence_before) != len(tables['memory_evidence']) or len(entry_before) != len(tables['memory_entries']):
            raise ValueError('duplicate archive identity')
        for ref in archive.get('external_evidence', []):
            found = db.execute('SELECT id FROM memory_evidence WHERE character_id=? AND user_id=? AND event_id=?',
                               (ref['character_id'], ref['user_id'], ref['event_id'])).fetchone()
            if found:
                evidence_ids[ref['id']] = found[0]
        for ref in archive.get('external_entries', []):
            found = db.execute('SELECT id FROM memory_entries WHERE uid=? AND character_id=? AND user_id=?',
                               (ref['uid'], ref['character_id'], ref['user_id'])).fetchone()
            if found:
                entry_ids[ref['id']] = found[0]
        for row in tables['memory_evidence']:
            if row['status'] not in _RAW_STATUS or row['kind'] not in {'user','manual','assistant_generated','delivery','playback','tool','system_event','environment'}:
                raise ValueError('invalid evidence state')
            found = db.execute('SELECT * FROM memory_evidence WHERE character_id=? AND user_id=? AND event_id=?',
                               (row['character_id'], row['user_id'], row['event_id'])).fetchone()
            if found is None:
                _insert(db, 'memory_evidence', {key: value for key, value in row.items() if key != 'id'})
                target = db.execute('SELECT last_insert_rowid()').fetchone()[0]
                new_evidence.append(row)
                remap_evidence.append(row)
            else:
                if any(found[key] != row[key] for key in ('kind', 'source', 'conversation_id', 'turn_id')):
                    raise ValueError('conflicting evidence identity')
                target = found['id']
                if _RAW_STATUS[row['status']] > _RAW_STATUS[found['status']]:
                    if row['status'] in {'redacted', 'retracted', 'deleted'}:
                        # Older withdrawal archives erased all metadata. The
                        # matched current event can still prove its frozen
                        # identity/outcome; never replace that with old blanks.
                        current_receipts[target] = journal._receipt_metadata(json.loads(found['metadata']),
                            answer_text=found['content'] if found['kind']=='assistant_generated'
                            and found['status']=='active' else None)
                    db.execute('UPDATE memory_evidence SET status=?,content=?,metadata=?,revision=MAX(revision,?) WHERE id=?',
                               (row['status'], row['content'], json.dumps(row['metadata']), row['revision'], target))
                    remap_evidence.append(row)
                    changed_evidence.add(target)
                db.execute('UPDATE memory_evidence SET organized=MAX(organized,?) WHERE id=?', (row['organized'], target))
            evidence_ids[row['id']] = target
        imported_entries = 0
        for row in tables['memory_entries']:
            if row['status'] not in _ENTRY_STATUS or not row.get('uid'):
                raise ValueError('invalid entry state or portable identity')
            found = db.execute('SELECT * FROM memory_entries WHERE uid=?', (row['uid'],)).fetchone()
            if found is None:
                mapped = {**row, 'sources': _sources(db, row['sources'], evidence_ids), 'replaces': []}
                _insert(db, 'memory_entries', {key: value for key, value in mapped.items() if key != 'id'})
                target = db.execute('SELECT last_insert_rowid()').fetchone()[0]
                imported_entries += 1
            else:
                if any(found[key] != row[key] for key in ('character_id', 'user_id', 'kind', 'semantic_key')):
                    raise ValueError('conflicting memory identity')
                target = found['id']
                if _ENTRY_STATUS[row['status']] > _ENTRY_STATUS[found['status']]:
                    db.execute('UPDATE memory_entries SET status=?,content=?,sources=? WHERE id=?',
                               (row['status'], row['content'], json.dumps(_sources(db,row['sources'],evidence_ids)), target))
                    if row['status'] in {'retracted', 'deleted'}:
                        changed_entries.add(target)
            entry_ids[row['id']] = target
        for row in tables['memory_entries']:
            target = entry_ids[row['id']]
            reason = row['revision_reason']
            predecessors = [entry_ids.get(previous) for previous in row['replaces']]
            if (bool(predecessors) != bool(reason) or reason not in {None, 'change', 'correction'}
                    or any(previous is None for previous in predecessors)):
                raise ValueError('memory archive revision dependency is missing; restore the matching source-role archive or a complete database backup first')
            for previous in predecessors:
                predecessor = db.execute('SELECT user_id,status FROM memory_entries WHERE id=?', (previous,)).fetchone()
                allowed = {'retracted', 'deleted'} if reason == 'correction' else {'superseded', 'retracted', 'deleted'}
                if predecessor['user_id'] != row['user_id'] or predecessor['status'] not in allowed:
                    # A foreign locator grants no write authority. In particular,
                    # importing a correction cannot leave its old false fact active
                    # or redact the source role to make incompatible saves fit.
                    raise ValueError('memory archive revision dependency is incompatible; restore the matching source-role archive or a complete database backup first')
            existing = json.loads(db.execute('SELECT replaces FROM memory_entries WHERE id=?', (target,)).fetchone()[0])
            db.execute('UPDATE memory_entries SET replaces=? WHERE id=?',
                       (json.dumps(sorted(set(existing) | set(predecessors))), target))
        for row in tables['memory_exclusions']:
            # External locators identify dependencies; they grant no authority
            # to redact another character's originals during a role import.
            if (row['evidence_id'] not in evidence_before
                    or type(row['start']) is not int or type(row['end']) is not int
                    or not 0 <= row['start'] <= row['end']
                    or row['reason'] not in {'deleted', 'correction'}):
                raise ValueError('invalid withdrawal fence')
            if row['start'] == row['end']:
                continue  # legacy no-op from withdrawing an already empty reply
            inserted = db.execute('INSERT OR IGNORE INTO memory_exclusions VALUES (?,?,?,?,?)',
                       (evidence_ids[row['evidence_id']], row['start'], row['end'], row['reason'], row['recorded_at']))
            if inserted.rowcount:
                changed_evidence.add(evidence_ids[row['evidence_id']])
        for row in db.execute('SELECT * FROM memory_evidence').fetchall():
            content = journal._with_exclusions(db, [row])[0]['content']
            if content != row['content']:
                db.execute('UPDATE memory_evidence SET content=?,revision=revision+1 WHERE id=?', (content, row['id']))
        for row in remap_evidence:
            meta = dict(row['metadata'])
            refs = set(meta.get('context_evidence_ids', [])) | {i for i in (meta.get('input_evidence_id'), meta.get('generated_evidence_id')) if i is not None}
            missing = bool(refs - evidence_ids.keys() or set(meta.get('memory_entry_ids', [])) - entry_ids.keys())
            # A role save carries only locators for other roles. Their current
            # tombstones remain authoritative even when this import added none.
            missing = missing or any(old_id not in evidence_before and (
                db.execute("SELECT 1 FROM memory_evidence WHERE id=? AND status IN ('deleted','retracted')", (evidence_ids[old_id],)).fetchone()
                or db.execute('SELECT 1 FROM memory_exclusions WHERE evidence_id=?', (evidence_ids[old_id],)).fetchone())
                for old_id in refs if old_id in evidence_ids)
            missing = missing or any(old_id not in entry_before and db.execute(
                "SELECT 1 FROM memory_entries WHERE id=? AND status IN ('deleted','retracted')", (entry_ids[old_id],)).fetchone()
                for old_id in meta.get('memory_entry_ids', []) if old_id in entry_ids)
            changed = any(old_id in evidence_before and db.execute('SELECT revision FROM memory_evidence WHERE id=?',
                          (evidence_ids[old_id],)).fetchone()[0] > evidence_before[old_id]['revision'] for old_id in refs if old_id in evidence_ids)
            changed = changed or any(old_id in entry_before and db.execute('SELECT status FROM memory_entries WHERE id=?',
                (entry_ids[old_id],)).fetchone()[0] in {'deleted','retracted'} and entry_before[old_id]['status'] not in {'deleted','retracted'}
                for old_id in meta.get('memory_entry_ids', []) if old_id in entry_ids)
            withdrawn = row['kind'] in {'assistant_generated','delivery','playback','tool','environment'} and (missing or changed)
            for key in ('input_evidence_id', 'generated_evidence_id'):
                if meta.get(key) is not None:
                    meta[key] = evidence_ids.get(meta[key])
            meta['context_evidence_ids'] = [evidence_ids[i] for i in meta.get('context_evidence_ids', []) if i in evidence_ids]
            meta['context_evidence_revisions'] = {str(evidence_ids[int(key)]): value for key,value in meta.get('context_evidence_revisions', {}).items() if int(key) in evidence_ids}
            meta['memory_entry_ids'] = [entry_ids[i] for i in meta.get('memory_entry_ids', []) if i in entry_ids]
            # These retained references already belong to the destination DB.
            # Merge only after mapping the incoming archive's references.
            meta.update(current_receipts.get(evidence_ids[row['id']], {}))
            if withdrawn or row['status'] in {'deleted', 'retracted'}:
                meta = journal._receipt_metadata(meta, answer_text=row['content']
                    if row['kind'] == 'assistant_generated' and row['status'] == 'active' else None)
            if withdrawn:
                db.execute("UPDATE memory_evidence SET content='',status='deleted',revision=revision+1 WHERE id=?", (evidence_ids[row['id']],))
                changed_evidence.add(evidence_ids[row['id']])
            db.execute('UPDATE memory_evidence SET metadata=? WHERE id=?', (json.dumps(meta, ensure_ascii=False), evidence_ids[row['id']]))
        # Propagate imported withdrawals through both newly restored and newer
        # local derivatives that were not present in that backup.
        imported_raw = {evidence_ids[row['id']]: row for row in remap_evidence}
        while True:
            before = len(changed_evidence), len(changed_entries)
            for row in db.execute("SELECT * FROM memory_entries WHERE status IN ('active','superseded')").fetchall():
                sources = json.loads(row['sources'])
                if not _supported(db, sources):
                    locations = [{key: source[key] for key in ('id','start','end') if key in source} for source in sources]
                    db.execute("UPDATE memory_entries SET status='retracted',content='',sources=? WHERE id=?", (json.dumps(locations),row['id']))
                    changed_entries.add(row['id'])
            for row in db.execute("SELECT * FROM memory_evidence WHERE kind IN ('assistant_generated','delivery','playback','tool','environment') AND status IN ('active','redacted')").fetchall():
                meta = json.loads(row['metadata'])
                refs = set(meta.get('context_evidence_ids', [])) | {meta.get('input_evidence_id'),meta.get('generated_evidence_id')}
                affected = bool(refs & changed_evidence or set(meta.get('memory_entry_ids', [])) & changed_entries)
                archived = imported_raw.get(row['id'])
                if archived is not None and affected:
                    # A newer, already corrected reply in this backup may cite
                    # unchanged retained fragments. Compare the saved version.
                    old_meta = archived['metadata']
                    old_refs = set(old_meta.get('context_evidence_ids', [])) | {old_meta.get('input_evidence_id'),old_meta.get('generated_evidence_id')}
                    affected = any(i in evidence_before and db.execute('SELECT revision FROM memory_evidence WHERE id=?',
                        (evidence_ids[i],)).fetchone()[0] > evidence_before[i]['revision'] for i in old_refs if i in evidence_ids)
                    affected = affected or any(i in entry_before and db.execute('SELECT status FROM memory_entries WHERE id=?',
                        (entry_ids[i],)).fetchone()[0] in {'deleted','retracted'} and entry_before[i]['status'] not in {'deleted','retracted'}
                        for i in old_meta.get('memory_entry_ids', []) if i in entry_ids)
                if affected:
                    meta = journal._receipt_metadata(meta, answer_text=row['content']
                        if row['kind'] == 'assistant_generated' and row['status'] == 'active' else None)
                    db.execute("UPDATE memory_evidence SET content='',metadata=?,status='deleted',revision=revision+1 WHERE id=?",
                               (json.dumps(meta, ensure_ascii=False), row['id']))
                    changed_evidence.add(row['id'])
            if before == (len(changed_evidence),len(changed_entries)):
                break
        for row in tables['memory_context_resets']:
            db.execute('''INSERT INTO memory_context_resets VALUES (?,?,?,?,?)
                ON CONFLICT(character_id,user_id,conversation_id) DO UPDATE SET reset_at=MAX(reset_at,excluded.reset_at),through_id=MAX(through_id,excluded.through_id)''',
                (row['character_id'], row['user_id'], row['conversation_id'], evidence_ids.get(row['through_id'], 0), row['reset_at']))
        for row in tables['memory_context']:
            if not db.execute('SELECT 1 FROM memory_context WHERE character_id=? AND user_id=?', (row['character_id'],row['user_id'])).fetchone():
                notes = [{**note, 'sources': _sources(db,note['sources'],evidence_ids)} for note in row['notes']]
                _insert(db,'memory_context',{**row,'through_id':evidence_ids.get(row['through_id'],0),'notes':notes})
        for row in db.execute('SELECT * FROM memory_context').fetchall():
            notes = [note for note in json.loads(row['notes']) if _supported(db,note['sources'])]
            db.execute('UPDATE memory_context SET notes=? WHERE character_id=? AND user_id=?',
                       (json.dumps(notes,ensure_ascii=False),row['character_id'],row['user_id']))
        journal._repair_tool_dependencies(db)
        if 'processing' in archive:
            processing.merge_state(db, archive['processing'], character_id=character_id)
        else:
            # Unknown historical spend in a legacy archive cannot silently
            # grant fresh automatic attempts. Existing target work is untouched.
            for row in new_evidence:
                if not row['organized'] and tuple(row[k] for k in ('character_id','user_id','conversation_id','turn_id')) not in existing_exchanges:
                    scope = MemoryScope(row['character_id'], row['user_id'])
                    processing.hold(db, scope, [row], 'cost_history_unknown')
        for character, user in {(row['character_id'],row['user_id']) for row in tables['memory_evidence']}:
            processing.retire_unavailable(db, MemoryScope(character, user))
            journal._bump(db, character, user)
            db.execute('''UPDATE memory_processing SET processed_id=COALESCE((SELECT MAX(id) FROM memory_evidence
                WHERE character_id=? AND user_id=? AND organized=1),0) WHERE character_id=? AND user_id=?''', (character,user,character,user))
        return {'imported_evidence': len(new_evidence), 'imported_entries': imported_entries, 'existing_scope': False}


def prepare_restore(archive: dict, destination: Path, *, current: Path | None = None,
                    retention_days: int = 365) -> dict:
    """Never overwrite the live DB or an existing candidate, including on failure."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb'):
        pass
    try:
        if current is not None:
            with closing(sqlite3.connect(Path(current).resolve().as_uri() + '?mode=ro', uri=True)) as source, closing(sqlite3.connect(destination)) as target:
                source.backup(target)
        journal = EvidenceJournal(SQLiteMemoryStore(destination))
        report = merge_archive(journal, archive)
        report['expired_raw'] = journal.expire_raw(time.time() - retention_days * 86400)
        with closing(journal.store._connect()) as db, db:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('restored memory integrity check failed')
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_vectors'").fetchone():
                db.execute('DELETE FROM memory_vectors')
        return {**report, 'candidate': str(destination), 'activated': False}
    except Exception:
        # This function alone created these temporary candidate files.
        for suffix in ('', '-wal', '-shm'):
            destination.with_name(destination.name + suffix).unlink(missing_ok=True)
        raise
