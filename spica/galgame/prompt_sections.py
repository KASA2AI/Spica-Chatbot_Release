"""Galgame prompt section builders (OO migration Phase 1).

The DISPLAY half of the gated galgame context injection, moved verbatim out of
``spica/runtime/stages.py``: given a resolved game target, format the game-memory
reads into the ``[GAME_*]`` / ``[COMPANION_CONTEXT]`` prompt sections. The GATE
half (mode decision, target resolution) and the node itself
(``retrieve_game_context_node``) stay in ``stages.py`` -- this module never
decides WHETHER to inject, only WHAT the sections look like.

Import boundary (guarded by tests/test_layering.py): exactly ``json`` /
``typing.Any`` / ``DEFAULT_INTERLOCUTOR_NAME``. This module must not import
``spica.runtime.*`` (stages imports us -- the reverse edge would be a cycle),
``spica.galgame.session`` (the state owner has no business in a pure formatter),
``spica.core.events`` (N1: transform layers never produce RuntimeEvent), or Qt
(CLAUDE.md #1). ``game_memory`` / ``request`` / ``deps`` stay duck-typed ``Any``
for exactly this reason.

Byte parity with the pre-move behaviour is pinned by
tests/test_game_prompt_golden.py (Phase 0 golden #2).
"""

import json
from typing import Any

from spica.conversation.character_loader import DEFAULT_INTERLOCUTOR_NAME

_COMPANION_INTENT = "ask_companion_memory"
# Stage 2 (Path B): active mode also injects the most recent summaries -- a smaller
# limit than offline's prompt_context_recent_limit because EVERY companion turn pays
# the prompt cost. This
# bridges "summary fired -> buffer emptied -> details of 20 minutes ago vanish":
# progress.current_scene_summary is never written and major_events is titles only,
# so without summaries an active turn loses all summarized narrative.
_GAME_CONTEXT_ACTIVE_SUMMARY_LIMIT = 2


def _should_inject_companion(mode: str, request: Any) -> bool:
    if mode == "active":
        return True
    return getattr(request, "command_intent", None) == _COMPANION_INTENT


def _section(header: str, body: str) -> str:
    return f"{header}\n{body}"


def _format_progress(progress: Any) -> str:
    return json.dumps(
        {
            "chapter": progress.chapter,
            "route": progress.route,
            "location": progress.location,
            "current_scene_summary": progress.current_scene_summary,
            "major_events": progress.major_events,
            "unresolved_threads": progress.unresolved_threads,
            "last_played_at": progress.last_played_at,
        },
        ensure_ascii=False,
    )


def _format_summaries(summaries: list[Any]) -> str:
    return json.dumps(
        [
            {
                "summary_zh": s.summary_zh,
                "characters": s.characters,
                "major_events": s.major_events,
                "unresolved_threads": s.unresolved_threads,
                "created_at": s.created_at,
            }
            for s in summaries
        ],
        ensure_ascii=False,
    )


def _format_buffer(lines: list[Any]) -> str:
    # Compact: omit the speaker key when there is no speaker. OCR narration lines
    # carry speaker=None, and emitting `"speaker": null, ` for each is pure prompt
    # bloat (~17 chars/line) on a long backlog -- a reader gets the identical {text}
    # whether the null key is present or absent, so this is semantically lossless.
    return json.dumps(
        [
            {"speaker": line.speaker, "text": line.text} if line.speaker else {"text": line.text}
            for line in lines
        ],
        ensure_ascii=False,
    )


def _format_relations(relations: list[Any]) -> str:
    return json.dumps(
        [
            {
                "character_a": r.character_a,
                "character_b": r.character_b,
                "relation_summary": r.relation_summary,
                "confidence": r.confidence,
            }
            for r in relations
        ],
        ensure_ascii=False,
    )


def _format_choices(choices: list[Any]) -> str:
    return json.dumps(
        [
            {
                "options": ev.options,
                "selected_option_index": ev.selected_option_index,
                "selected_option_text": ev.selected_option_text,
                "selection_source": ev.selection_source,
            }
            for ev in choices
        ],
        ensure_ascii=False,
    )


def _format_beats(beats: list[Any]) -> str:
    return json.dumps(
        [{"type": b.type, "content": b.content, "source": b.source} for b in beats],
        ensure_ascii=False,
    )


def _build_game_context_sections(
    mode: str, game_memory: Any, request: Any, deps: Any, game_id: str, playthrough_id: str,
    session_id: str | None,
) -> list[str]:
    sections: list[str] = []

    progress = game_memory.get_progress_state(game_id, playthrough_id)
    if progress is not None:
        sections.append(_section("[GAME_PROGRESS]", _format_progress(progress)))

    if mode == "offline":
        summaries = game_memory.recent_summaries(game_id, playthrough_id, limit=deps.config.galgame.prompt_context_recent_limit)
        if summaries:
            sections.append(_section("[RECENT_GAME_SUMMARIES]", _format_summaries(summaries)))
    else:
        # Stage 2: recent summaries BEFORE the live buffer (past -> present reading
        # order, same section position as offline). Without this, anything already
        # summarized OUT of the buffer is invisible to an active companion turn.
        summaries = game_memory.recent_summaries(
            game_id, playthrough_id, limit=_GAME_CONTEXT_ACTIVE_SUMMARY_LIMIT
        )
        if summaries:
            sections.append(_section("[RECENT_GAME_SUMMARIES]", _format_summaries(summaries)))
        buffer_lines = game_memory.unsummarized_committed_story_lines(game_id, playthrough_id)
        # Tail cap (yaml: galgame.game_buffer_tail_limit): keep only the last N lines
        # so the live prompt does not grow unbounded with the unsummarized backlog
        # (older lines are covered by [RECENT_GAME_SUMMARIES] above). <=0 -> no cap
        # (byte-identical to pre-cap). Caps ONLY this prompt view; the summarizer
        # still reads the full backlog via the same adapter method.
        tail_limit = deps.config.galgame.game_buffer_tail_limit
        if tail_limit > 0 and len(buffer_lines) > tail_limit:
            buffer_lines = buffer_lines[-tail_limit:]
        if buffer_lines:
            sections.append(_section("[CURRENT_GAME_BUFFER]", _format_buffer(buffer_lines)))
        # B1: the line currently on screen (PENDING_CURRENT) -- not yet committed
        # into the buffer above. Reading order is past -> present: progress ->
        # summaries -> committed buffer -> the line right now. Scoped to this live
        # session_id (crash-residue isolation); status partitions it from the
        # COMMITTED buffer so the same line is never injected twice; omitted during
        # the brief commit gap (the line is in the buffer then). Active-only (the
        # else branch): offline has no live session.
        current_line = game_memory.current_pending_story_line(game_id, playthrough_id, session_id)
        if current_line is not None:
            sections.append(_section("[CURRENT_LINE]", _format_buffer([current_line])))

    relations = game_memory.character_relations(game_id, playthrough_id)
    if relations:
        sections.append(_section("[GAME_RELATIONS]", _format_relations(relations)))

    choices = game_memory.recent_choice_events(game_id, playthrough_id, limit=deps.config.galgame.prompt_context_recent_limit)
    if choices:
        sections.append(_section("[GAME_CHOICES]", _format_choices(choices)))

    if _should_inject_companion(mode, request):
        character_id = str(deps.config.character.character_id or "spica")
        user_id = str(deps.config.character.interlocutor_name or DEFAULT_INTERLOCUTOR_NAME)
        # P5 D-P5-6: the prompt reader EXCLUDES silent reaction beats -- they
        # accrue faster than spoken ones and would crowd her real words out.
        beats = game_memory.recent_companion_beats_for_prompt(
            game_id, user_id, character_id, limit=deps.config.galgame.prompt_context_recent_limit
        )
        if beats:
            sections.append(_section("[COMPANION_CONTEXT]", _format_beats(beats)))

    return sections
