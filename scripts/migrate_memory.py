"""Local backup, legacy preview/confirmation and isolated memory restore.

No command activates a candidate, starts a service, or restores business tasks.
Run from the repository root with python -m scripts.migrate_memory --help.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile

from memory.evidence import EvidenceJournal
from memory.store import SQLiteMemoryStore
from memory.transfer import export_archive, prepare_restore
from spica.ports.memory import MemoryScope


def _read_only(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    return db


def legacy_candidates(path):
    with closing(_read_only(path)) as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM memories WHERE status='active' ORDER BY id")]
    for row in rows:
        row['fingerprint'] = hashlib.sha256(json.dumps(row,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        row['character_id'] = row['conversation_id'].split('::',1)[0] if '::' in row['conversation_id'] else None
        row['original_chat_available'] = False
        row['disposition'] = ('domain_only' if row['source'] == 'galgame_companion'
                              else 'requires_personal_confirmation')
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command',required=True)
    preview = commands.add_parser('preview',help='read legacy records without promoting them')
    preview.add_argument('--database',type=Path,required=True)
    backup = commands.add_parser('backup',help='consistent SQLite backup, including legacy tables')
    backup.add_argument('--database',type=Path,required=True)
    backup.add_argument('--output',type=Path,required=True)
    restore = commands.add_parser('restore',help='prepare a separate candidate with current withdrawal fences')
    restore.add_argument('--backup',type=Path,required=True)
    restore.add_argument('--current',type=Path)
    restore.add_argument('--candidate',type=Path,required=True)
    confirm = commands.add_parser('confirm-legacy',help='record explicitly confirmed content as present-day manual evidence')
    confirm.add_argument('--legacy-database',type=Path,required=True)
    confirm.add_argument('--target-database',type=Path,required=True)
    confirm.add_argument('--legacy-id',type=int,required=True)
    confirm.add_argument('--character',required=True)
    confirm.add_argument('--principal',default='owner')
    confirm.add_argument('--text',required=True)
    confirm.add_argument('--subject',choices=('user','relationship','character','project'),required=True)
    confirm.add_argument('--kind',choices=('fact','preference','state','episode'),required=True)
    args = parser.parse_args()
    if args.command == 'preview':
        result = {'candidates':legacy_candidates(args.database), 'activated':False}
    elif args.command == 'backup':
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('xb'):
            pass
        try:
            with closing(_read_only(args.database)) as source, closing(sqlite3.connect(args.output)) as target:
                source.backup(target)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('backup integrity check failed')
            result = {'backup':str(args.output),'sha256':hashlib.sha256(args.output.read_bytes()).hexdigest()}
        except Exception:
            args.output.unlink(missing_ok=True)
            raise
    elif args.command == 'restore':
        # Initialize/migrate the schema only in a temporary copy of the backup.
        with tempfile.TemporaryDirectory(prefix='spica-restore-') as temporary:
            staged = Path(temporary)/'backup.sqlite3'
            with closing(_read_only(args.backup)) as source, closing(sqlite3.connect(staged)) as target:
                source.backup(target)
            journal = EvidenceJournal(SQLiteMemoryStore(staged))
            result = prepare_restore(export_archive(journal),args.candidate,current=args.current or staged)
            result['withdrawal_fences_from'] = str(args.current or args.backup)
            if args.current is None:
                result['limitation'] = 'Only changes present in this backup are known; later deletions require a newer snapshot.'
    else:
        candidate = next((row for row in legacy_candidates(args.legacy_database) if row['id']==args.legacy_id),None)
        if candidate is None or candidate['character_id'] != args.character:
            raise ValueError('legacy candidate is absent or belongs to another character')
        if candidate['disposition'] == 'domain_only':
            raise ValueError('galgame history must remain in the game memory scope')
        journal = EvidenceJournal(SQLiteMemoryStore(args.target_database))
        entry = journal.remember(MemoryScope(args.character,args.principal),args.text,kind=args.kind,subject=args.subject,
                                 provenance={'fingerprint':candidate['fingerprint'],'legacy_id':candidate['id'],
                                             'legacy_recorded_at':candidate['created_at']})
        result = {'memory_id':entry,'source':'manual_confirmation','original_chat_available':False}
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
