"""Persist admitted input and generated results through the existing memory port.

The evidence journal owns raw dialogue; it does not duplicate it in recent.json.
Legacy memory providers retain their synchronous recent-buffer/background commit
contract. Game dialogue stays in its own domain; only the host publishes bounded,
source-labelled game history cards to personal recall. Generated text is distinct
from the later desktop presentation receipt. Qt-free.
"""

from __future__ import annotations

import logging
import hashlib
import copy
from dataclasses import asdict, replace
from typing import Any, Callable

from spica.conversation.time_context import format_local_time_for_prompt
from agent_tools.function_tools.screen.schema import screen_observation_context_for_next_turn
from spica.runtime.context import StreamedAnswer, TurnContext, TurnError, is_domain_conversation
from spica.runtime.deps import TurnDeps
from spica.runtime.jobs import InlineJobRunner
from spica.runtime.scope import MemoryScopeStrategy
from spica.ports.memory import EvidenceAdmissionError

logger = logging.getLogger(__name__)


def record_turn_input(ctx: TurnContext, deps: Any) -> None:
    """Persist admitted personal input before generation can fail or disconnect."""
    if getattr(deps, 'defer_prepared_turn', None) is not None or not ctx.request.user_input.strip():
        return
    record = getattr(deps.memory, "record_evidence", None)
    if not callable(record):
        return
    scope = MemoryScopeStrategy(deps.config).evidence_scope(ctx.request, deps.personal_owner_id)
    metadata = {"turn_id": ctx.request.evidence_turn_id, "runtime_turn_id": ctx.request.runtime_turn_id,
                **({'material_hint': asdict(ctx.request.material_hint)} if ctx.request.material_hint is not None else {}),
                **({'event_binding': asdict(ctx.request.event_binding)} if ctx.request.event_binding is not None else {}),
                **({'memory_domain': ctx.request.conversation_id.split('::', 1)[0]}
                   if is_domain_conversation(ctx.request.conversation_id) else {}),
                }
    try:
        ctx.metadata["input_evidence_id"] = record(
            scope, event_id=ctx.request.evidence_turn_id + ":user",
            kind="system_event" if ctx.request.interaction_mode == "system" else "user",
            content=ctx.user_input,
            source=ctx.request.input_source,
            modality="system" if ctx.request.interaction_mode == "system" else ctx.request.input_modality,
            metadata=metadata,
            reject_withdrawn=True,
        )
        ctx.metadata['input_evidence_metadata'] = copy.deepcopy(metadata)
        if ctx.request.event_binding is not None and ctx.request.interaction_mode == 'system':
            import json
            record_turn_result(ctx, deps, kind='environment',
                content=json.dumps(asdict(ctx.request.event_binding), ensure_ascii=False),
                event_suffix='business-context', source=ctx.request.event_binding.kind,
                metadata={'actor': 'business'})
    except EvidenceAdmissionError:
        ctx.error = TurnError("STALE_INPUT", "这条旧消息的资料已被修改或删除，请发送一条新消息。")
    except Exception as exc:
        logger.warning("input evidence could not be persisted: %s", type(exc).__name__)
        ctx.metadata["memory_error"] = "input evidence could not be persisted"
        ctx.error = TurnError('EVIDENCE_UNAVAILABLE','这条消息暂时无法可靠保存，请稍后重新发送。')


def record_turn_result(ctx: TurnContext, deps: Any, *, kind: str, content: str,
                       event_suffix: str, source: str, metadata: dict | None = None,
                       defer: Callable[[Callable[[], int]], None] | None = None) -> dict | None:
    if getattr(deps, 'defer_prepared_turn', None) is not None or not content:
        return
    record = getattr(deps.memory, "record_evidence", None)
    if not callable(record):
        return
    scope = MemoryScopeStrategy(deps.config).evidence_scope(ctx.request, deps.personal_owner_id)
    result_metadata = {
        **({'material_hint': asdict(ctx.request.material_hint)} if ctx.request.material_hint is not None else {}),
        **({'event_binding': asdict(ctx.request.event_binding)} if ctx.request.event_binding is not None else {}),
        "input_evidence_id": ctx.metadata.get("input_evidence_id"),
        "context_evidence_ids": ctx.metadata.get("context_evidence_ids", []),
        "context_evidence_revisions": ctx.metadata.get("context_evidence_revisions", {}),
        "memory_entry_ids": ctx.metadata.get("memory_entry_ids", []),
        **(metadata or {}),
    }
    if is_domain_conversation(ctx.request.conversation_id):
        result_metadata['memory_domain'] = ctx.request.conversation_id.split('::', 1)[0]
    # Tool arguments/results and observations are derived from this same
    # supplied context. Later results also depend on earlier tool outputs.
    result_metadata["context_evidence_ids"] = sorted(set(result_metadata["context_evidence_ids"])
        | set(ctx.metadata.get("result_evidence_ids", [])))
    result_metadata = copy.deepcopy({"turn_id": ctx.request.evidence_turn_id, **result_metadata})
    event_id = ctx.request.evidence_turn_id + ':' + event_suffix
    def write():
        return record(scope, event_id=event_id, kind=kind, content=content, source=source,
                      modality='tool' if kind == 'tool' else 'system', metadata=result_metadata)
    try:
        if defer is not None:
            defer(write)
        else:
            evidence_id = write()
            if kind in {"tool", "environment"}:
                ctx.metadata.setdefault("result_evidence_ids", []).append(evidence_id)
    except Exception as exc:
        logger.warning("result evidence could not be persisted: %s", type(exc).__name__)
        ctx.metadata["memory_error"] = "result evidence could not be persisted"
    return result_metadata


def save_incomplete_stream_evidence(ctx: TurnContext, deps: Any, fragments: list[str],
                                    emitted_units: list[dict]) -> None:
    """Keep emitted answer fragments as incomplete evidence, never a completed turn.

    These are parsed, whole presentation units. Unemitted JSON/token tails and
    the legacy recent/extraction/deferred-delivery paths do not enter this write.
    """
    record_turn_result(ctx, deps, kind='assistant_generated', content=''.join(fragments),
        event_suffix='assistant-generated', source='model',
        metadata={'generation_complete': False, 'outcome': 'cancelled', 'basis': 'emitted_units',
                  'emitted_units': emitted_units},
        defer=deps.defer_incomplete_evidence)


def save_stream_memory(ctx: TurnContext, services: Any, deps: Any = None) -> None:
    if ctx.error and ctx.error.code in {"STALE_INPUT",'EVIDENCE_UNAVAILABLE'}:
        return
    deps = deps or TurnDeps.from_legacy_services(services)
    if getattr(deps, 'defer_prepared_turn', None) is not None:
        if ctx.error is not None or ctx.request.cancelled and ctx.request.cancelled.is_set():
            return
        snapshot = TurnContext(request=ctx.request, user_input=ctx.user_input,
            user_local_time=copy.deepcopy(ctx.user_local_time), answer=copy.deepcopy(ctx.answer),
            screen_observation=copy.deepcopy(ctx.screen_observation), recent=copy.deepcopy(ctx.recent),
            metadata=copy.deepcopy(ctx.metadata))
        delivered_deps = replace(deps, defer_prepared_turn=None, jobs=InlineJobRunner())
        scope = MemoryScopeStrategy(deps.config).evidence_scope(ctx.request, deps.personal_owner_id)
        dependencies = {key: snapshot.metadata.get(key, [] if key != 'context_evidence_revisions' else {})
                        for key in ('context_evidence_ids', 'context_evidence_revisions', 'memory_entry_ids')}

        def current():
            validate = getattr(deps.memory, 'context_is_current', None)
            return not callable(validate) or validate(scope, dependencies)

        def commit(evidence_turn_id):
            delivered = TurnContext(request=replace(snapshot.request, evidence_turn_id=evidence_turn_id, cancelled=None),
                user_input=snapshot.user_input, user_local_time=copy.deepcopy(snapshot.user_local_time),
                answer=copy.deepcopy(snapshot.answer), screen_observation=copy.deepcopy(snapshot.screen_observation),
                recent=copy.deepcopy(snapshot.recent), metadata=copy.deepcopy(snapshot.metadata))
            record_turn_input(delivered, delivered_deps)
            save_stream_memory(delivered, None, delivered_deps)
            if delivered.error or delivered.metadata.get('memory_error'):
                raise RuntimeError('prepared speech delivery evidence not saved')
            return dict(delivered.metadata)

        deps.defer_prepared_turn(commit, current)
        return
    strategy = MemoryScopeStrategy(deps.config)
    answer_text = (ctx.answer.answer if ctx.answer else None) or ""
    generated_metadata = record_turn_result(ctx, deps, kind="assistant_generated", content=answer_text,
                       event_suffix="assistant-generated", source="model", metadata={
                           "answer_sha256": hashlib.sha256(answer_text.encode()).hexdigest(),
                           "answer_text_sha256": hashlib.sha256(answer_text.replace('\r\n', '\n').replace('\r', '\n').strip().encode()).hexdigest(),
                           "generation_complete": ctx.error is None and not (ctx.request.cancelled and ctx.request.cancelled.is_set()),
                           "input_evidence_id": ctx.metadata.get("input_evidence_id"),
                           "context_evidence_ids": ctx.metadata.get("context_evidence_ids", []),
                           "context_evidence_revisions": ctx.metadata.get("context_evidence_revisions", {}),
                           "memory_entry_ids": ctx.metadata.get('memory_entry_ids', []),
                       })
    # recent_memory append is SYNCHRONOUS and must complete before `done` (N4-memory).
    # Phase 2: the bucket key is character-scoped via the strategy -- the SAME
    # derivation stages.load_recent_context_node reads with, so write/read symmetry
    # is structural.
    if not callable(getattr(deps.memory, "working_context", None)):
        # Legacy providers retain their buffer contract. The evidence-backed
        # provider also owns domain dialogue copies, in their original scope,
        # so withdrawal and retention cannot leave a second recent.json copy.
        try:
            deps.recent.append_turn(
                strategy.recent_key(ctx.request), ctx.user_input, answer_text,
                user_local_time=(format_local_time_for_prompt(ctx.user_local_time)
                                 if ctx.request.include_user_time_context else None),
                interaction_mode=ctx.request.interaction_mode,
                screen_observation_context=screen_observation_context_for_next_turn(ctx.screen_observation),
            )
        except Exception as exc:
            logger.warning("memory commit failed (recent append): %s", exc, exc_info=True)
            ctx.metadata["memory_error"] = str(exc)

    if is_domain_conversation(ctx.request.conversation_id):
        # The sourced domain input/results above are already durable. Do not
        # send the exchange through a legacy personal extractor as well.
        return

    # The journal commit is idempotent by evidence turn ID. Legacy providers can
    # continue their own extraction; neither path blocks the presentation lane.
    scope = strategy.ltm_scope(ctx.request, deps.personal_owner_id)
    user_text = ctx.user_input
    meta = {
        "interlocutor_name": deps.config.character.interlocutor_name or scope.user_id,
        "max_active_memories": deps.config.memory.max_long_term_memories,
        "evidence_turn_id": ctx.request.evidence_turn_id,
        "conversation_id": ctx.request.conversation_id,
        "interaction_mode": ctx.request.interaction_mode,
        "input_source": ctx.request.input_source,
        "input_modality": "system" if ctx.request.interaction_mode == "system" else
            ctx.request.input_modality,
        **({'input_evidence_metadata': copy.deepcopy(ctx.metadata['input_evidence_metadata'])}
           if 'input_evidence_metadata' in ctx.metadata else {}),
        **({'assistant_evidence_metadata': generated_metadata} if generated_metadata is not None else {}),
    }

    def _commit_long_term() -> None:
        try:
            result = deps.memory.commit_turn(scope, user_text, answer_text, meta=meta)
            ctx.metadata.update(result)
        except Exception as exc:
            logger.warning("memory commit failed (long-term): %s", exc, exc_info=True)
            ctx.metadata["memory_error"] = str(exc)

    deps.jobs.submit(_commit_long_term)
