"""Personal conversation evidence in the existing memory SQLite database.

The journal stores observations, not business tasks. Character and authenticated
principal own the data; conversation identifies the entry where it happened.
"""

from __future__ import annotations

from contextlib import closing
from concurrent.futures import CancelledError
import hashlib
import json
import re
import time
from datetime import datetime
from typing import Any, Callable
from threading import Event
from uuid import uuid4, uuid5, NAMESPACE_URL

from memory.store import SQLiteMemoryStore
from memory import processing
from spica.ports.memory import EvidenceAdmissionError, MemoryScope


# Ordinary pauses keep the latest exchange. After a longer gap, dated notes
# and relevant memories provide continuity; original exchanges remain readable.
_WORKING_CONTEXT_IDLE_SECONDS = 6 * 3600


def historical_query(query: str) -> bool:
    """An explicit past-time question may use dated, no-longer-current records."""
    if re.search(r'以前|过去|曾经|最初|当时|之前|后来|改变|変わ|昔|かつて|前に|'
                 r'昨天|昨晚|前天|昨日|昨夜|一昨日|上周|上个月|去年|先週|先月|昨年|'
                 r'\d+\s*(?:天|日|周|週間|个月|か月|年)前|'
                 r'\b(?:yesterday|previously|last\s+(?:night|week|month|year))\b', query, re.I):
        return True
    today = datetime.now().astimezone().date()
    for year, month, day in re.findall(r'(?<!\d)(\d{4})(?:[-/.]|年)(\d{1,2})(?:[-/.]|月)(\d{1,2})(?:日)?(?!\d)', query):
        try:
            if datetime(int(year), int(month), int(day)).date() < today:
                return True
        except ValueError:
            continue
    return False


class EvidenceJournal:
    def __init__(self, store: SQLiteMemoryStore, *, shared_fact_characters: tuple[str, ...] = (),
                 semantic_scores: Callable[..., dict[int, float]] | None = None) -> None:
        self.store = store
        self.shared_fact_characters = tuple(shared_fact_characters)
        self._semantic_scores = semantic_scores
        with closing(store._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS memory_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    character_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL, event_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1,
                    kind TEXT NOT NULL, source TEXT NOT NULL, modality TEXT NOT NULL,
                    content TEXT NOT NULL, occurred_at REAL, recorded_at REAL NOT NULL,
                    metadata TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                    UNIQUE(character_id, user_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS memory_evidence_scope
                    ON memory_evidence(character_id, user_id, id);
                CREATE TABLE IF NOT EXISTS memory_processing (
                    character_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0,
                    passive_version INTEGER NOT NULL DEFAULT 0,
                    processed_id INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(character_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS memory_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    character_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    kind TEXT NOT NULL, semantic_key TEXT NOT NULL, content TEXT NOT NULL,
                    origin TEXT NOT NULL, sources TEXT NOT NULL,
                    subject TEXT NOT NULL DEFAULT 'user',
                    status TEXT NOT NULL DEFAULT 'active',
                    revision INTEGER NOT NULL, revision_reason TEXT,
                    replaces TEXT NOT NULL DEFAULT '[]',
                    recorded_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS memory_entries_scope
                    ON memory_entries(character_id,user_id,status);
                CREATE TABLE IF NOT EXISTS memory_exclusions (
                    evidence_id INTEGER NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
                    reason TEXT NOT NULL, recorded_at REAL NOT NULL,
                    PRIMARY KEY(evidence_id,start,end,reason)
                );
                CREATE TABLE IF NOT EXISTS memory_context (
                    character_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    through_id INTEGER NOT NULL, notes TEXT NOT NULL,
                    PRIMARY KEY(character_id,user_id)
                );
                CREATE TABLE IF NOT EXISTS memory_context_resets (
                    character_id TEXT NOT NULL, user_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
                    through_id INTEGER NOT NULL,
                    PRIMARY KEY(character_id,user_id,conversation_id)
                );
            """)
            processing_columns = {row['name'] for row in db.execute('PRAGMA table_info(memory_processing)')}
            if 'passive_version' not in processing_columns:
                db.execute('ALTER TABLE memory_processing ADD COLUMN passive_version INTEGER NOT NULL DEFAULT 0')
            if 'processing_state' not in processing_columns:
                db.execute("ALTER TABLE memory_processing ADD COLUMN processing_state TEXT NOT NULL DEFAULT '{}'")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(memory_entries)")}
            if "subject" not in columns:
                db.execute("ALTER TABLE memory_entries ADD COLUMN subject TEXT NOT NULL DEFAULT 'user'")
            if "replaces" not in columns:
                db.execute("ALTER TABLE memory_entries ADD COLUMN replaces TEXT NOT NULL DEFAULT '[]'")
            for column in ("valid_from", "valid_until"):
                if column not in columns:
                    db.execute(f"ALTER TABLE memory_entries ADD COLUMN {column} REAL")
            if 'uid' not in columns:
                db.execute('ALTER TABLE memory_entries ADD COLUMN uid TEXT')
            for row in db.execute('SELECT id,character_id,user_id,semantic_key,recorded_at FROM memory_entries WHERE uid IS NULL').fetchall():
                # Two backups of a pre-UID store must assign the same identity
                # even if one has since withdrawn the content/source quotes.
                identity = json.dumps(dict(row),sort_keys=True,ensure_ascii=False)
                db.execute('UPDATE memory_entries SET uid=? WHERE id=?', (uuid5(NAMESPACE_URL,identity).hex, row['id']))
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS memory_entries_uid ON memory_entries(uid)')
            db.execute('''CREATE TRIGGER IF NOT EXISTS memory_entries_assign_uid AFTER INSERT ON memory_entries
                WHEN NEW.uid IS NULL BEGIN
                UPDATE memory_entries SET uid=lower(hex(randomblob(16))) WHERE id=NEW.id;
                END''')
            evidence_columns = {row["name"] for row in db.execute("PRAGMA table_info(memory_evidence)")}
            if "turn_id" not in evidence_columns:
                db.execute("ALTER TABLE memory_evidence ADD COLUMN turn_id TEXT NOT NULL DEFAULT ''")
            if "revision" not in evidence_columns:
                db.execute("ALTER TABLE memory_evidence ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
            if "organized" not in evidence_columns:
                db.execute("ALTER TABLE memory_evidence ADD COLUMN organized INTEGER NOT NULL DEFAULT 0")
                db.execute("""UPDATE memory_evidence SET organized=1 WHERE id <= COALESCE((
                    SELECT processed_id FROM memory_processing p WHERE p.character_id=memory_evidence.character_id
                    AND p.user_id=memory_evidence.user_id),0)""")
            reset_columns = {row["name"] for row in db.execute("PRAGMA table_info(memory_context_resets)")}
            if "reset_at" not in reset_columns:
                db.execute("ALTER TABLE memory_context_resets ADD COLUMN reset_at REAL NOT NULL DEFAULT 0")
                db.execute("""UPDATE memory_context_resets SET reset_at=COALESCE((
                    SELECT MAX(recorded_at) FROM memory_evidence e WHERE e.character_id=memory_context_resets.character_id
                    AND e.user_id=memory_context_resets.user_id AND e.id<=memory_context_resets.through_id),0)""")
            for row in db.execute("SELECT id,event_id,metadata FROM memory_evidence WHERE turn_id=''").fetchall():
                db.execute("UPDATE memory_evidence SET turn_id=? WHERE id=?", (self._turn_key(row), row["id"]))
            db.execute("CREATE INDEX IF NOT EXISTS memory_evidence_turn ON memory_evidence(character_id,user_id,turn_id)")
            db.execute('CREATE INDEX IF NOT EXISTS memory_evidence_event ON memory_evidence(user_id,event_id)')
            # Older repeated withdrawals recorded empty ranges for an already
            # erased reply. They exclude no text; real fences remain intact.
            db.execute('DELETE FROM memory_exclusions WHERE start=end AND start>=0')
            self._repair_tool_dependencies(db)

    def _visibility(self, scope: MemoryScope) -> tuple[str, tuple]:
        if scope.character_id not in self.shared_fact_characters:
            return "character_id=? AND user_id=?", (scope.character_id, scope.user_id)
        slots = ",".join("?" for _ in self.shared_fact_characters)
        return ("user_id=? AND (character_id=? OR (character_id IN (" + slots
                + ") AND subject='user' AND origin='direct' AND kind IN ('fact','preference')))",
                (scope.user_id, scope.character_id, *self.shared_fact_characters))

    def pending_shared_inputs(self, scope: MemoryScope, entries: list[dict], *,
                              include_own=False, only_source_id=None) -> tuple[list[dict], dict[int, float]]:
        """Local admission checks only; peer originals never enter a role view."""
        if (scope.character_id not in self.shared_fact_characters and not include_own) or not entries:
            return [], {}
        characters = self.shared_fact_characters if scope.character_id in self.shared_fact_characters else (scope.character_id,)
        slots = ','.join('?' for _ in characters)
        source_ids = {source['id'] for entry in entries for source in entry['sources']}
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN')
            times = dict(db.execute('''SELECT id,recorded_at FROM memory_evidence WHERE user_id=?
                AND id IN (SELECT value FROM json_each(?))''', (scope.user_id, json.dumps(sorted(source_ids)))))
            rows = db.execute("""SELECT * FROM memory_evidence WHERE user_id=? AND (? OR character_id!=?)
                AND character_id IN (""" + slots + """) AND kind='user' AND organized=0
                AND status IN ('active','redacted') AND (? IS NULL OR id=?) ORDER BY id""",
                (scope.user_id, include_own, scope.character_id, *characters, only_source_id, only_source_id)).fetchall()
            pending = self._with_exclusions(db, rows)
            for original in pending:
                references = set()
                replies = list(db.execute("""SELECT metadata FROM memory_evidence WHERE character_id=?
                    AND user_id=? AND conversation_id=? AND turn_id=? AND kind='assistant_generated'
                    AND status='active'""", (original['character_id'], scope.user_id,
                                             original['conversation_id'], original['turn_id'])))
                if self.correction_text(original['content']):
                    previous = db.execute("""SELECT metadata FROM memory_evidence WHERE character_id=?
                        AND user_id=? AND conversation_id=? AND kind='assistant_generated' AND status='active'
                        AND recorded_at<=? AND recorded_at>? ORDER BY recorded_at DESC,id DESC LIMIT 1""",
                        (original['character_id'], scope.user_id, original['conversation_id'],
                         original['recorded_at'], original['recorded_at'] - _WORKING_CONTEXT_IDLE_SECONDS)).fetchone()
                    if previous is not None:
                        replies.append(previous)
                for reply in replies:
                    references.update(json.loads(reply['metadata']).get('memory_entry_ids', []))
                original['referenced_entry_ids'] = references
        support_times = {entry['id']: max((times.get(source['id'], 0) for source in entry['sources']),
                                          default=entry['recorded_at']) for entry in entries}
        return [row for row in pending if row['content'].replace('█', '').strip()], support_times

    @staticmethod
    def correction_text(text):
        return bool(re.search(r'记错|记反|更正|纠正|不对|不是这样|不是的|刚才.{0,8}(?:错了|错误)|訂正|違い|違う|違います|誤り|\b(?:correction|incorrect|wrong|actually)\b', text, re.I))

    def pending_related_entry_ids(self, original, entries, *, confirmed_only=False, publish=None):
        """One local relation check for intake, read-time holds and retention."""
        related = {entry['id'] for entry in entries if entry['id'] in original['referenced_entry_ids']}
        if not entries:
            return related
        try:
            if self._semantic_scores is None:
                raise RuntimeError('memory semantic selection unavailable')
            scores = self._semantic_scores(original['content'], entries,
                **({'publish': publish} if publish is not None else {}))
        except Exception:
            # Index uncertainty can temporarily withhold a fact, but cannot
            # establish a durable dispute against every unrelated entry.
            return related if confirmed_only else {entry['id'] for entry in entries}
        keywords = self.store._keywords(original['content'])
        for entry in entries:
            hits = any(word in self.store._normalize_for_search(entry['content']) for word in keywords)
            score = scores.get(entry['id'], 0.)
            if score >= .42 or hits and score >= .28:
                related.add(entry['id'])
        return related

    def pending_entry_relations(self, scope, entries, *, include_own=False, only_source_id=None,
                                confirmed_only=False, publish=None):
        """Choose correction targets once for intake, read-time holds and expiry.

        Reads may temporarily withhold uncertain peer updates; durable callers
        require confirmed relations. Neither mode grants access to peer prose.
        include_own admits ordinary statements as updates to peer facts; facts
        from the same role still require an explicit correction.
        """
        shared = [entry for entry in entries if entry['character_id'] == scope.character_id
            or (entry['character_id'] in self.shared_fact_characters and entry['subject'] == 'user'
                and entry['origin'] == 'direct' and entry['kind'] in {'fact', 'preference'})]
        pending, support_times = self.pending_shared_inputs(scope, shared,
            include_own=True, only_source_id=only_source_id)
        relations = []
        for original in pending:
            own = original['character_id'] == scope.character_id
            correction = self.correction_text(original['content'])
            if own and not correction and not include_own:
                continue
            eligible = [entry for entry in shared if support_times[entry['id']] < original['recorded_at']
                and (not own or correction or entry['character_id'] != scope.character_id)]
            related = self.pending_related_entry_ids(original, eligible,
                confirmed_only=confirmed_only, publish=publish)
            if related:
                relations.append((original, related))
        return relations

    def record_dispute(self, scope, source_id, entry_ids):
        if not entry_ids:
            return
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._record_dispute(db, scope, source_id, entry_ids)

    def _record_dispute(self, db, scope, source_id, entry_ids, *, expected_revision=None):
        original = db.execute("SELECT event_id,revision,organized FROM memory_evidence WHERE id=? AND character_id=? AND user_id=? AND kind='user' AND status IN ('active','redacted')",
                              (source_id, scope.character_id, scope.user_id)).fetchone()
        if (original is None or expected_revision is not None
                and (original['revision'] != expected_revision or original['organized'])):
            return
        visibility, params = self._visibility(scope)
        targets = db.execute('SELECT uid FROM memory_entries WHERE ' + visibility
            + " AND status IN ('active','superseded') AND id IN (SELECT value FROM json_each(?))",
            (*params, json.dumps(sorted(entry_ids)))).fetchall()
        if not targets:
            return
        state = processing.load(db, scope)
        state['disputes'][original['event_id']] = sorted(set(state['disputes'].get(original['event_id'], [])) | {row[0] for row in targets})
        processing.save(db, scope, state)

    def disputed_entry_ids(self, scope):
        with closing(self.store._connect()) as db:
            states = db.execute('SELECT processing_state FROM memory_processing WHERE user_id=?', (scope.user_id,)).fetchall()
            targets = {uid for row in states for values in json.loads(row[0]).get('disputes', {}).values() for uid in values}
            visibility, params = self._visibility(scope)
            return {row[0] for row in db.execute('SELECT id FROM memory_entries WHERE ' + visibility
                + " AND status IN ('active','superseded') AND uid IN (SELECT value FROM json_each(?))",
                (*params, json.dumps(sorted(targets))))}

    def append(
        self, scope: MemoryScope, *, event_id: str, kind: str, content: str,
        source: str, modality: str = "text", occurred_at: float | None = None,
        metadata: dict[str, Any] | None = None,
        reject_withdrawn: bool = False,
    ) -> int:
        if not all(isinstance(v, str) and v.strip() for v in (
            scope.character_id, scope.user_id, event_id, content, source,
        )):
            raise ValueError("evidence requires scope, event identity, content and source")
        if kind not in {"user", "assistant_generated", "delivery", "playback", "tool", "environment", "manual", "system_event"}:
            raise ValueError("invalid evidence kind")
        if modality not in {"text", "speech", "system", "tool"}:
            raise ValueError("invalid evidence modality")
        owner = (scope.character_id, scope.user_id)
        metadata = metadata or {}
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM memory_evidence WHERE character_id=? AND user_id=? AND event_id=?",
                (*owner, event_id),
            ).fetchone()
            if existing is not None:
                # Redacted/withdrawn input never gets restored by a transport
                # retry. A reused live ID with different content is a conflict.
                if existing["status"] == "active" and (
                    existing["content"] != content or existing["kind"] != kind
                    or existing["source"] != source
                    or existing['modality'] != modality
                    or existing["conversation_id"] != (scope.conversation_id or "default")
                ):
                    raise EvidenceAdmissionError("evidence event identity already has different content")
                if reject_withdrawn and (existing["status"] != "active" or db.execute(
                        "SELECT 1 FROM memory_exclusions WHERE evidence_id=?", (existing["id"],)).fetchone()):
                    raise EvidenceAdmissionError("the replayed evidence was withdrawn")
                return int(existing["id"])
            status = "active"
            if kind in {"assistant_generated", "delivery", "playback", "tool", "environment"}:
                if self._dependencies_withdrawn(db, metadata):
                    # A result generated before deletion can arrive afterwards.
                    # Retain its event identity, never republish its old text.
                    content, metadata, status = "", self._receipt_metadata(metadata), "deleted"
            result = db.execute(
                """INSERT INTO memory_evidence
                   (character_id,user_id,conversation_id,event_id,kind,source,modality,
                    content,occurred_at,recorded_at,metadata,turn_id,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*owner, scope.conversation_id or "default", event_id, kind, source, modality,
                 content, occurred_at, time.time(), json.dumps(metadata or {}, ensure_ascii=False),
                 self._turn_key({"event_id": event_id, "metadata": metadata}), status),
            )
            # New generated speech/receipts cannot revise an older source.
            # Keep their append counter separate so a frozen processing batch
            # can finish while these events arrive. User/manual/tool evidence,
            # publication, restore, deletion and correction still fence it.
            passive = int(kind in {'system_event', 'assistant_generated', 'playback', 'delivery', 'environment'})
            db.execute(
                """INSERT INTO memory_processing(character_id,user_id,version,passive_version) VALUES (?,?,1,?)
                   ON CONFLICT(character_id,user_id) DO UPDATE SET version=version+1,
                   passive_version=passive_version+excluded.passive_version""", (*owner, passive),
            )
            return int(result.lastrowid)

    @staticmethod
    def _dependencies_withdrawn(db, metadata: dict) -> bool:
        dependencies = set(metadata.get("context_evidence_ids", []))
        dependencies.update((metadata.get("input_evidence_id"), metadata.get("generated_evidence_id")))
        seen = metadata.get("context_evidence_revisions", {})
        for source_id in dependencies - {None}:
            dependency = db.execute("SELECT revision,status FROM memory_evidence WHERE id=?", (source_id,)).fetchone()
            expected = seen.get(str(source_id), seen.get(source_id))
            if dependency is not None and (dependency["status"] in {"deleted", "retracted"}
                    or expected is not None and expected != dependency["revision"]
                    or expected is None and db.execute("SELECT 1 FROM memory_exclusions WHERE evidence_id=?", (source_id,)).fetchone()):
                return True
        return any(db.execute("SELECT 1 FROM memory_entries WHERE id=? AND status IN ('deleted','retracted')", (entry_id,)).fetchone()
                   for entry_id in metadata.get("memory_entry_ids", []))

    def _pending_raw_corrections(self, db, scope):
        """Use already captured prompt dependencies for explicit short corrections."""
        owner = scope.character_id, scope.user_id
        rows = db.execute("SELECT * FROM memory_evidence WHERE character_id=? AND user_id=? AND kind='user' AND organized=0 AND status IN ('active','redacted')", owner).fetchall()
        links = []
        for correction in self._with_exclusions(db, rows):
            if not self.correction_text(correction['content']):
                continue
            replies = list(db.execute("SELECT metadata FROM memory_evidence WHERE character_id=? AND user_id=? AND conversation_id=? AND turn_id=? AND kind='assistant_generated' AND status='active'",
                (*owner, correction['conversation_id'], correction['turn_id'])))
            # The reply being corrected remains an anchor after this turn's
            # answer is saved. Its earlier sources may no longer fit in the
            # correction turn's recent window.
            previous = db.execute("SELECT metadata,status FROM memory_evidence WHERE character_id=? AND user_id=? AND conversation_id=? AND kind='assistant_generated' AND id<? AND recorded_at>? ORDER BY id DESC LIMIT 1",
                (*owner, correction['conversation_id'], correction['id'], correction['recorded_at']-_WORKING_CONTEXT_IDLE_SECONDS)).fetchone()
            if previous is not None and previous['status'] == 'active':
                replies.append(previous)
            refs = set()
            for reply in replies:
                metadata = json.loads(reply['metadata'])
                refs.update(metadata.get('context_evidence_ids', []))
                if metadata.get('input_evidence_id') is not None:
                    refs.add(metadata['input_evidence_id'])
            # These are the sources actually supplied to the corrected reply
            # and its correction. Recursing through every recent-context edge
            # would treat the entire conversation as this correction's subject.
            targets = list(db.execute("SELECT id,conversation_id,turn_id FROM memory_evidence WHERE character_id=? AND user_id=? AND id<? AND id IN (SELECT value FROM json_each(?)) AND status IN ('active','redacted')",
                (*owner, correction['id'], json.dumps(sorted(refs)))))
            if targets:
                links.append((correction, targets))
        return links

    def context_is_current(self, scope: MemoryScope, dependencies: dict) -> bool:
        """Selected originals/records remain usable, including valid true history.

        This intentionally ignores unrelated scope-version changes. A missing or
        out-of-scope source cannot authorize re-sending its captured text.
        """
        source_ids = set(dependencies.get('context_evidence_ids', []))
        source_ids.update(value for value in (dependencies.get('input_evidence_id'),
                          dependencies.get('generated_evidence_id')) if value is not None)
        visibility, params = self._visibility(scope)
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN')
            for source_id in source_ids:
                if not db.execute('SELECT 1 FROM memory_evidence WHERE id=? AND character_id=? AND user_id=?',
                                  (source_id, scope.character_id, scope.user_id)).fetchone():
                    return False
            for entry_id in dependencies.get('memory_entry_ids', []):
                if not db.execute("SELECT 1 FROM memory_entries WHERE id=? AND " + visibility
                                  + " AND status IN ('active','superseded')", (entry_id, *params)).fetchone():
                    return False
            links = self._pending_raw_corrections(db, scope)
            if links:
                selected, remaining = set(), set(source_ids)
                while remaining:
                    rows = db.execute('SELECT metadata FROM memory_evidence WHERE character_id=? AND user_id=? AND id IN (SELECT value FROM json_each(?))',
                                      (scope.character_id, scope.user_id, json.dumps(sorted(remaining)))).fetchall()
                    selected.update(remaining)
                    remaining = set()
                    for row in rows:
                        metadata = json.loads(row['metadata'])
                        refs = set(metadata.get('context_evidence_ids', []))
                        refs.update(value for value in (metadata.get('input_evidence_id'), metadata.get('generated_evidence_id')) if value is not None)
                        remaining.update(refs-selected)
                if any({row['id'] for row in targets} & selected and correction['id'] not in selected
                       for correction, targets in links):
                    return False
            return not self._dependencies_withdrawn(db, dependencies)

    def _repair_tool_dependencies(self, db) -> None:
        """Recover pre-dependency tool records on opening/restoring an old store."""
        stale: dict[tuple[str, str], dict[int, list[tuple[int, int]]]] = {}
        for row in db.execute("SELECT * FROM memory_evidence WHERE kind IN ('tool','environment') AND status='active'").fetchall():
            metadata = json.loads(row['metadata'])
            owner = (row['character_id'], row['user_id'])
            identity = (*owner, row['conversation_id'], row['turn_id'])
            legacy = metadata.get('input_evidence_id') is None
            reply = None
            if legacy:
                original = db.execute("""SELECT id FROM memory_evidence WHERE character_id=? AND user_id=?
                    AND conversation_id=? AND turn_id=? AND kind IN ('user','system_event') ORDER BY id LIMIT 1""", identity).fetchone()
                if original is not None:
                    metadata['input_evidence_id'] = original['id']
                reply = db.execute("""SELECT metadata,status FROM memory_evidence WHERE character_id=? AND user_id=?
                    AND conversation_id=? AND turn_id=? AND kind='assistant_generated' ORDER BY id LIMIT 1""", identity).fetchone()
                if reply is not None:
                    prior = json.loads(reply['metadata'])
                    for field in ('context_evidence_ids', 'context_evidence_revisions', 'memory_entry_ids'):
                        if field not in metadata and field in prior:
                            metadata[field] = prior[field]
                    metadata['context_evidence_ids'] = [value for value in metadata.get('context_evidence_ids', [])
                                                        if value < row['id']]
                    metadata['context_evidence_revisions'] = {key: value for key, value in
                        metadata.get('context_evidence_revisions', {}).items() if int(key) < row['id']}
                if metadata != json.loads(row['metadata']):
                    db.execute('UPDATE memory_evidence SET metadata=? WHERE id=?', (json.dumps(metadata), row['id']))
            if (self._dependencies_withdrawn(db, metadata)
                    or reply is not None and reply['status'] in {'deleted', 'retracted'}):
                if row['content']:
                    stale.setdefault(owner, {})[row['id']] = [(0, len(row['content']))]
                else:
                    db.execute("UPDATE memory_evidence SET metadata=?,status='deleted' WHERE id=?",
                               (json.dumps(self._receipt_metadata(metadata)), row['id']))
        for (character, user), spans in stale.items():
            self._withdraw(db, MemoryScope(character, user), [], reason='deleted', delete_source=True,
                           evidence_spans=spans)

    @staticmethod
    def _receipt_metadata(metadata: dict, *, answer_text: str | None = None) -> dict:
        # Reconcile already-issued replies without retaining their old text.
        metadata = dict(metadata)
        if answer_text and metadata.get('generation_complete'):
            metadata.setdefault('answer_sha256', hashlib.sha256(answer_text.encode()).hexdigest())
            # QQ stores its validated visible text with normalized line ends.
            visible = answer_text.replace('\r\n', '\n').replace('\r', '\n').strip()
            metadata.setdefault('answer_text_sha256', hashlib.sha256(visible.encode()).hexdigest())
        fields = ('turn_id', 'runtime_turn_id', 'reply_binding', 'answer_sha256',
                  'answer_text_sha256', 'memory_domain',
                  'generation_complete', 'input_evidence_id', 'generated_evidence_id',
                  'outcome', 'message_id', 'authority', 'basis', 'endpoint', 'audio_units')
        return {key: metadata[key] for key in fields if key in metadata}

    def read(self, scope: MemoryScope, *, ids: list[int] | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if ids == []:
            return []
        params: list[Any] = [scope.character_id, scope.user_id]
        selection = ""
        if ids is not None:
            selection = " AND id IN (" + ",".join("?" for _ in ids) + ")"
            params.extend(ids)
        with closing(self.store._connect()) as db:
            rows = db.execute(
                "SELECT * FROM memory_evidence WHERE character_id=? AND user_id=?"
                + selection + " ORDER BY id DESC LIMIT ?", (*params, max(1, limit)),
            ).fetchall()
            return self._with_exclusions(db, reversed(rows))

    def _with_exclusions(self, db, rows) -> list[dict]:
        results = []
        for row in rows:
            value = self._row(row)
            value["recorded_local"] = datetime.fromtimestamp(row["recorded_at"]).astimezone().isoformat()
            value["exclusions"] = [dict(item) for item in db.execute(
                "SELECT start,end,reason FROM memory_exclusions WHERE evidence_id=?", (row["id"],))]
            for excluded in sorted(value["exclusions"], key=lambda item: item["start"], reverse=True):
                start, end = excluded["start"], min(excluded["end"], len(value["content"]))
                if start < end:
                    value["content"] = value["content"][:start] + "█" * (end - start) + value["content"][end:]
            results.append(value)
        return results

    def pending_scopes(self, *, include_held=False) -> list[MemoryScope]:
        with closing(self.store._connect()) as db:
            rows = db.execute("""SELECT p.character_id,p.user_id FROM memory_processing p
                WHERE EXISTS (SELECT 1 FROM memory_evidence e
                    WHERE e.character_id=p.character_id AND e.user_id=p.user_id AND e.organized=0)
            """).fetchall()
        scopes = [MemoryScope(row["character_id"], row["user_id"]) for row in rows]
        return scopes if include_held else [scope for scope in scopes if self.pending_work(scope)['first_id']]

    def status(self, scope: MemoryScope) -> dict[str, int]:
        with closing(self.store._connect()) as db:
            row = db.execute("""SELECT version,processed_id,
                (SELECT COUNT(*) FROM memory_evidence e WHERE e.character_id=p.character_id
                 AND e.user_id=p.user_id AND e.organized=0) AS pending
                FROM memory_processing p WHERE character_id=? AND user_id=?""",
                (scope.character_id, scope.user_id)).fetchone()
            originals = db.execute('SELECT conversation_id,turn_id FROM memory_evidence WHERE character_id=? AND user_id=? AND organized=0',
                                   (scope.character_id, scope.user_id)).fetchall()
            work = processing.status(db, scope, originals)
        return {**(dict(row) if row is not None else {"version": 0, "processed_id": 0, "pending": 0}), **work}

    def pending_work(self, scope: MemoryScope) -> dict[str, Any]:
        """Count persisted new material, not playback notifications or retries."""
        with closing(self.store._connect()) as db:
            rows = db.execute('''SELECT * FROM memory_evidence
                WHERE character_id=? AND user_id=? AND organized=0 ORDER BY id''',
                (scope.character_id, scope.user_id)).fetchall()
            state = processing.load(db, scope)
            rows = processing.eligible(rows, state)
            from memory.event_projection import home_projection, is_home_terminal
            projected = home_projection(self._with_exclusions(db, rows))
        usable = [row for row in projected if row['status'] in {'active', 'redacted'} and row['content'].replace('█', '').strip()]
        priority_turns = {processing.exchange_key(row) for row in usable if is_home_terminal(row)
            or row['kind'] in {'user', 'manual'} and (row['metadata'].get('revision_reason')
                or self.correction_text(row['content']) or re.search(
                r"测试结束|实验结束|只是测试|只是实验|别再催|今天不看|以后再说|テスト.{0,5}終", row['content']))}
        turns = list(dict.fromkeys(processing.exchange_key(row) for row in rows))
        # Completion covers the selected records, not future receipts of the
        # same exchange. Retain its admission and accumulated attempt budget.
        admitted = {key for key in turns if state['turns'].get(key, {}).get('state') in {'active', 'pending', 'done'}}
        # Frozen work retains its own eligibility across restarts. Its old
        # count, size and age cannot admit a newly arrived small exchange.
        fresh = [row for row in usable if processing.exchange_key(row) not in admitted]
        return {'first_id': rows[0]['id'] if rows else 0, 'last_id': rows[-1]['id'] if rows else 0,
                'turns': turns, 'priority_turns': priority_turns,
                'priority_immediate': any(not state['turns'].get(key, {}).get('reason') for key in priority_turns),
                'admitted_turns': admitted,
                'last_recorded_at': max((row['recorded_at'] for row in rows), default=0),
                'oldest_user_at': min((row['recorded_at'] for row in fresh if row['kind'] == 'user'), default=0),
                'retry_at': max((state['turns'].get(processing.exchange_key(row), {}).get('retry_at', 0.) for row in rows), default=0.),
                'user_turns': len({(row['conversation_id'], row['turn_id']) for row in fresh if row['kind'] == 'user'}),
                'estimated_tokens': sum(self._tokens(row['content']) for row in fresh
                    if row['kind'] not in {'playback', 'delivery'})}

    def snapshot(self, scope: MemoryScope, *, token_budget: int = 12000,
                 through_id: int | None = None, turn_keys: set[str] | None = None, publish=None) -> dict[str, Any] | None:
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN")
            state = db.execute("SELECT * FROM memory_processing WHERE character_id=? AND user_id=?",
                               (scope.character_id, scope.user_id)).fetchone()
            if state is None:
                return None
            rows = db.execute("""SELECT * FROM memory_evidence WHERE character_id=? AND user_id=?
                AND organized=0 AND (? IS NULL OR id<=?) ORDER BY id""",
                (scope.character_id, scope.user_id, through_id, through_id)).fetchall()
            rows = processing.eligible(rows, processing.load(db, scope))
            if turn_keys is not None:
                rows = [row for row in rows if processing.exchange_key(row) in turn_keys]
            if not rows:
                return None
            # Bound each request by whole admitted turns, including all tool
            # results. A single large turn remains whole and is never sliced.
            selected, siblings, used = [], [], 0
            raw_links = self._pending_raw_corrections(db, scope)
            def turn_key(row):
                return row['conversation_id'], self._turn_key(row)
            exchanges = {}
            for row in rows:
                exchanges.setdefault(turn_key(row), []).append(row)
            for exchange in exchanges.values():
                # A late tool/delivery result may follow an already organized
                # input. Give the organizer that whole original exchange too;
                # only the new records belong to this processing coverage.
                earlier = []
                for conversation, turn in dict.fromkeys(turn_key(row) for row in exchange):
                    earlier.extend(db.execute("""SELECT * FROM memory_evidence WHERE character_id=?
                        AND user_id=? AND conversation_id=? AND turn_id=? AND organized=1 ORDER BY id""",
                        (scope.character_id, scope.user_id, conversation, turn)).fetchall())
                # A paused old turn can still explain a new short correction.
                # It is supporting material, never counted as newly covered.
                exchange_ids = {row['id'] for row in exchange}
                for correction, targets in raw_links:
                    if correction['id'] not in exchange_ids:
                        continue
                    for conversation, turn in {(row['conversation_id'], row['turn_id']) for row in targets}:
                        earlier.extend(db.execute("SELECT * FROM memory_evidence WHERE character_id=? AND user_id=? AND conversation_id=? AND turn_id=? AND status IN ('active','redacted') ORDER BY id",
                            (scope.character_id, scope.user_id, conversation, turn)).fetchall())
                present_ids = exchange_ids | {row['id'] for row in [*selected, *siblings]}
                earlier = list({row['id']: row for row in earlier if row['id'] not in present_ids}.values())
                from memory.event_projection import home_projection
                projected = home_projection(self._with_exclusions(db, [*selected, *siblings, *exchange, *earlier]))
                cost = self._tokens(json.dumps(projected, ensure_ascii=False))
                if cost > token_budget // 2 and selected:
                    break
                selected.extend(exchange)
                siblings.extend(earlier)
                used = cost
            rows = sorted(selected, key=lambda row: row['id'])
            visibility, params = self._visibility(scope)
            entries = db.execute("SELECT * FROM memory_entries WHERE " + visibility
                                 + " AND status IN ('active','superseded') ORDER BY id", params).fetchall()
            entries = self._visible_entries(db, entries, scope)
            for entry in entries:
                entry.pop('uid', None)  # internal portable identity, not model output
                for column in ('valid_from', 'valid_until'):
                    if entry.get(column) is not None:
                        entry[column] = datetime.fromtimestamp(entry[column]).astimezone().isoformat()
            context = db.execute("SELECT notes FROM memory_context WHERE character_id=? AND user_id=?",
                                 (scope.character_id, scope.user_id)).fetchone()
            notes = json.loads(context[0]) if context else []
            support_ids = sorted({source["id"] for entry in [*entries, *notes] for source in entry["sources"]}
                                 - {row["id"] for row in rows})
            support = []
            if support_ids:
                support = db.execute("SELECT * FROM memory_evidence WHERE character_id=? AND user_id=? AND id IN ("
                                     + ",".join("?" for _ in support_ids) + ")",
                                     (scope.character_id, scope.user_id, *support_ids)).fetchall()
            guarded_characters = self.shared_fact_characters if scope.character_id in self.shared_fact_characters else (scope.character_id,)
            versions, content_versions = {}, {}
            for character in guarded_characters:
                current = db.execute("SELECT version,passive_version FROM memory_processing WHERE character_id=? AND user_id=?",
                                     (character, scope.user_id)).fetchone()
                versions[character] = current[0] if current else 0
                content_versions[character] = current[0] - current[1] if current else 0
            snapshot = {"version": state["version"], "covered_ids": [row["id"] for row in rows],
                        "evidence": home_projection(self._with_exclusions(db, rows)), "existing_entries": [],
                        "working_summary": [], "supporting_evidence": self._with_exclusions(db, siblings),
                        "scope_versions": versions, "scope_content_versions": content_versions}
            # Select prior records together with their whole originals. Merely
            # limiting new evidence while appending the whole old database does
            # not bound the actual API input.
            originals = {row["id"]: row for row in self._with_exclusions(db, support)}
            included_sources = set(snapshot["covered_ids"]) | {row['id'] for row in siblings}
            keywords = self.store._keywords(" ".join(row["content"] for row in rows if row["kind"] in {"user", "manual"}))
            query = "\n".join(row['content'] for row in snapshot['evidence'] if row['kind'] in {'user', 'manual'})
            semantic, semantic_error = {}, None
            if query and entries and self._semantic_scores is not None:
                try:
                    semantic = self._semantic_scores(query, entries, **({"publish": publish} if publish is not None else {}))
                except Exception as exc:
                    semantic_error = exc
            def score(item):
                normalized = self.store._normalize_for_search(item.get("content", item.get("text", "")))
                hits = sum(keyword in normalized for keyword in keywords)
                return semantic.get(item.get('id'), 0.0) + min(hits, 4) * .025 if semantic else hits
            previous = [("working_summary", note) for note in notes]
            previous += [("existing_entries", entry) for entry in entries]
            previous.sort(key=lambda pair: (score(pair[1]), pair[0] == "working_summary", pair[1].get("id", 0)), reverse=True)
            for field, item in previous:
                missing = {source["id"] for source in item["sources"]} - included_sources
                extra = [originals[source_id] for source_id in sorted(missing) if source_id in originals]
                snapshot[field].append(item)
                snapshot["supporting_evidence"].extend(extra)
                if self._tokens(json.dumps(snapshot, ensure_ascii=False)) > token_budget:
                    snapshot[field].pop()
                    if extra:
                        del snapshot["supporting_evidence"][-len(extra):]
                else:
                    included_sources.update(missing)
            if semantic_error is not None and len(snapshot['existing_entries']) < len(entries):
                # A failed selector must not publish a correction from a
                # partial view that silently omitted its possible old target.
                raise RuntimeError('memory semantic selection unavailable for the bounded snapshot') from semantic_error
            return snapshot

    def admit_processing(self, scope, turn_keys):
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT * FROM memory_evidence WHERE character_id=? AND user_id=? AND organized=0',
                              (scope.character_id, scope.user_id)).fetchall()
            processing.admit(db, scope, [row for row in rows if processing.exchange_key(row) in turn_keys])

    def reserve_processing(self, scope, snapshot, *, max_attempts=3):
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if not self._matches_snapshot(db, scope, snapshot):
                return None
            rows = db.execute('SELECT * FROM memory_evidence WHERE character_id=? AND user_id=? AND id IN (SELECT value FROM json_each(?))',
                              (scope.character_id, scope.user_id, json.dumps(snapshot['covered_ids']))).fetchall()
            return processing.reserve(db, scope, rows, max_attempts=max_attempts)

    def finish_processing(self, scope, receipt, **outcome):
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            processing.finish(db, scope, receipt, **outcome)

    def hold_processing(self, scope, snapshot, reason):
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT * FROM memory_evidence WHERE character_id=? AND user_id=? AND id IN (SELECT value FROM json_each(?))',
                              (scope.character_id, scope.user_id, json.dumps(snapshot['covered_ids']))).fetchall()
            processing.hold(db, scope, rows, reason)

    def provider_paused(self, provider_key):
        with closing(self.store._connect()) as db:
            return db.execute("SELECT 1 FROM memory_processing WHERE json_extract(processing_state,'$.provider_hold.key')=? LIMIT 1",
                              (provider_key,)).fetchone() is not None

    def resume_processing(self, scope, provider_key=None):
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            processing.resume(db, scope)
            if provider_key:
                peers = db.execute("SELECT character_id FROM memory_processing WHERE user_id=? AND json_extract(processing_state,'$.provider_hold.key')=?",
                                   (scope.user_id, provider_key)).fetchall()
                for peer in peers:
                    processing.resume(db, MemoryScope(peer[0], scope.user_id))


    def is_current(self, scope: MemoryScope, snapshot: dict) -> bool:
        """Check a captured scope before starting another processing request."""
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN')
            return self._matches_snapshot(db, scope, snapshot)

    @staticmethod
    def _matches_snapshot(db, scope: MemoryScope, snapshot: dict) -> bool:
        versions = {**snapshot.get('scope_versions', {}), scope.character_id: snapshot['version']}
        for character, version in versions.items():
            current = db.execute('SELECT version,passive_version FROM memory_processing WHERE character_id=? AND user_id=?',
                                 (character, scope.user_id)).fetchone()
            if current is None and character == scope.character_id:
                return False
            if (current[0] if current else 0) != version:
                expected = snapshot.get('scope_content_versions', {}).get(character)
                if expected is None or (current[0] - current[1] if current else 0) != expected:
                    return False
        return True

    def apply(self, scope: MemoryScope, snapshot: dict[str, Any], entries: list[dict], *, notes: list[dict] | None = None,
              publish=None, source_corrections: list[dict] | None = None,
              verified_generated_sources: list[dict] | None = None, processing_receipt=None) -> bool:
        owner = (scope.character_id, scope.user_id)
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if not self._matches_snapshot(db, scope, snapshot):
                return False
            if processing_receipt is not None and not processing.owns(db, scope, processing_receipt):
                return False
            visibility, params = self._visibility(scope)
            targets = {}
            for entry in entries:
                for previous in entry["replaces"]:
                    target = db.execute("SELECT * FROM memory_entries WHERE id=? AND " + visibility
                        + (" AND status IN ('active','superseded')" if entry["revision_reason"] == "correction" else " AND status='active'"),
                        (previous, *params)).fetchone()
                    if target is None:
                        raise ValueError("memory revision no longer targets an allowed record")
                    if previous in targets and targets[previous][1] != entry["revision_reason"]:
                        raise ValueError("one source cannot be both corrected and changed in the same proposal")
                    targets[previous] = (target, entry["revision_reason"])
            corrections = [target for target, reason in targets.values() if reason == "correction"]
            raw_spans = {}
            raw_correcting_ids = set()
            for correction in source_corrections or []:
                wrong = self._normalize_sources(db, correction['sources'])
                correcting = self._normalize_sources(db, correction['correction_sources'])
                if not wrong or not correcting:
                    raise ValueError('raw correction requires both originals and correcting inputs')
                for source in wrong:
                    original = db.execute('SELECT character_id,user_id FROM memory_evidence WHERE id=?', (source['id'],)).fetchone()
                    if tuple(original) != owner or any(source['id'] >= item['id'] for item in correcting):
                        raise ValueError('raw correction must target an earlier original in the same writable scope')
                    raw_spans.setdefault(source['id'], []).append((source['start'], source['end']))
                raw_correcting_ids.update(source['id'] for source in correcting)
            normalized_entries = [{**entry, "sources": self._normalize_sources(db, entry["sources"])} for entry in entries]
            if corrections or raw_spans:
                retained = self._normalize_sources(db, [source for entry in entries for source in entry.get("retained_sources", [])]
                                                   + list(verified_generated_sources or []))
                old_source_ids = {source["id"] for target in corrections for source in json.loads(target["sources"])} | raw_spans.keys()
                new_inputs = {row["id"] for row in snapshot["evidence"] if row["kind"] in {"user", "manual"}}
                correcting_ids = {source["id"] for entry in entries for source in entry.get("correction_sources", [])} | raw_correcting_ids
                if not correcting_ids or not correcting_ids <= new_inputs - old_source_ids:
                    raise ValueError("correction requires the current correcting original input")
                protected = {}
                for source in retained:
                    if source["id"] in old_source_ids:
                        continue
                    row = db.execute("SELECT kind,metadata FROM memory_evidence WHERE id=?", (source["id"],)).fetchone()
                    metadata = json.loads(row["metadata"])
                    context_ids = set(metadata.get("context_evidence_ids", [])) | {metadata.get("input_evidence_id")}
                    if row["kind"] == "assistant_generated" and context_ids & correcting_ids:
                        protected.setdefault(source["id"], []).append((source["start"], source["end"]))
                    else:
                        raise ValueError("retained fragment is neither valid old support nor a response to this correction")
                self._withdraw(db, scope, corrections, reason="correction", delete_source=False,
                               retained_sources=retained, protected_generated=protected, evidence_spans=raw_spans)
            for previous, (target, reason) in targets.items():
                if reason == "change":
                    db.execute("UPDATE memory_entries SET status='superseded' WHERE id=?", (previous,))
                    self._bump(db, target["character_id"], scope.user_id)
            # Retire old interpretations as one batch before inserting any new
            # record. New citations must still be valid after that withdrawal.
            for entry in normalized_entries:
                refs = self._normalize_sources(db, entry["sources"])
                sources = json.dumps(refs, ensure_ascii=False, sort_keys=True)
                duplicate = db.execute("""SELECT id FROM memory_entries WHERE character_id=? AND user_id=?
                    AND kind=? AND semantic_key=? AND sources=? AND status='active'""",
                    (*owner, entry["kind"], entry["key"], sources)).fetchone()
                if duplicate is not None:
                    continue
                history_only = bool(entry["replaces"]) and entry["revision_reason"] == "correction" and all(
                    previous["status"] == "superseded" for previous in snapshot["existing_entries"] if previous["id"] in entry["replaces"])
                db.execute("""INSERT INTO memory_entries
                    (character_id,user_id,kind,semantic_key,content,origin,sources,revision,revision_reason,recorded_at,subject,replaces,status,valid_from,valid_until)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (*owner, entry["kind"], entry["key"], entry["text"], entry["origin"], sources,
                     snapshot["version"], entry["revision_reason"], time.time(), entry.get("subject", "user"), json.dumps(entry["replaces"]),
                     "superseded" if history_only else "active",
                     *(datetime.fromisoformat(entry[key]).timestamp() if entry.get(key) else None for key in ("valid_from", "valid_until"))))
            db.executemany("UPDATE memory_evidence SET organized=1 WHERE id=? AND character_id=? AND user_id=?",
                           [(source_id, *owner) for source_id in snapshot["covered_ids"]])
            db.execute("UPDATE memory_processing SET processed_id=MAX(processed_id,?),version=version+1 WHERE character_id=? AND user_id=?",
                       (max(snapshot["covered_ids"]), *owner))
            if notes is not None:
                previous_context = db.execute("SELECT notes FROM memory_context WHERE character_id=? AND user_id=?", owner).fetchone()
                considered = {json.dumps(note, ensure_ascii=False, sort_keys=True) for note in snapshot.get("working_summary", [])}
                preserved = [note for note in json.loads(previous_context[0]) if json.dumps(note, ensure_ascii=False, sort_keys=True) not in considered] if previous_context else []
                normalized = self._normalize_notes(db, notes)
                # Same transaction as entries/cursor: only a complete verified
                # snapshot can replace the last usable working summary.
                db.execute("""INSERT INTO memory_context VALUES (?,?,?,?)
                    ON CONFLICT(character_id,user_id) DO UPDATE SET through_id=excluded.through_id,notes=excluded.notes""",
                    (*owner, max(snapshot["covered_ids"]), json.dumps([*preserved, *normalized], ensure_ascii=False)))
            if processing_receipt is not None:
                processing.finish(db, scope, processing_receipt, success=True)
            processing.resolve_disputes(db, scope, snapshot)
            return publish(db) if publish is not None else True

    @staticmethod
    def _turn_key(row) -> str:
        if "turn_id" in row.keys() and row["turn_id"]:
            return row["turn_id"]
        metadata = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"])
        if row["event_id"].startswith("manual:"):
            return row["event_id"]
        return metadata.get("turn_id") or (row["event_id"].split(":tool:")[0] if ":tool:" in row["event_id"] else row["event_id"].rsplit(":", 1)[0])

    @staticmethod
    def _tokens(text: str) -> int:
        # Conservative for Chinese/Japanese (one token per character), while
        # ASCII prose is roughly four characters per token. Provider usage is
        # still measured in real-model validation; this is a selection budget.
        return sum(1.0 if ord(char) > 127 else 0.25 for char in text).__ceil__()

    @staticmethod
    def _normalize_sources(db, sources: list[dict]) -> list[dict]:
        refs = []
        for source in sources:
            original = db.execute("SELECT content FROM memory_evidence WHERE id=?", (source["id"],)).fetchone()
            if original is None or source["quote"] not in original[0]:
                raise ValueError(f"source {source['id']} lost original support during correction")
            start = source.get("start") if source.get("start") is not None else original[0].index(source["quote"])
            end = source.get("end") if source.get("end") is not None else start + len(source["quote"])
            if (type(start) is not int or type(end) is not int
                    or not 0 <= start < end <= len(original[0])
                    or end - start != len(source["quote"])
                    or original[0][start:end] != source["quote"]):
                raise ValueError("source offsets do not match the quoted original fragment")
            if db.execute("SELECT 1 FROM memory_exclusions WHERE evidence_id=? AND start<? AND end>?",
                          (source["id"], end, start)).fetchone():
                raise ValueError(f"source {source['id']} uses withdrawn support; retain only verified unchanged old fragments")
            refs.append({**source, "start": start, "end": end})
        return refs

    @classmethod
    def _normalize_notes(cls, db, notes: list[dict]) -> list[dict]:
        return [{**note, "sources": cls._normalize_sources(db, note["sources"])} for note in notes]

    @staticmethod
    def _origin_label(row: dict) -> str:
        recorded = datetime.fromtimestamp(row['recorded_at']).astimezone()
        label = f"evidence={row['id']} modality={row['modality']} recorded_at={recorded.isoformat()}"
        now = time.time()
        reference_date = datetime.fromtimestamp(now).astimezone().date()
        # Crossing midnight changes "today" even during an ordinary short
        # pause. Date annotation does not reset the six-hour context window.
        if recorded.date() < reference_date or row['recorded_at'] < now - _WORKING_CONTEXT_IDLE_SECONDS:
            label += (' temporal_scope=historical reference_date='
                      + reference_date.isoformat()
                      + f" recorded_days_ago={(now-row['recorded_at'])/86400:.1f}")
        if row.get('occurred_at') is not None:
            label += ' occurred_at=' + datetime.fromtimestamp(row['occurred_at']).astimezone().isoformat()
        if row.get('expired_states'):
            label += ' expired_states=' + json.dumps(row['expired_states'], ensure_ascii=False)
        return label

    @classmethod
    def _turn_messages(cls, rows: list[dict]) -> list[dict]:
        messages = []
        outcome = next((row["metadata"].get("outcome") for row in rows if row["kind"] == "delivery"), "unknown")
        index = 0
        while index < len(rows):
            row = rows[index]
            source = row["source"]
            origin = cls._origin_label(row)
            if row["kind"] == "user":
                domain = row['metadata'].get('memory_domain')
                label = (f"CONTEXT_DATA source=domain_user_input input_source={source} domain={domain} "
                         "personal_claims_unverified=true" if domain else f"PAST_MESSAGE source={source}")
                messages.append({"role": "user", "content": f"[{label} {origin}]\n" + row["content"]})
            elif row["kind"] == "manual":
                messages.append({"role": "user", "content": f"[CONTEXT_DATA source=memory_editor {origin}]\n"
                                 + json.dumps(row["metadata"], ensure_ascii=False) + "\n" + row["content"]})
            elif row["kind"] == "assistant_generated":
                incomplete = row['metadata'].get('generation_complete') is False
                if incomplete:
                    origin += ' generation_complete=false outcome=' + row['metadata'].get('outcome', 'incomplete')
                    if row['metadata'].get('basis'):
                        origin += ' basis=' + row['metadata']['basis']
                    if row['metadata'].get('emitted_units'):
                        origin += ' emitted_units=' + json.dumps(row['metadata']['emitted_units'], separators=(',', ':'))
                messages.append({"role": "assistant", "content": f"[ASSISTANT_GENERATED {origin} delivery={outcome}]\n"
                    + ('[回复未完成；emitted_units 只记录发出的运行时事件，不证明设备已呈现或本人听到。]\n' if incomplete else '')
                    + row["content"]})
            elif row["kind"] == "tool" and row["metadata"].get("actor") == "business":
                messages.append({"role": "user", "content": f"[CONTEXT_DATA source={row['source']} {origin}]\n" + row["content"]})
            elif row["kind"] == "tool":
                batch = row["metadata"].get("batch")
                calls, outputs = [], []
                while index < len(rows) and rows[index]["kind"] == "tool" and rows[index]["metadata"].get("batch") == batch:
                    tool = rows[index]
                    call_id = tool["metadata"].get("model_call_id") or "evidence_" + str(tool["id"])
                    calls.append({"id": call_id, "type": "function", "function": {
                        "name": tool["source"], "arguments": tool["metadata"].get("arguments", "{}")}})
                    outputs.append({"role": "tool", "tool_call_id": call_id, "content": tool["content"]})
                    index += 1
                messages.append({"role": "assistant", "content": f"[PAST_TOOL_EXCHANGE {origin}]", "tool_calls": calls})
                messages.extend(outputs)
                continue
            elif row["kind"] in {"environment", "system_event", "delivery", "playback"}:
                messages.append({"role": "user", "content": f"[CONTEXT_DATA source={row['source'] if row['kind'] == 'environment' else row['kind']} {origin}]\n" +
                                 (json.dumps(row["metadata"], ensure_ascii=False) if row["kind"] in {"delivery", "playback"} else row["content"])})
            index += 1
        return messages

    def working_context(self, scope: MemoryScope, query: str, *, token_budget: int,
                        exclude_turn_id: str | None = None, pending_entry_ids: set[int] | None = None,
                        similarity: Callable[[str], float] | None = None,
                        recent_turn_limit: int | None = None, event_binding=None,
                        cancelled: Event | None = None) -> dict:
        import re
        def check_cancelled():
            if cancelled is not None and cancelled.is_set():
                raise CancelledError('memory retrieval cancelled')
        check_cancelled()
        owner = (scope.character_id, scope.user_id)
        source_recall = bool(re.search(r"原话|原文|逐字|完整对话|当时怎么说|そのまま|何て言|何と言|exact wording", query, re.IGNORECASE))
        continuation = bool(re.search(r"接着|继续|合起来|这些限制|刚才|その条件|まとめ|続き|それ", query))
        referential = continuation or source_recall or historical_query(query) or bool(re.search(r"上次|那个|那件|之前|さっき|前に|この前|前回|あれ", query))
        source_recall = source_recall or (referential and bool(re.search(
            r"你.{0,12}(?:说|提到|讲|推荐|建议|告诉)|(?:言っ|話し|勧め|すすめ|教え).{0,8}(?:た|くれ)", query)))
        keywords = self.store._keywords(query)
        semantic_available = True
        def related(content):
            nonlocal semantic_available
            check_cancelled()
            normalized = self.store._normalize_for_search(content)
            if any(word in normalized for word in keywords):
                return True
            if not referential or similarity is None or not semantic_available:
                return False
            try:
                # Deictic ZH/JA references are shorter than stable-fact queries.
                # The local name-reference pair scores .36; unrelated turns <.15.
                return similarity(content) >= .32
            except CancelledError:
                raise
            except Exception:
                semantic_available = False
                return False  # keep same-entry raw evidence and lexical matches

        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN")
            now = time.time()
            recent_after = now - _WORKING_CONTEXT_IDLE_SECONDS
            pending_entry_ids = pending_entry_ids or set()
            raw_links = self._pending_raw_corrections(db, scope)
            raw_corrections = {}
            raw_note_sources = set()
            for correction, targets in raw_links:
                key = correction['conversation_id'], correction['turn_id']
                for target in targets:
                    raw_corrections.setdefault((target['conversation_id'], target['turn_id']), set()).add(key)
                    raw_note_sources.add(target['id'])
            pending_sources = set()
            if pending_entry_ids:
                for entry in db.execute('''SELECT sources FROM memory_entries WHERE user_id=?
                    AND id IN (SELECT value FROM json_each(?))''', (scope.user_id, json.dumps(sorted(pending_entry_ids)))):
                    pending_sources.update(source['id'] for source in json.loads(entry['sources']))
            expired_sources = {}
            for state in db.execute("""SELECT sources,valid_until FROM memory_entries
                WHERE character_id=? AND user_id=? AND status='active'
                AND valid_until IS NOT NULL AND valid_until<=?""", (*owner, now)):
                for source in json.loads(state['sources']):
                    expired_sources.setdefault(source['id'], []).append({**source,
                        'valid_until': datetime.fromtimestamp(state['valid_until']).astimezone().isoformat()})

            def expired_note(sources):
                for source in sources:
                    for expired in expired_sources.get(source['id'], []):
                        if (source.get('start') is None or expired.get('start') is None
                                or source['start'] < expired['end'] and source['end'] > expired['start']):
                            return True
                return False

            summary = db.execute("SELECT * FROM memory_context WHERE character_id=? AND user_id=?", owner).fetchone()
            through_id = summary["through_id"] if summary else 0
            notes = json.loads(summary["notes"]) if summary else []
            reset_rows = list(db.execute(
                "SELECT * FROM memory_context_resets WHERE character_id=? AND user_id=?", owner))
            reset_times = {row["conversation_id"]: row["reset_at"] for row in reset_rows}
            candidates = list(db.execute("""SELECT turn_id,conversation_id,MAX(id) AS latest,
                MAX(recorded_at) AS latest_time,MIN(organized) AS organized FROM memory_evidence
                WHERE character_id=? AND user_id=? AND status IN ('active','redacted')
                GROUP BY turn_id,conversation_id ORDER BY latest_time DESC,latest DESC""", owner))
            by_turn = {(row['conversation_id'], row['turn_id']): row for row in candidates}
            recent_keys = {(row['conversation_id'], row['turn_id']) for row in [row for row in candidates
                if row['conversation_id'] == (scope.conversation_id or 'default') and row['turn_id'] != exclude_turn_id
                and row['latest_time'] > max(recent_after, reset_times.get(row['conversation_id'], 0))][:recent_turn_limit]}
            home_user = None
            if event_binding is not None and event_binding.kind == 'home.wake':
                from memory.event_projection import home_turn_selection
                bound = list(db.execute("""SELECT conversation_id,turn_id,kind FROM memory_evidence
                    WHERE character_id=? AND user_id=? AND status IN ('active','redacted') AND turn_id!=?
                    AND json_extract(metadata,'$.event_binding.kind')=?
                    AND json_extract(metadata,'$.event_binding.event_id')=? ORDER BY id""",
                    (*owner, exclude_turn_id or '', event_binding.kind, event_binding.event_id)))
                users, recent_keys = home_turn_selection(bound)
                home_user = users[-1] if users else None
                if home_user is not None:
                    recent_keys.add(home_user)
                recent_keys.update((row['conversation_id'], row['turn_id']) for row in bound[-1:])
            continuity_keys = set()
            if continuation and recent_turn_limit is not None:
                # Follow actual prior prompt sources; a turn count is not a
                # task boundary. Scope/reset/time and the usual validity and
                # whole-exchange budget checks still apply to every ancestor.
                frontier, seen = set(recent_keys), set()
                while frontier:
                    current = frontier.pop()
                    if current in seen:
                        continue
                    seen.add(current)
                    for row in db.execute('SELECT metadata FROM memory_evidence WHERE character_id=? AND user_id=? AND conversation_id=? AND turn_id=?', (*owner, *current)):
                        refs = json.loads(row['metadata']).get('context_evidence_ids', [])
                        for source in db.execute('SELECT conversation_id,turn_id,recorded_at FROM memory_evidence WHERE character_id=? AND user_id=? AND id IN (SELECT value FROM json_each(?))', (*owner, json.dumps(refs))):
                            key = (source['conversation_id'], source['turn_id'])
                            if key[0] == (scope.conversation_id or 'default') and key[1] != exclude_turn_id and source['recorded_at'] > max(recent_after, reset_times.get(key[0], 0)):
                                continuity_keys.add(key)
                                frontier.add(key)
            priority, relevant_keys = {}, set()
            if keywords:
                pending = db.execute("""SELECT * FROM memory_evidence WHERE character_id=? AND user_id=?
                    AND organized=0 AND status IN ('active','redacted')
                    AND (kind='user' OR kind='manual' AND json_extract(metadata,'$.revision_reason') IS NOT NULL
                        OR kind='environment' AND json_extract(metadata,'$.actor')='business'
                        AND json_extract(metadata,'$.event_binding.kind') IS NOT NULL)""", owner).fetchall()
                for row in self._with_exclusions(db, pending):
                    check_cancelled()
                    if row['turn_id'] == exclude_turn_id or row['recorded_at'] <= reset_times.get(row['conversation_id'], 0):
                        continue
                    content = row['content'].replace('█', '').strip()
                    if not content:
                        continue
                    hits = sum(word in self.store._normalize_for_search(content) for word in keywords)
                    relevance = float(hits)
                    admitted = bool(hits)
                    if similarity is not None and semantic_available:
                        try:
                            score = similarity(content)
                            relevance = score + min(hits, 4) * .025
                            admitted = score >= .42 or hits and score >= .28
                        except CancelledError:
                            raise
                        except Exception:
                            semantic_available = False
                    key = (row['conversation_id'], row['turn_id'])
                    priority[key] = max(priority.get(key, 0), relevance)
                    if admitted:
                        relevant_keys.add(key)
                # Keep the current exchange for ordinary continuation, then
                # reserve space for relevant pending evidence before chatter.
                candidates.sort(key=lambda row: ((row['conversation_id'], row['turn_id']) in recent_keys,
                    (row['conversation_id'], row['turn_id']) in continuity_keys,
                    priority.get((row['conversation_id'], row['turn_id']), -1), row['latest_time'], row['latest']), reverse=True)
            if home_user is not None:
                candidates.sort(key=lambda row: (row['conversation_id'], row['turn_id']) == home_user, reverse=True)
            def readable_exchange(candidate):
                # Corrections and ordinary candidates share the same source,
                # reset and domain boundary before any relevance/budget choice.
                turn, conversation = candidate["turn_id"], candidate["conversation_id"]
                if turn == exclude_turn_id or candidate["latest_time"] <= reset_times.get(conversation, 0):
                    return []
                if (candidate["organized"] and not referential
                        and conversation != (scope.conversation_id or "default")):
                    return []
                rows = db.execute("""SELECT * FROM memory_evidence WHERE character_id=? AND user_id=?
                    AND conversation_id=? AND turn_id=? AND status IN ('active','redacted') ORDER BY id DESC""",
                    (*owner, conversation, turn)).fetchall()
                values = self._with_exclusions(db, rows)
                values = [value for value in values if value["kind"] != "manual" or value["metadata"].get("revision_reason")]
                if conversation != (scope.conversation_id or 'default') and any(
                        value['metadata'].get('memory_domain') for value in values):
                    # Unprocessed, relevant participant statements can carry a
                    # personal correction across leaving a game. Never replay
                    # its model narration, tools or observations outside that
                    # domain. Once organized, only verified personal facts and
                    # notes cross this boundary, not raw game dialogue.
                    if candidate['organized']:
                        return []
                    values = [value for value in values if value['kind'] == 'user']
                values = [value for value in values if value["content"].replace("█", "").strip()]
                if any(row['id'] in pending_sources
                       or pending_entry_ids.intersection(row['metadata'].get('memory_entry_ids', []))
                       or pending_sources.intersection(row['metadata'].get('context_evidence_ids', []))
                       or row['metadata'].get('input_evidence_id') in pending_sources for row in values):
                    return []
                from memory.event_projection import home_projection
                values = home_projection(values)
                # A late delivery receipt cannot renew the age of the user's
                # old exchange. Explicit history reads keep its original date.
                input_times = [row['recorded_at'] for row in values if row['kind'] in {'user', 'manual'}]
                exchange_time = max(input_times, default=candidate['latest_time'])
                if not referential and any(row['id'] in expired_sources for row in values):
                    return []
                # Until publication, originals are the failure/correction
                # fallback. Idle time cannot make an unprocessed correction
                # disappear while the older interpreted fact remains active.
                if candidate['organized'] and exchange_time < recent_after and not (source_recall and related(
                        '\n'.join(row['content'] for row in values))):
                    return []
                for row in values:
                    if row['id'] in expired_sources:
                        row['expired_states'] = [{'quote': source.get('quote'), 'valid_until': source['valid_until']}
                            for source in expired_sources[row['id']]]
                if conversation != (scope.conversation_id or 'default'):
                    from memory.event_projection import is_home_terminal
                    values = [row for row in values if row['kind'] in {'user', 'assistant_generated', 'tool', 'delivery'}
                              or is_home_terminal(row)]
                return values

            def projected_messages(groups):
                from memory.event_projection import home_projection
                projected_ids = {row['id'] for row in home_projection([row for values in groups.values() for row in values])}
                messages, evidence_ids = [], []
                for group in sorted(groups.values(), key=lambda rows: min((row['recorded_at'], row['id']) for row in rows)):
                    rows = [row for row in reversed(group) if row['id'] in projected_ids]
                    evidence_ids.extend(row['id'] for row in rows)
                    messages.extend(self._turn_messages(rows))
                return messages, evidence_ids

            groups, used, latest_other = {}, 0, None
            latest_current_ids = set()
            latest_current_complete = False
            for candidate in candidates:
                check_cancelled()
                turn, conversation = candidate["turn_id"], candidate["conversation_id"]
                if (conversation, turn) in groups:
                    continue
                if used >= token_budget:
                    break
                values = readable_exchange(candidate)
                if not values:
                    continue
                if conversation == (scope.conversation_id or "default") and values:
                    keep_recent = (conversation, turn) in recent_keys | continuity_keys
                    complete = any(row['kind'] in {'assistant_generated', 'tool'}
                        or row['kind'] == 'manual' and row['metadata'].get('revision_reason') for row in values)
                    if candidate['organized'] and (referential or not complete) and (conversation, turn) not in continuity_keys:
                        keep_recent = False
                    latest_current = not latest_current_ids
                    if latest_current:
                        latest_current_ids.update(row["id"] for row in values)
                        latest_current_complete = complete
                        # An organized standalone observation is already
                        # represented by its summary. Replay complete exchanges
                        # for continuation, not every old input-only event.
                        if candidate['organized'] and not latest_current_complete and not referential:
                            continue
                    # Finishing background organization is not a conversation
                    # reset. Keep the latest exchange for normal continuation
                    # in any language; older organized turns still need an
                    # explicit source request and a relevant anchor.
                    if candidate["organized"] and not latest_current and not keep_recent and (
                        not source_recall or not related("\n".join(row["content"] for row in values))
                    ):
                        continue
                    if (not candidate['organized'] and not latest_current and not keep_recent
                            and (conversation, turn) not in relevant_keys
                            and not (referential and related('\n'.join(row['content'] for row in values)))):
                        continue
                if conversation != (scope.conversation_id or "default"):
                    if latest_other is not None or not values:
                        continue
                    if not related("\n".join(row["content"] for row in values)):
                        continue
                    latest_other = turn
                if not values:
                    continue
                # Budget the actual provider messages, including call arguments,
                # receipt metadata, source wrappers and JSON escaping.
                bundle = {(conversation, turn): values}
                frontier = list(raw_corrections.get((conversation, turn), ()))
                while frontier:
                    key = frontier.pop()
                    if key in bundle or key in groups or key[1] == exclude_turn_id:
                        continue
                    correction = readable_exchange(by_turn[key]) if key in by_turn else []
                    if not correction:
                        bundle.clear()  # never replay a disputed original alone
                        break
                    bundle[key] = correction
                    frontier.extend(raw_corrections.get(key, ()))
                if not bundle:
                    continue
                proposed = {**groups, **bundle}
                messages, _ = projected_messages(proposed)
                cost = self._tokens(json.dumps(messages, ensure_ascii=False)) if messages else 0
                if cost > token_budget:
                    continue  # leave the entire exchange out; verified notes may still fit
                groups, used = proposed, cost
            messages, evidence_ids = projected_messages(groups)
            chosen = []
            current_reset = reset_times.get(scope.conversation_id or "default", 0)
            for note in notes:
                check_cancelled()
                ids = {source["id"] for source in note["sources"]}
                if ids.intersection(pending_sources | raw_note_sources):
                    continue
                source_rows = [db.execute('''SELECT id,recorded_at,occurred_at,modality FROM memory_evidence
                    WHERE id=? AND character_id=? AND user_id=?''', (source_id, *owner)).fetchone() for source_id in sorted(ids)]
                if not ids or any(row is None for row in source_rows):
                    continue
                latest_source_time = max(row['recorded_at'] for row in source_rows)
                if not ids or latest_source_time <= current_reset or ids.issubset(evidence_ids):
                    continue
                if not referential and expired_note(note['sources']):
                    continue
                if related(note["text"]) or ((latest_current_complete or referential or not keywords)
                        and ids.intersection(latest_current_ids)):
                    reference_date = datetime.fromtimestamp(now).astimezone().date()
                    historical = (latest_source_time < recent_after or expired_note(note['sources'])
                                  or datetime.fromtimestamp(latest_source_time).astimezone().date() < reference_date)
                    times = [{'evidence_id': row['id'], 'modality': row['modality'],
                              'recorded_at': datetime.fromtimestamp(row['recorded_at']).astimezone().isoformat(),
                              'occurred_at': datetime.fromtimestamp(row['occurred_at']).astimezone().isoformat()
                                  if row['occurred_at'] is not None else None} for row in source_rows]
                    candidate = [*chosen, {"text": note["text"], "source_ids": sorted(ids),
                        'source_times': times, 'temporal_scope': 'historical'
                            if historical else 'recent',
                        **({'reference_date': reference_date.isoformat()} if historical else {}),
                        'expired_states': [{'evidence_id': source_id, 'quote': source.get('quote'),
                                            'valid_until': source['valid_until']}
                            for source_id in sorted(ids) for source in expired_sources.get(source_id, [])]}]
                    summary_message = {"role": "user", "content": "[CONTEXT_DATA source=verified_working_summary]\n" + json.dumps(candidate, ensure_ascii=False)}
                    if self._tokens(json.dumps([summary_message, *messages], ensure_ascii=False)) <= token_budget:
                        chosen = candidate
            if chosen:
                messages.insert(0, {"role": "user", "content": "[CONTEXT_DATA source=verified_working_summary]\n" + json.dumps(chosen, ensure_ascii=False)})
            used = self._tokens(json.dumps(messages, ensure_ascii=False)) if messages else 0
            selected_ids = set(evidence_ids) | {source_id for note in chosen for source_id in note["source_ids"]}
            revisions = {source_id: db.execute("SELECT revision FROM memory_evidence WHERE id=?", (source_id,)).fetchone()[0]
                         for source_id in selected_ids}
            check_cancelled()
            return {"messages": messages, "evidence_ids": evidence_ids, "estimated_tokens": used,
                    "summary_through_id": through_id, "status": "available" if semantic_available else "unavailable",
                    "evidence_revisions": revisions,
                    "summary_source_ids": sorted({source_id for note in chosen for source_id in note["source_ids"]})}

    def clear_context(self, scope: MemoryScope, *, clear_long_term: bool = False,
                      delete_source: bool = True) -> None:
        owner = (scope.character_id, scope.user_id)
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            through_id = db.execute("SELECT COALESCE(MAX(id),0) FROM memory_evidence WHERE character_id=? AND user_id=?", owner).fetchone()[0]
            db.execute("""INSERT INTO memory_context_resets VALUES (?,?,?,?,?)
                ON CONFLICT(character_id,user_id,conversation_id) DO UPDATE SET through_id=excluded.through_id,reset_at=excluded.reset_at""",
                (*owner, scope.conversation_id or "default", through_id, time.time()))
            if clear_long_term:
                visibility, params = self._visibility(scope)
                targets = db.execute("SELECT * FROM memory_entries WHERE " + visibility
                    + " AND status IN ('active','superseded')", params).fetchall()
                pending = db.execute("SELECT id,content FROM memory_evidence WHERE character_id=? AND user_id=? AND organized=0",
                                     owner).fetchall()
                # Explicit long-term clear also withdraws its not-yet-extracted
                # originals. One transaction fences an in-flight publication
                # and old archives; later input remains eligible as new evidence.
                self._withdraw(db, scope, targets, reason='deleted', delete_source=delete_source,
                    evidence_spans={row['id']: [(0, len(row['content']))] for row in pending if row['content']})
                db.execute('UPDATE memory_evidence SET organized=1 WHERE character_id=? AND user_id=? AND id<=?',
                           (*owner, through_id))
                db.execute('UPDATE memory_processing SET processed_id=MAX(processed_id,?) WHERE character_id=? AND user_id=?',
                           (through_id, *owner))
            self._bump(db, *owner)

    def entries(self, scope: MemoryScope, *, include_history: bool = False,
                limit: int | None = None, before_id: int | None = None) -> list[dict[str, Any]]:
        statuses = "('active','superseded')" if include_history else "('active')"
        visibility, params = self._visibility(scope)
        suffix = " AND id<?" if before_id is not None else ""
        if before_id is not None:
            params = (*params, before_id)
        suffix += " ORDER BY id DESC"
        if limit is not None:
            suffix += " LIMIT ?"
            params = (*params, max(1, limit))
        with closing(self.store._connect()) as db:
            rows = db.execute("SELECT * FROM memory_entries WHERE " + visibility
                              + " AND status IN " + statuses + suffix, params).fetchall()
            return self._visible_entries(db, rows, scope)

    def _visible_entries(self, db, rows, scope: MemoryScope) -> list[dict]:
        entries = [self._entry(row) for row in rows]
        source_ids = {source["id"] for entry in entries if entry["character_id"] == scope.character_id
                      for source in entry["sources"]}
        # An imported entry can cite another role's original as a read-only
        # dependency. Entry ownership never grants access to that source text.
        visible_sources = {row[0] for row in db.execute("""SELECT id FROM memory_evidence
            WHERE character_id=? AND user_id=? AND id IN (SELECT value FROM json_each(?))""",
            (scope.character_id, scope.user_id, json.dumps(sorted(source_ids))))} if source_ids else set()
        for entry in entries:
            if entry["character_id"] != scope.character_id:
                # Sharing a stable fact does not share its source chat.
                entry["sources"] = [{"id": source["id"], "origin_character": entry["character_id"]}
                                    for source in entry["sources"]]
            else:
                entry["sources"] = [source if source["id"] in visible_sources else
                                    {"id": source["id"], "availability": "outside_scope"}
                                    for source in entry["sources"]]
        return entries

    def remember(self, scope: MemoryScope, content: str, *, kind: str = "fact", subject: str = "user",
                 replaces: int | None = None, reason: str | None = None, provenance: dict | None = None) -> int:
        if subject not in {"user", "relationship", "character", "project"} or kind not in {"fact", "preference", "state", "episode"}:
            raise ValueError("invalid memory category")
        if not content.strip() or bool(replaces is not None) != bool(reason) or reason not in {None, "change", "correction"}:
            raise ValueError("invalid memory revision")
        owner = (scope.character_id, scope.user_id)
        visibility, params = self._visibility(scope)
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = None
            if replaces is not None:
                previous = db.execute("SELECT * FROM memory_entries WHERE id=? AND " + visibility
                                      + (" AND status IN ('active','superseded')" if reason == "correction" else " AND status='active'"),
                                      (replaces, *params)).fetchone()
                if previous is None:
                    raise LookupError("memory revision requires an allowed active record")
                kind, subject = previous["kind"], previous["subject"]
                if reason == "correction":
                    self._withdraw(db, scope, [previous], reason=reason, delete_source=False)
                else:
                    db.execute("UPDATE memory_entries SET status='superseded' WHERE id=?", (replaces,))
                    self._bump(db, previous["character_id"], scope.user_id)
            manual_event = "manual:" + ("legacy:" + provenance['fingerprint'] if provenance else uuid4().hex)
            if provenance:
                old = db.execute('SELECT id FROM memory_evidence WHERE character_id=? AND user_id=? AND event_id=?', (*owner,manual_event)).fetchone()
                if old is not None:
                    raise EvidenceAdmissionError('this legacy candidate was already handled; it cannot be replayed')
            result = db.execute("""INSERT INTO memory_evidence
                (character_id,user_id,conversation_id,event_id,kind,source,modality,content,recorded_at,metadata,turn_id)
                VALUES (?,?,?,?, 'manual','memory_editor','text',?,?,?,?)""",
                (*owner, scope.conversation_id or "default", manual_event, content, time.time(),
                json.dumps({"replaces": replaces, "revision_reason": reason,
                             "target_status": previous["status"] if previous is not None else None,
                             **({'legacy': provenance, 'original_chat_available': False} if provenance else {})}), manual_event))
            source_id = int(result.lastrowid)
            self._bump(db, *owner)
            version = db.execute("SELECT version FROM memory_processing WHERE character_id=? AND user_id=?", owner).fetchone()[0]
            result = db.execute("""INSERT INTO memory_entries
                (character_id,user_id,kind,semantic_key,content,origin,sources,revision,recorded_at,subject,revision_reason,replaces,status,valid_from,valid_until)
                VALUES (?,?,?,?,?,'direct',?,?,?,?,?,?,?,?,?)""", (*owner, kind,
                previous["semantic_key"] if previous else "manual:" + str(source_id), content,
                json.dumps([{"id": source_id, "quote": content, "start": 0, "end": len(content)}], ensure_ascii=False),
                version, time.time(), subject, reason, json.dumps([replaces] if replaces is not None else []),
                previous["status"] if previous is not None and reason == "correction" else "active",
                previous["valid_from"] if previous is not None and reason == "correction" else None,
                previous["valid_until"] if previous is not None and reason == "correction" else None))
            return int(result.lastrowid)

    def forget(self, scope: MemoryScope, entry_id: int, *, delete_source: bool) -> None:
        visibility, params = self._visibility(scope)
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            target = db.execute("SELECT * FROM memory_entries WHERE id=? AND " + visibility,
                                (entry_id, *params)).fetchone()
            if target is None:
                raise LookupError("memory is outside the current allowed scope")
            self._withdraw(db, scope, [target], reason="deleted", delete_source=delete_source)

    @staticmethod
    def _bump(db, character: str, user: str) -> None:
        db.execute("""INSERT INTO memory_processing(character_id,user_id,version) VALUES (?,?,1)
            ON CONFLICT(character_id,user_id) DO UPDATE SET version=version+1""", (character, user))

    def _withdraw(self, db, scope: MemoryScope, targets, *, reason: str, delete_source: bool,
                  retained_sources=(), protected_generated=None, evidence_spans=None) -> None:
        """Withdraw original spans and their derivatives in the caller's transaction."""
        spans: dict[int, list[tuple[int, int]]] = {}
        originals = {}
        changed_characters = {scope.character_id, *(target["character_id"] for target in targets)}
        for target in targets:
            for source in json.loads(target["sources"]):
                # Cross-role locators are read-only dependencies. Deleting a
                # shared original entry still uses that entry's actual owner;
                # a derived/imported entry cannot redact another role's chat.
                original = db.execute("SELECT * FROM memory_evidence WHERE id=? AND user_id=? AND character_id=?",
                                      (source["id"], target["user_id"], target["character_id"])).fetchone()
                if original is None:
                    continue
                originals[source["id"]] = original["content"]
                start = source.get("start", original["content"].find(source.get("quote", "")))
                end = source.get("end", start + len(source.get("quote", "")))
                # Old records may predate strict source-bound validation. A
                # deletion can only mask existing characters, never allocate
                # according to an untrusted legacy end offset.
                if original["content"]:
                    end = min(end, len(original["content"]))
                if start < 0 or end <= start:
                    continue
                spans.setdefault(source["id"], []).append((start, end))
                changed_characters.add(original["character_id"])
        for source_id, ranges in (evidence_spans or {}).items():
            original = db.execute('SELECT content FROM memory_evidence WHERE id=? AND character_id=? AND user_id=?',
                                  (source_id, scope.character_id, scope.user_id)).fetchone()
            if original is not None:
                originals[source_id] = original['content']
                spans.setdefault(source_id, []).extend(ranges)
        # Old interpretations/derivatives are invalidated using their full
        # support. Only the actually false raw fragments become exclusions.
        excluded_spans = {}
        for source_id, ranges in spans.items():
            kept = [(source["start"], source["end"]) for source in retained_sources if source["id"] == source_id] if reason == "correction" else []
            excluded_spans[source_id] = self._subtract_spans(ranges, kept)
            for start, end in excluded_spans[source_id]:
                db.execute("INSERT OR IGNORE INTO memory_exclusions VALUES (?,?,?,?,?)",
                           (source_id, start, end, reason, time.time()))
            db.execute("UPDATE memory_evidence SET revision=revision+1 WHERE id=?", (source_id,))
        # Mask all spans together. Multiple citations of one message must not
        # undo an earlier mask by repeatedly starting from its original text.
        if delete_source:
            for source_id, ranges in spans.items():
                content = originals[source_id]
                if not content:  # already expired; locations still deny re-extraction
                    continue
                for start, end in sorted(ranges, reverse=True):
                    content = content[:start] + "█" * (end - start) + content[end:]
                metadata = json.loads(db.execute('SELECT metadata FROM memory_evidence WHERE id=?', (source_id,)).fetchone()[0])
                db.execute("UPDATE memory_evidence SET content=?,metadata=?,status='redacted' WHERE id=?",
                           (content, json.dumps(self._receipt_metadata(metadata)), source_id))
        affected = {target["id"] for target in targets}
        erased_evidence: set[int] = set()
        erased_turns: set[str] = set()
        entries = db.execute("SELECT * FROM memory_entries WHERE user_id=? AND status NOT IN ('deleted','retracted')",
                             (scope.user_id,)).fetchall()
        generated = db.execute("SELECT * FROM memory_evidence WHERE user_id=? AND kind IN ('assistant_generated','delivery','playback','tool','environment')",
                               (scope.user_id,)).fetchall()
        # Citations always point to originals. Generated replies can still
        # repeat a selected memory; follow those explicit dependencies to closure.
        while True:
            before = (len(affected), len(erased_evidence))
            for row in entries:
                for source in json.loads(row["sources"]):
                    if source["id"] in erased_evidence:
                        affected.add(row["id"])
                        break
                    if source["id"] not in spans:
                        continue
                    start = source.get("start", originals[source["id"]].find(source.get("quote", "")))
                    end = source.get("end", start + len(source.get("quote", "")))
                    if any(start < right and end > left for left, right in spans[source["id"]]):
                        affected.add(row["id"])
                        break
            for row in generated:
                metadata = json.loads(row["metadata"])
                if (metadata.get("input_evidence_id") in spans
                        or metadata.get("generated_evidence_id") in erased_evidence
                        or metadata.get("turn_id") in erased_turns
                        or set(metadata.get("context_evidence_ids", [])) & (set(spans) | erased_evidence)
                        or set(metadata.get("memory_entry_ids", [])) & affected):
                    erased_evidence.add(row["id"])
                    if metadata.get("turn_id"):
                        erased_turns.add(metadata["turn_id"])
            if before == (len(affected), len(erased_evidence)):
                break
        status = "retracted" if reason == "correction" else "deleted"
        for row in entries:
            if row["id"] in affected:
                changed_characters.add(row["character_id"])
        # Keep only citation locations in tombstones. A record invalidated by
        # another deletion can itself be explicitly erased later, including
        # its other supporting fragments. No quote survives in the tombstone.
        for entry_id in affected:
            row = db.execute("SELECT sources FROM memory_entries WHERE id=?", (entry_id,)).fetchone()
            locations = [{key: source[key] for key in ("id", "start", "end") if key in source}
                         for source in json.loads(row["sources"])]
            db.execute("UPDATE memory_entries SET status=?,content='',sources=? WHERE id=?",
                       (status, json.dumps(locations), entry_id))
        for row in generated:
            if row["id"] not in erased_evidence:
                continue
            metadata = json.loads(row['metadata'])
            receipt = self._receipt_metadata(metadata, answer_text=row['content']
                if row['kind'] == 'assistant_generated' and row['status'] == 'active' else None)
            metadata.update(receipt)
            protected = (protected_generated or {}).get(row["id"], []) if reason == "correction" else []
            removed = self._subtract_spans([(0, len(row["content"]))], protected)
            if protected:
                content = row["content"]
                for start, end in sorted(removed, reverse=True):
                    content = content[:start] + "█" * (end - start) + content[end:]
                db.execute("UPDATE memory_evidence SET content=?,metadata=?,status='redacted' WHERE id=?",
                           (content, json.dumps(metadata), row["id"]))
            else:
                metadata = receipt
                db.execute("UPDATE memory_evidence SET content='',metadata=?,status=? WHERE id=?",
                           (json.dumps(metadata), status, row["id"]))
            for start, end in removed:
                db.execute("INSERT OR IGNORE INTO memory_exclusions VALUES (?,?,?,?,?)",
                           (row["id"], start, end, reason, time.time()))
            db.execute("UPDATE memory_evidence SET revision=revision+1 WHERE id=?", (row["id"],))
            changed_characters.add(row["character_id"])
        for character in changed_characters:
            self._bump(db, character, scope.user_id)
            processing.retire_unavailable(db, MemoryScope(character, scope.user_id))
        for context in db.execute("SELECT * FROM memory_context WHERE user_id=?", (scope.user_id,)).fetchall():
            notes = json.loads(context["notes"])
            kept = [note for note in notes if not any(
                source["id"] in erased_evidence or any(
                    source.get("start", 0) < end and source.get("end", 2**63 - 1) > start
                    for start, end in spans.get(source["id"], []))
                for source in note["sources"])]
            if kept != notes:
                db.execute("UPDATE memory_context SET notes=? WHERE character_id=? AND user_id=?",
                           (json.dumps(kept, ensure_ascii=False), context["character_id"], scope.user_id))
                self._bump(db, context["character_id"], scope.user_id)

    @staticmethod
    def _subtract_spans(ranges, retained) -> list[tuple[int, int]]:
        result = []
        for left, right in ranges:
            if left >= right:
                continue
            pieces = [(left, right)]
            for start, end in retained:
                pieces = [(a, b) for low, high in pieces for a, b in
                          ((low, min(high, start)), (max(low, end), high)) if a < b]
            result.extend(pieces)
        return sorted(set(result))

    def expire_raw(self, cutoff: float, *, publish=None) -> int:
        """Apply the chosen raw retention policy; verified memories remain."""
        from spica.runtime.context import is_domain_conversation
        with closing(self.store._connect()) as db:
            expiring = db.execute("SELECT * FROM memory_evidence WHERE recorded_at<? AND status!='expired'", (cutoff,)).fetchall()
        if not expiring:
            return 0
        # The local index may have recovered after intake. Recheck at this
        # existing write boundary before erasing the only correction text;
        # foreground selection remains read-only. Never hold a write lock
        # while the encoder may update its disposable vector cache.
        disputes = []
        for row in expiring:
            if (row['kind'] != 'user' or row['organized'] or row['status'] not in {'active', 'redacted'}
                    or is_domain_conversation(row['conversation_id'])):
                continue
            scope = MemoryScope(row['character_id'], row['user_id'], row['conversation_id'])
            entries = self.entries(scope, include_history=True)
            for original, related in self.pending_entry_relations(scope, entries, include_own=True,
                    only_source_id=row['id'], confirmed_only=True, publish=publish):
                disputes.append((scope, original, related))
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            expired = db.execute("SELECT * FROM memory_evidence WHERE status!='expired' AND id IN (SELECT value FROM json_each(?))",
                (json.dumps([row['id'] for row in expiring]),)).fetchall()
            ids = {row["id"] for row in expired}
            if not ids:
                return 0
            for scope, original, related in disputes:
                self._record_dispute(db, scope, original['id'], related, expected_revision=original['revision'])
            db.executemany("UPDATE memory_evidence SET content='',metadata='{}',status='expired',revision=revision+1 WHERE id=?", [(i,) for i in ids])
            # Citation locations survive, but verbatim raw excerpts obey the
            # same retention period as their original messages.
            rows = db.execute("SELECT id,sources FROM memory_entries WHERE status!='deleted'").fetchall()
            for row in rows:
                sources = json.loads(row["sources"])
                if any(source["id"] in ids for source in sources):
                    for source in sources:
                        if source["id"] in ids:
                            source.pop("quote", None)
                            source["availability"] = "expired"
                    db.execute("UPDATE memory_entries SET sources=? WHERE id=?",
                               (json.dumps(sources, ensure_ascii=False), row["id"]))
            for row in db.execute("SELECT * FROM memory_context").fetchall():
                notes = json.loads(row["notes"])
                for note in notes:
                    for source in note["sources"]:
                        if source["id"] in ids:
                            source.pop("quote", None)
                            source["availability"] = "expired"
                db.execute("UPDATE memory_context SET notes=? WHERE character_id=? AND user_id=?",
                           (json.dumps(notes, ensure_ascii=False), row["character_id"], row["user_id"]))
            for character, user in {(row["character_id"], row["user_id"]) for row in expired}:
                db.execute("UPDATE memory_processing SET version=version+1 WHERE character_id=? AND user_id=?", (character, user))
                processing.retire_unavailable(db, MemoryScope(character, user))
            return len(ids) if publish is None or publish(db) else 0

    @staticmethod
    def _entry(row) -> dict[str, Any]:
        result = dict(row)
        result["sources"] = json.loads(result["sources"])
        result["replaces"] = json.loads(result["replaces"])
        return result

    @staticmethod
    def _row(row) -> dict[str, Any]:
        result = dict(row)
        result["metadata"] = json.loads(result["metadata"])
        return result
