"""One personal memory entrance over durable evidence and service-owned processing."""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import CancelledError
from threading import Event
from typing import Any

from spica.conversation.character_compat import DEFAULT_INTERLOCUTOR_NAME
from spica.core.character import character_memory_prefix
from memory.evidence import EvidenceJournal
from memory.consolidation import MemoryConsolidation
from memory.semantic import MemorySemanticIndex
from spica.ports.model import BoundModel
from spica.config.schema import MemoryConfig
from spica.ports.memory import MemoryItem, MemoryRecall, MemoryScope, MemorySourceQuote, MemorySourceTime

logger = logging.getLogger(__name__)

_SUPPORTED = {"commit_turn", "retrieve", "evidence", "maintenance"}


def scoped_conversation_id(character_id: str, conversation_id: str | None) -> str:
    # The single definition of the long-term-memory namespace key (Phase 7): the
    # store key is namespaced by character_id so different characters never see
    # each other's memories. ChatEngine's manual remember/list/clear reuse this
    # instead of re-hardcoding the "::" format.
    return character_memory_prefix(character_id) + (conversation_id or "default")


class SqliteMemoryAdapter:
    name = "sqlite"

    def __init__(self, store: Any, recent: Any | None = None, *, max_active_memories: int = 200,
                 memory_config: MemoryConfig | None = None) -> None:
        self.store = store
        self.recent = recent
        self.max_active_memories = max_active_memories
        self.config = memory_config or MemoryConfig()
        self._journal: EvidenceJournal | None = None
        self._maintenance: MemoryConsolidation | None = None
        self._semantic = MemorySemanticIndex(store, self.config.embedding_model_dir)

    @property
    def journal(self) -> EvidenceJournal:
        if self._journal is None:
            self._journal = EvidenceJournal(self.store, shared_fact_characters=self.config.shared_fact_characters,
                semantic_scores=lambda query, entries, **kwargs: self._semantic.scores(query, entries, **kwargs))
        return self._journal

    def record_evidence(self, scope: MemoryScope, **event: Any) -> int:
        from spica.runtime.context import is_domain_conversation
        if is_domain_conversation(scope.conversation_id or ''):
            event['metadata'] = {**event.get('metadata', {}),
                                 'memory_domain': scope.conversation_id.split('::', 1)[0]}
        evidence_id = self.journal.append(scope, **event)
        if event.get('kind') == 'user' and not is_domain_conversation(scope.conversation_id or ''):
            entries = self.journal.entries(scope, include_history=True)
            for original, disputed in self.journal.pending_entry_relations(scope, entries,
                    include_own=True, only_source_id=evidence_id, confirmed_only=True):
                self.journal.record_dispute(scope, original['id'], disputed)
        if self._maintenance is not None:
            # Urgency is reconstructed from still-schedulable originals, not
            # from a notification which may replay an already processed/held ID.
            self._maintenance.notify(scope)
        return evidence_id

    def working_context(self, scope: MemoryScope, query: str, *, exclude_turn_id: str | None = None,
                        token_budget: int | None = None, event_binding=None,
                        cancelled: Event | None = None) -> dict:
        try:
            if cancelled is not None and cancelled.is_set():
                raise CancelledError('memory retrieval cancelled')
            from spica.runtime.context import is_domain_conversation
            budget = self.config.context_token_budget if token_budget is None else max(0, token_budget)
            pending = self._pending_shared_entries(scope, self.journal.entries(scope, include_history=True))
            context = self.journal.working_context(scope, query, token_budget=budget,
                exclude_turn_id=exclude_turn_id, pending_entry_ids=pending,
                similarity=self._semantic.similarity_for(query, cancelled=cancelled),
                event_binding=event_binding, cancelled=cancelled,
                recent_turn_limit=self.config.recent_context_limit
                    if is_domain_conversation(scope.conversation_id or '') else 2)
            if pending:
                context['shared_updates_pending'] = True
            return context
        except CancelledError:
            raise
        except Exception as exc:
            logger.warning("working context unavailable: %s", type(exc).__name__)
            return {"messages": [], "status": "unavailable"}

    def clear_context(self, scope: MemoryScope, *, clear_long_term: bool = False) -> None:
        self.journal.clear_context(scope, clear_long_term=clear_long_term,
                                   delete_source=self.config.delete_source_on_forget)
        if clear_long_term:
            self._clear_legacy_domain_context(scope)

    def evidence(self, scope: MemoryScope, *, ids: list[int] | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self.journal.read(scope, ids=ids, limit=limit)

    def context_is_current(self, scope: MemoryScope, dependencies: dict) -> bool:
        disputed = self.journal.disputed_entry_ids(scope)
        if disputed.intersection(dependencies.get('memory_entry_ids', [])):
            return False
        if disputed and dependencies.get('context_evidence_ids'):
            sources = {source['id'] for entry in self.journal.entries(scope, include_history=True)
                       if entry['id'] in disputed for source in entry['sources']}
            if sources.intersection(dependencies['context_evidence_ids']):
                return False
            remaining, checked = set(dependencies['context_evidence_ids']), set()
            while remaining:
                selected = self.journal.read(scope, ids=list(remaining), limit=len(remaining))
                checked.update(remaining)
                remaining = set()
                for row in selected:
                    metadata = row['metadata']
                    if disputed.intersection(metadata.get('memory_entry_ids', [])):
                        return False
                    refs = set(metadata.get('context_evidence_ids', []))
                    refs.update(value for value in (metadata.get('input_evidence_id'), metadata.get('generated_evidence_id')) if value is not None)
                    if refs & sources:
                        return False
                    remaining.update(refs - checked)
        return self.journal.context_is_current(scope, dependencies)

    def remember(self, scope: MemoryScope, content: str, *, category: str = "user", importance: float = 0.8) -> int:
        entry_id = self.journal.remember(scope, content, subject=category)
        if self._maintenance is not None:
            self._maintenance.notify(scope)
        return entry_id

    def list_memory(self, scope: MemoryScope, *, limit: int = 50, include_history: bool = False,
                    before_id: int | None = None) -> list[dict]:
        return self.journal.entries(scope, include_history=include_history, limit=max(1, limit), before_id=before_id)

    def forget(self, scope: MemoryScope, memory_id: int) -> None:
        self.journal.forget(scope, memory_id, delete_source=self.config.delete_source_on_forget)
        self._clear_legacy_domain_context(scope)

    def revise(self, scope: MemoryScope, memory_id: int, content: str, *, reason: str = "correction") -> int:
        entry_id = self.journal.remember(scope, content, replaces=memory_id, reason=reason)
        self._clear_legacy_domain_context(scope)
        if self._maintenance is not None:
            self._maintenance.notify(scope, immediate=True)
        return entry_id

    def _clear_legacy_domain_context(self, scope: MemoryScope) -> None:
        # Pre-migration domain buffers have no dependency locators. They no
        # longer feed this provider; also discard these transient copies when
        # the person explicitly withdraws/corrects their personal memory.
        if self.recent is None:
            return
        from spica.runtime.context import is_domain_conversation
        prefix = character_memory_prefix(scope.character_id)
        for key in self.recent.dump():
            if key.startswith(prefix) and is_domain_conversation(key[len(prefix):]):
                self.recent.clear(key)

    def expire_raw(self, *, now: float | None = None) -> int:
        import time
        return self.journal.expire_raw((time.time() if now is None else now) - self.config.raw_retention_days * 86400)

    def export_archive(self, *, character_id: str | None = None) -> dict:
        from memory.transfer import export_archive
        self.expire_raw()
        return export_archive(self.journal, character_id=character_id)

    def restore_archive(self, archive: dict, *, character_id: str) -> dict:
        from memory.transfer import merge_archive
        result = merge_archive(self.journal, archive, character_id=character_id, empty_only=True)
        self.expire_raw()
        return result

    def record_presentation(self, scope: MemoryScope, turn_id: str, *, outcome: str, endpoint: str,
                            audio_units: int = 0) -> int | None:
        from contextlib import closing
        import json
        with closing(self.store._connect()) as db:
            original = db.execute('SELECT id,conversation_id,metadata FROM memory_evidence WHERE character_id=? AND user_id=? AND event_id=?',
                                  (scope.character_id,scope.user_id,turn_id+':user')).fetchone()
            generated = db.execute('SELECT id FROM memory_evidence WHERE character_id=? AND user_id=? AND event_id=?',
                                   (scope.character_id,scope.user_id,turn_id+':assistant-generated')).fetchone()
        if original is None:
            return None  # domain/group turns keep their own storage owner
        if endpoint != 'desktop':
            raise ValueError('standalone memory accepts desktop presentation receipts only')
        payload = {'outcome': outcome, 'endpoint': 'desktop', 'perceived_by_user': 'unconfirmed',
                   'authority': 'local_presentation', 'basis': 'local_presentation'}
        return self.record_evidence(MemoryScope(scope.character_id,scope.user_id,original['conversation_id']),
            event_id=turn_id+':presentation',kind='playback',source=endpoint or 'unknown',modality='system',
            content=json.dumps(payload),
            metadata={**{key: value for key, value in json.loads(original['metadata']).items() if key in {'material_hint', 'event_binding'}},
                      **payload,'turn_id':turn_id,'input_evidence_id':original['id'],
                      'generated_evidence_id':generated['id'] if generated else None})

    def start_maintenance(self, model: BoundModel, *, idle_seconds: float = 30.0) -> None:
        if self._maintenance is not None:
            raise RuntimeError("memory maintenance already started")
        self._maintenance = MemoryConsolidation(self.journal,
            model if self.config.consolidation_enabled else None, idle_seconds=idle_seconds,
            token_budget=self.config.context_token_budget, retention_days=self.config.raw_retention_days,
            min_user_turns=self.config.consolidation_min_user_turns,
            min_tokens=self.config.consolidation_min_tokens,
            max_attempts=self.config.consolidation_max_attempts,
            input_limit=self.config.consolidation_input_limit,
            extract_output_limit=self.config.consolidation_extract_output_limit,
            verify_output_limit=self.config.consolidation_verify_output_limit,
            candidate_limit=self.config.consolidation_candidate_limit,
            sparse_seconds=self.config.consolidation_sparse_seconds)

    def wait_for_maintenance(self, *, timeout: float = 5.0) -> bool:
        return self._maintenance is not None and self._maintenance.wait(timeout)

    def maintenance_status(self, scope: MemoryScope) -> dict:
        status = self._maintenance.status(scope) if self._maintenance else self.journal.status(scope)
        return dict(status, auto_enabled=self.config.consolidation_enabled,
                    max_attempts=self.config.consolidation_max_attempts)

    def shutdown(self, timeout: float = 1.5) -> bool:
        return self._maintenance is None or self._maintenance.shutdown(timeout)

    def _scoped_conversation_id(self, scope: MemoryScope) -> str:
        return scoped_conversation_id(scope.character_id, scope.conversation_id)

    def commit_turn(
        self,
        scope: MemoryScope,
        user_text: str,
        assistant_text: str,
        meta: dict | None = None,
    ) -> dict:
        from uuid import uuid4
        meta = meta or {}
        scope = MemoryScope(scope.character_id, scope.user_id,
                            meta.get("conversation_id") or scope.conversation_id)
        turn_id = meta.get("evidence_turn_id") or uuid4().hex
        ids = []
        if user_text:
            kind = "system_event" if meta.get("interaction_mode") == "system" else "user"
            ids.append(self.record_evidence(
                scope, event_id=turn_id + ":user", kind=kind, content=user_text,
                source=meta.get("input_source", "local"), modality=meta.get("input_modality", "text"),
                metadata=meta.get('input_evidence_metadata', {'turn_id': turn_id}),
            ))
        if assistant_text:
            ids.append(self.record_evidence(scope, event_id=turn_id + ":assistant-generated",
                       kind="assistant_generated", content=assistant_text, source="model", modality="system",
                       metadata=meta.get('assistant_evidence_metadata',
                           {'turn_id': turn_id, 'input_evidence_id': ids[0] if user_text else None})))
        return {"memory_evidence_ids": ids, "memory_candidates": 0, "saved_memory_ids": [], "pruned_memories": 0}

    def _pending_shared_entries(self, scope: MemoryScope, entries: list[dict]) -> set[int]:
        blocked = self.journal.disputed_entry_ids(scope)
        for _, related in self.journal.pending_entry_relations(scope, entries):
            blocked.update(related)
        return blocked

    def retrieve(self, scope: MemoryScope, query: str, limit: int) -> MemoryRecall:
        try:
            keywords = self.store._keywords(query)
            if not keywords:
                return MemoryRecall()
            from memory.evidence import historical_query
            historical = historical_query(query)
            now = time.time()
            rows = [row for row in self.journal.entries(scope, include_history=historical)
                    if historical or (row.get("valid_from") is None or row["valid_from"] <= now)
                    and (row.get("valid_until") is None or row["valid_until"] > now)]
            semantic_available = True
            try:
                # A routine sign-off can dominate short embeddings and pull
                # in unrelated old afternoons. Search the substantive clause.
                semantic_query = re.sub(r'[,，。]?\s*(?:今天|今日)?(?:就这样吧|就到这里吧|先这样吧)[。！!]*$', '', query).strip()
                scores = self._semantic.scores(semantic_query or query, rows)
            except Exception as exc:
                logger.warning("semantic memory recall unavailable: %s", type(exc).__name__)
                scores, semantic_available = {}, False
            candidates = []
            for row in rows:
                haystack = self.store._normalize_for_search(row["content"])
                hits = sum(keyword in haystack for keyword in keywords)
                score = scores.get(row["id"], 0.0)
                # MiniLM's measured ZH/JA contrast supports empty recall for
                # unrelated small talk. Lexical matches can recover names and
                # exact task IDs without selecting arbitrary important facts.
                if semantic_available and not (score >= .42 or hits and score >= .28):
                    continue
                if not semantic_available and not hits:
                    continue
                candidates.append((score + min(hits, 4) * .025 if semantic_available else float(hits), row))
            candidates.sort(key=lambda item: (item[0], item[1]["id"]), reverse=True)
            current_rows = {row["id"]: row for row in self.journal.entries(scope, include_history=historical)}
            pending = self._pending_shared_entries(scope, list(current_rows.values()))
            waiting_for_update = any(row['id'] in pending for _, row in candidates)
            # Maintenance can expire original quotes while the encoder runs.
            # Reuse scores, but return the latest visible record and fragments.
            candidates = [(score, current_rows[row["id"]]) for score, row in candidates
                          if row["id"] in current_rows and row['id'] not in pending][:max(1, limit)]
            source_ids = sorted({source["id"] for _, row in candidates for source in row["sources"]})
            originals = {row["id"]: row for row in self.evidence(scope, ids=source_ids, limit=len(source_ids))}
            items = []
            for score, row in candidates:
                quotes = []
                source_times = {}
                for source in row["sources"]:
                    original = originals.get(source["id"])
                    if original and original['status'] in {'active', 'redacted', 'expired'}:
                        source_times[original['id']] = MemorySourceTime(original['id'], original['recorded_at'],
                                                                       original.get('occurred_at'))
                    quote = source.get("quote")
                    if not original or not quote or original["status"] not in {"active", "redacted"}:
                        continue
                    # Evidence reads preserve offsets while hiding withdrawn
                    # spans. A duplicate surviving phrase cannot revive this span.
                    start, end = source.get("start"), source.get("end")
                    if start is None or end is None or original["content"][start:end] != quote:
                        continue
                    receipt = {}
                    incomplete = (original['kind'] == 'assistant_generated'
                                  and original['metadata'].get('generation_complete') is False)
                    if original["kind"] in {"delivery", "playback"} or incomplete:
                        receipt = {key: original["metadata"][key] for key in
                            ("authority", "basis", "outcome", "perceived_by_user", "device_rendering")
                            if isinstance(original["metadata"].get(key), str)}
                        receipt.setdefault("perceived_by_user", "unconfirmed")
                        if incomplete:
                            receipt['generation'] = 'incomplete'
                    quotes.append(MemorySourceQuote(source["id"], quote, original["kind"],
                        original["source"], original["modality"], tuple(receipt.items())))
                items.append(MemoryItem(
                    text=row["content"], score=float(score), type=row["kind"], id=row["id"],
                    scope=row["subject"],
                    source_ids=tuple(sorted({source["id"] for source in row["sources"]})),
                    revision=row["revision"], revision_reason=row["revision_reason"],
                    status=row["status"], valid_from=row.get("valid_from"), valid_until=row.get("valid_until"),
                    source_quotes=tuple(quotes),
                    source_times=tuple(source_times.values()),
                ))
            return MemoryRecall(items=tuple(items), status='unavailable' if waiting_for_update or not semantic_available else
                                "matched" if items else "no_match")
        except Exception as exc:
            logger.warning("memory retrieval unavailable: %s", type(exc).__name__)
            return MemoryRecall(status="unavailable")

    def rebuild_index(self) -> int:
        self.journal
        return self._semantic.rebuild()

    def get_context_block(self, scope: MemoryScope) -> str | None:
        # SQLite backend injects memories via the prompt's [LONG_TERM_MEMORY]
        # section already; no separate profile block.
        return None

    def run_maintenance(self, scope: MemoryScope, reason: str) -> None:
        if reason == 'resume' and self._maintenance is None:
            self.journal.resume_processing(scope)
            return
        if self._maintenance is not None:
            if reason == 'resume':
                self._maintenance.resume(scope)
            else:
                self._maintenance.notify(scope)

    def supports(self, capability: str) -> bool:
        return capability in _SUPPORTED
