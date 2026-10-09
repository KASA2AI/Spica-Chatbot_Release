"""Work metadata in memory_processing; evidence and scheduling keep their owners."""
from __future__ import annotations

import json
import time
from uuid import uuid4


def exchange_key(row):
    return json.dumps([row['conversation_id'], row['turn_id']], ensure_ascii=False, separators=(',', ':'))


def load(db, scope):
    row = db.execute('SELECT processing_state FROM memory_processing WHERE character_id=? AND user_id=?',
                     (scope.character_id, scope.user_id)).fetchone()
    state = json.loads(row[0]) if row else {}
    state.setdefault('turns', {})
    state.setdefault('disputes', {})
    state.setdefault('resolved_disputes', {})
    return state


def save(db, scope, state):
    # Accounting must not change the source content version.
    db.execute('''INSERT INTO memory_processing(character_id,user_id,processing_state) VALUES (?,?,?)
        ON CONFLICT(character_id,user_id) DO UPDATE SET processing_state=excluded.processing_state''',
        (scope.character_id, scope.user_id, json.dumps(state, ensure_ascii=False)))


def eligible(rows, state):
    return [row for row in rows if state['turns'].get(exchange_key(row), {}).get('state') not in {'held', 'unavailable'}]


def hold(db, scope, rows, reason):
    state = load(db, scope)
    for row in rows:
        value = state['turns'].setdefault(exchange_key(row), {})
        value.update(state='held', reason=reason, active_token=None)
    save(db, scope, state)


def admit(db, scope, rows):
    """Freeze eligible exchanges, without charging a model attempt."""
    state = load(db, scope)
    changed = False
    for key in dict.fromkeys(exchange_key(row) for row in eligible(rows, state)):
        value = state['turns'].setdefault(key, {})
        if value.get('state') not in {'active', 'pending'}:
            value.update(state='pending', reason='', active_token=None, retry_at=0.)
            changed = True
    if changed:
        save(db, scope, state)


def reserve(db, scope, rows, *, max_attempts):
    state = load(db, scope)
    keys = list(dict.fromkeys(exchange_key(row) for row in rows))
    if not keys:
        return None
    exhausted_keys = set()
    for key in keys:
        value = state['turns'].get(key, {})
        if value.get('state') == 'held' or value.get('total_attempts', 0) - value.get('resume_floor', 0) >= max_attempts:
            exhausted_keys.add(key)
    if exhausted_keys:
        hold(db, scope, [row for row in rows if exchange_key(row) in exhausted_keys], 'attempts_exhausted')
        return None
    token = uuid4().hex
    for key in keys:
        value = state['turns'].setdefault(key, {})
        value.update(total_attempts=value.get('total_attempts', 0) + 1,
                     state='active', reason='', active_token=token, max_attempts=max_attempts)
    save(db, scope, state)
    return dict(batch_id=token, turns=keys,
                attempt=max(state['turns'][key]['total_attempts'] for key in keys),
                attempt_in_window=max(state['turns'][key]['total_attempts']-state['turns'][key].get('resume_floor', 0) for key in keys),
                remaining_attempts=min(max_attempts-state['turns'][key]['total_attempts']+state['turns'][key].get('resume_floor', 0) for key in keys))


def owns(db, scope, receipt):
    state = load(db, scope)
    return bool(receipt['turns']) and all(state['turns'].get(key, {}).get('active_token') == receipt['batch_id']
                                         for key in receipt['turns'])


def finish(db, scope, receipt, *, success=False, reason='', max_attempts=3, pause=False,
           retry_at=0., provider_key=None):
    state = load(db, scope)
    owns = False
    for key in receipt['turns']:
        value = state['turns'].get(key, {})
        if value.get('active_token') != receipt['batch_id']:
            continue
        owns = True
        exhausted = value.get('total_attempts', 0) - value.get('resume_floor', 0) >= max_attempts
        value.update(state='done' if success else ('held' if pause or exhausted else 'pending'),
                     reason=reason, active_token=None, retry_at=0. if success else retry_at)
    if provider_key and owns:
        state['provider_hold'] = dict(key=provider_key, reason=reason, at=time.time())
    save(db, scope, state)


def resume(db, scope):
    state = load(db, scope)
    now = time.time()
    for value in state['turns'].values():
        if value.get('state') == 'held':
            value.update(state='pending', reason='', resume_floor=value.get('total_attempts', 0),
                         resumed_at=now, recovery_generation=value.get('recovery_generation', 0)+1,
                         retry_at=0., active_token=None)
    previous_hold = state.pop('provider_hold', None)
    if previous_hold:
        state['provider_recovery'] = dict(key=previous_hold['key'], at=now)
    save(db, scope, state)


def status(db, scope, rows):
    state = load(db, scope)
    held = [row for row in rows if state['turns'].get(exchange_key(row), {}).get('state') == 'held']
    return dict(held=len(held), schedulable=len(eligible(rows, state)),
        held_reasons=sorted({state['turns'][exchange_key(row)].get('reason', '') for row in held}),
        total_attempts=sum(value.get('total_attempts', 0) for value in state['turns'].values()),
        provider_hold=state.get('provider_hold'))


def retire_unavailable(db, scope):
    state = load(db, scope)
    groups = {}
    for row in db.execute('SELECT * FROM memory_evidence WHERE character_id=? AND user_id=?',
                          (scope.character_id, scope.user_id)):
        groups.setdefault(exchange_key(row), []).append(row)
    for key, rows in groups.items():
        if all(row['status'] not in {'active', 'redacted'} or not row['content'].replace('█', '').strip() for row in rows):
            value = state['turns'].setdefault(key, {})
            value.update(state='unavailable', reason='source_unavailable', active_token=None, retry_at=0.)
    retired = {row[0] for row in db.execute("SELECT uid FROM memory_entries WHERE user_id=? AND status IN ('deleted','retracted')", (scope.user_id,))}
    state['disputes'] = {source: sorted(set(targets)-retired) for source, targets in state['disputes'].items()
                         if set(targets)-retired}
    save(db, scope, state)


def _dispute_source_key(db, character_id, user_id, source):
    # Own event IDs are opaque strings. Foreign role archives use an explicit
    # [character, event] locator instead, without exporting the private text.
    if not db.execute('SELECT 1 FROM memory_evidence WHERE character_id=? AND user_id=? AND event_id=?',
                      (character_id, user_id, source)).fetchone():
        try:
            parts = json.loads(source)
            if isinstance(parts, list) and len(parts) == 2 and all(isinstance(part, str) for part in parts):
                return json.dumps(parts, separators=(',', ':'))
        except ValueError:
            pass
    return json.dumps([character_id, source], separators=(',', ':'))


def resolve_disputes(db, scope, snapshot):
    """A verified treatment may resolve only targets actually in its input."""
    from spica.ports.memory import MemoryScope
    sources = {row['event_id'] for row in snapshot['evidence']}
    ids = [entry['id'] for entry in snapshot.get('existing_entries', [])]
    considered = {row[0] for row in db.execute('SELECT uid FROM memory_entries WHERE id IN (SELECT value FROM json_each(?))', (json.dumps(ids),))}
    for row in db.execute('SELECT character_id,user_id FROM memory_processing WHERE user_id=?', (scope.user_id,)).fetchall():
        owner = MemoryScope(*row)
        state = load(db, owner)
        for source in sources:
            qualified = json.dumps([scope.character_id, source], separators=(',', ':'))
            matches = {source, qualified} if owner.character_id == scope.character_id else {qualified}
            for key in matches & state['disputes'].keys():
                targets = set(state['disputes'][key])
                resolved = targets & considered
                if resolved:
                    state['resolved_disputes'][qualified] = sorted(
                        set(state['resolved_disputes'].get(qualified, [])) | resolved)
                remaining = targets - considered
                if remaining:
                    state['disputes'][key] = sorted(remaining)
                else:
                    del state['disputes'][key]
        save(db, owner, state)


def archive_state(db, character_id=None):
    rows = db.execute('SELECT character_id,user_id,processing_state FROM memory_processing').fetchall()
    scopes = [dict(character_id=row['character_id'], user_id=row['user_id'],
        state=json.loads(row['processing_state'])) for row in rows
        if character_id is None or row['character_id'] == character_id]
    if character_id is not None:
        for scope in scopes:
            exported = {row[0] for row in db.execute('SELECT uid FROM memory_entries WHERE character_id=? AND user_id=?',
                        (character_id, scope['user_id']))}
            disputes = scope['state'].setdefault('disputes', {})
            resolved = scope['state'].setdefault('resolved_disputes', {})
            for row in rows:
                if row['user_id'] != scope['user_id'] or row['character_id'] == character_id:
                    continue
                for source, targets in json.loads(row['processing_state']).get('disputes', {}).items():
                    relevant = exported & set(targets)
                    if relevant:
                        # Only portable IDs cross the role boundary, never the
                        # other role's exchange, processing history or wording.
                        key = _dispute_source_key(db, row['character_id'], row['user_id'], source)
                        disputes[key] = sorted(set(disputes.get(key, [])) | relevant)
                for source, targets in json.loads(row['processing_state']).get('resolved_disputes', {}).items():
                    relevant = exported & set(targets)
                    if relevant:
                        resolved[source] = sorted(set(resolved.get(source, [])) | relevant)
    return dict(version=2, scopes=scopes)


def merge_state(db, document, *, character_id=None):
    from spica.ports.memory import MemoryScope
    if not isinstance(document, dict) or type(document.get('version')) is not int or document['version'] not in {1, 2} or not isinstance(document.get('scopes'), list):
        raise ValueError('unsupported memory processing archive')
    seen = set()
    for item in document['scopes']:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k] for k in ('character_id', 'user_id')):
            raise ValueError('invalid processing archive scope')
        scope = MemoryScope(item['character_id'], item['user_id'])
        if character_id is not None and scope.character_id != character_id:
            raise ValueError('processing archive scope mismatch')
        identity = (scope.character_id, scope.user_id)
        if identity in seen:
            raise ValueError('duplicate processing archive scope')
        seen.add(identity)
        incoming = item.get('state')
        allowed = {'turns', 'disputes', 'provider_hold', 'provider_recovery'}
        if document['version'] >= 2:
            allowed.add('resolved_disputes')
        if not isinstance(incoming, dict) or not set(incoming) <= allowed:
            raise ValueError('invalid processing state')
        current = load(db, scope)
        had_state = any(current.values())
        turns = incoming.get('turns', {})
        if not isinstance(turns, dict):
            raise ValueError('invalid processing exchanges')
        for key, candidate in turns.items():
            parts = json.loads(key)
            if not isinstance(parts, list) or len(parts) != 2 or not all(isinstance(p, str) for p in parts):
                raise ValueError('invalid processing exchange identity')
            if not db.execute('SELECT 1 FROM memory_evidence WHERE character_id=? AND user_id=? AND conversation_id=? AND turn_id=? LIMIT 1',
                              (*identity, *parts)).fetchone():
                continue  # Deleted original identity has no schedulable work.
            if not isinstance(candidate, dict) or candidate.get('state') not in {'active', 'pending', 'held', 'done', 'unavailable'}:
                raise ValueError('invalid processing exchange state')
            for field in ('total_attempts', 'resume_floor', 'recovery_generation', 'max_attempts'):
                if type(candidate.get(field, 0)) is not int or not 0 <= candidate.get(field, 0) <= 2**31-1:
                    raise ValueError('invalid processing attempts')
            if candidate.get('resume_floor', 0) > candidate.get('total_attempts', 0):
                raise ValueError('processing resume exceeds accumulated attempts')
            if not isinstance(candidate.get('reason', ''), str) or len(candidate.get('reason', '')) > 128:
                raise ValueError('invalid processing reason')
            for field in ('retry_at', 'resumed_at'):
                value = candidate.get(field, 0.)
                if type(value) not in (int, float) or not 0 <= value < 1e12:
                    raise ValueError('invalid processing timestamp')
            old = current['turns'].get(key, {})
            old_recovery = (old.get('recovery_generation', 0), old.get('resumed_at', 0.))
            new_recovery = (candidate.get('recovery_generation', 0), candidate.get('resumed_at', 0.))
            # A stale held archive cannot undo a later explicit local recovery.
            chosen = dict(old or candidate)
            if (old and old.get('state') != 'held' and candidate['state'] == 'held'
                    and not (old_recovery > new_recovery)):
                chosen.update(state='held', reason=candidate.get('reason', 'attempts_exhausted'))
            chosen['total_attempts'] = max(old.get('total_attempts', 0), candidate.get('total_attempts', 0))
            chosen['active_token'] = None
            if chosen.get('state') == 'active':
                chosen.update(state='pending', reason='interrupted')
            if (chosen.get('state') == 'pending' and chosen.get('max_attempts', 0)
                    and chosen['total_attempts']-chosen.get('resume_floor', 0) >= chosen['max_attempts']):
                chosen.update(state='held', reason='attempts_exhausted')
            current['turns'][key] = {k: v for k, v in chosen.items() if k in {
                'state', 'reason', 'total_attempts', 'resume_floor', 'recovery_generation', 'max_attempts', 'retry_at', 'resumed_at', 'active_token'}}
        disputes = incoming.get('disputes', {})
        if not isinstance(disputes, dict):
            raise ValueError('invalid processing disputes')
        for source, targets in disputes.items():
            if not isinstance(source, str) or not isinstance(targets, list) or not all(isinstance(v, str) for v in targets):
                raise ValueError('invalid disputed source')
            current['disputes'][source] = sorted(set(current['disputes'].get(source, [])) | set(targets))
        resolved = incoming.get('resolved_disputes', {})
        if not isinstance(resolved, dict):
            raise ValueError('invalid resolved disputes')
        for source, targets in resolved.items():
            parts = json.loads(source)
            if (not isinstance(parts, list) or len(parts) != 2 or not all(isinstance(part, str) and part for part in parts)
                    or not isinstance(targets, list) or not all(isinstance(target, str) for target in targets)):
                raise ValueError('invalid resolved dispute identity')
            key = json.dumps(parts, separators=(',', ':'))
            current['resolved_disputes'][key] = sorted(set(current['resolved_disputes'].get(key, [])) | set(targets))
        held = incoming.get('provider_hold')
        recovered = incoming.get('provider_recovery')
        if recovered:
            if (not isinstance(recovered, dict) or set(recovered) != {'key', 'at'}
                    or not isinstance(recovered['key'], str) or len(recovered['key']) > 256
                    or type(recovered['at']) not in (int, float) or not 0 <= recovered['at'] < 1e12):
                raise ValueError('invalid provider recovery')
            if not had_state:
                current['provider_recovery'] = recovered
        if held:
            if (not isinstance(held, dict) or not {'key', 'reason'} <= set(held) <= {'key', 'reason', 'at'}
                    or not all(isinstance(held[k], str) and len(held[k]) <= 256 for k in ('key', 'reason'))
                    or type(held.get('at', 0)) not in (int, float) or not 0 <= held.get('at', 0) < 1e12):
                raise ValueError('invalid provider hold')
            recovery = current.get('provider_recovery', {})
            if recovery.get('key') != held['key'] or recovery.get('at', 0) < held.get('at', 0):
                current.setdefault('provider_hold', held)
        save(db, scope, current)
    # A role-only archive may carry the old hold on a peer's fact. Reconcile
    # after all scopes are merged so archive order cannot undo a later verdict.
    for user_id in {user for _, user in seen}:
        states = [(MemoryScope(row['character_id'], user_id), load(db, MemoryScope(row['character_id'], user_id)))
                  for row in db.execute('SELECT character_id FROM memory_processing WHERE user_id=?', (user_id,)).fetchall()]
        resolved = {}
        for _, state in states:
            for key, targets in state['resolved_disputes'].items():
                resolved.setdefault(key, set()).update(targets)
        for scope, state in states:
            for source, targets in list(state['disputes'].items()):
                remaining = set(targets) - resolved.get(_dispute_source_key(db, scope.character_id, user_id, source), set())
                if remaining:
                    state['disputes'][source] = sorted(remaining)
                else:
                    del state['disputes'][source]
            save(db, scope, state)
