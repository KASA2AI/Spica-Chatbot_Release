"""Deterministic selection of original Home records, shared by all readers.

No generated summary or synthetic quotation: returned content/IDs/revisions are
untouched originals. Coverage may include omitted mechanical records, but only
returned originals can support a claim. Legacy events without a binding retain
their previous representation.
"""
import json


def is_home_business_fact(row):
    metadata = row.get('metadata', {})
    binding = metadata.get('event_binding', {})
    return (row.get('kind') == 'environment' and metadata.get('actor') == 'business'
            and row.get('source') == binding.get('kind') == 'home.wake'
            and bool(binding.get('event_id')))


def is_home_terminal(row):
    return (is_home_business_fact(row)
            and row['metadata']['event_binding'].get('phase') in {'completed', 'cancelled', 'paused'})


def home_turn_selection(rows):
    """Classify chronological rows of one scoped event for all memory readers.

    Only a generated expression occupies an automatic-expression slot. A
    failed request with no expression retains its evidence, not a speech slot.
    """
    def turn(row):
        return row['conversation_id'], row['turn_id']
    users = tuple(dict.fromkeys(turn(row) for row in rows if row['kind'] == 'user'))
    user_keys = set(users)
    automatic = list(dict.fromkeys(turn(row) for row in rows
        if row['kind'] == 'assistant_generated' and turn(row) not in user_keys))
    return users, set(automatic[-2:])


def home_projection(rows):
    groups, retained = {}, set()
    for row in rows:
        binding = row['metadata'].get('event_binding', {})
        if binding.get('kind') != 'home.wake' or not binding.get('event_id'):
            retained.add(row['id'])
            continue
        identity = row['character_id'], row['user_id'], binding['event_id']
        groups.setdefault(identity, []).append(row)
    for group in groups.values():
        group.sort(key=lambda row: row['id'])
        def turn(row):
            return row['conversation_id'], row['turn_id']
        users, recent_auto = home_turn_selection(group)
        user_turns = set(users)
        fact_turns = {turn(row) for row in group if is_home_business_fact(row) and row['status'] in {'active', 'redacted'}}
        previous_fact = None
        for row in group:
            kind, key = row['kind'], turn(row)
            if kind == 'system_event' and key in fact_turns:
                continue  # The owner's separately persisted facts replace its generation instructions.
            if is_home_business_fact(row):
                signature = json.dumps([row['metadata']['event_binding'], row['content']], sort_keys=True, ensure_ascii=False)
                if signature == previous_fact:
                    continue
                previous_fact = signature
            elif kind == 'assistant_generated' and key not in user_turns | recent_auto:
                if row['metadata'].get('generation_complete') is not False:
                    continue
            elif kind in {'delivery', 'playback'} and key not in user_turns | recent_auto:
                # Preserve failures/unknown outcomes; a successful repeated
                # receipt does not prove the person heard or woke up.
                if row['metadata'].get('outcome') == 'completed':
                    continue
            retained.add(row['id'])
    return [row for row in rows if row['id'] in retained]
