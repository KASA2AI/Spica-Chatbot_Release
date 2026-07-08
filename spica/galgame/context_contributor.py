"""Galgame prompt-context contributor (OO migration Phase 3).

The GATE half of the galgame context injection, moved verbatim out of
``spica/runtime/stages.py``: mode decision (``_game_context_mode``) and target
resolution (``_resolve_game_target`` + the conversation-id parsers). The DISPLAY
half stayed in ``prompt_sections.py`` (Phase 1); the generic node
(``contribute_context_node``, permanent alias ``retrieve_game_context_node``)
stayed in ``stages.py``. This module implements the ``PromptContextContributor``
protocol STRUCTURALLY -- it deliberately does not import the Protocol.

Import boundary (AST-guarded by tests/test_prompt_context_contributors.py +
tests/test_layering.py): must NOT import ``spica.runtime.stages`` (stages'
deps auto-fill reaches us lazily -- a module-level reverse edge would be a
cycle), ``spica.galgame.session`` (the state owner has no business in a pure
gate), ``spica.core.events`` (N1), or Qt (CLAUDE.md #1).
``spica.runtime.scope`` is allowed: the galgame->runtime direction already
exists (session.py / companion_controller.py) and scope never imports back.

The ``galgame::`` prefix literal is a deliberate copy (NOT deduped against
``context.GALGAME_CONVERSATION_PREFIX`` / ``models.game_conversation_id``) --
the standing gate-immutability ruling, see GALGAME_FINDINGS.md #9.

Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from typing import Any

from spica.galgame.prompt_sections import _build_game_context_sections
from spica.runtime.scope import character_scope_from_config

_GALGAME_CONVERSATION_PREFIX = "galgame::"
_OFFLINE_COMMAND_INTENTS = frozenset(
    {"ask_last_progress", "ask_game_progress", "ask_character_relation"}
)


def _game_context_mode(request: Any) -> str:
    gcr = getattr(request, "game_context_request", None)
    conversation_id = getattr(request, "conversation_id", "") or ""
    if (
        getattr(request, "interaction_mode", "chat") == "galgame"
        or conversation_id.startswith(_GALGAME_CONVERSATION_PREFIX)
        or (gcr is not None and getattr(gcr, "mode", None) == "active")
    ):
        return "active"
    if getattr(request, "command_intent", None) in _OFFLINE_COMMAND_INTENTS or (
        gcr is not None and getattr(gcr, "mode", None) == "offline"
    ):
        return "offline"
    return "none"


def _parse_game_id_from_conversation(conversation_id: str) -> str | None:
    if not conversation_id.startswith(_GALGAME_CONVERSATION_PREFIX):
        return None
    parts = conversation_id.split("::")
    return parts[1] if len(parts) >= 2 and parts[1] else None


def _parse_playthrough_from_conversation(conversation_id: str) -> str | None:
    parts = conversation_id.split("::")
    if len(parts) >= 4 and parts[2] == "playthrough" and parts[3]:
        return parts[3]
    return None


def _resolve_game_target(
    request: Any, game_memory: Any, mode: str
) -> tuple[str | None, str, str | None]:
    gcr = getattr(request, "game_context_request", None)
    conversation_id = getattr(request, "conversation_id", "") or ""
    game_id = getattr(gcr, "game_id", None) if gcr is not None else None
    if not game_id:
        game_id = _parse_game_id_from_conversation(conversation_id)
    playthrough_id = getattr(gcr, "playthrough_id", None) if gcr is not None else None
    if not playthrough_id:
        playthrough_id = _parse_playthrough_from_conversation(conversation_id) or "default"
    # B1: the live session id rides on the typed gate request (the companion
    # controller stamps it into the published binding). Absent on the manual/debug
    # conversation-id path -> CURRENT_LINE is simply not read for that turn.
    session_id = getattr(gcr, "session_id", None) if gcr is not None else None
    if not game_id and mode == "offline":
        last = game_memory.last_played_game()
        if last is not None:
            game_id = last.game_id
            playthrough_id = last.active_playthrough_id or "default"
    return game_id, playthrough_id, session_id


class GalgameContextContributor:
    """The galgame domain's PromptContextContributor (structural)."""

    name = "galgame"
    priority = 0

    def mode(self, request: Any) -> str:
        return _game_context_mode(request)

    def sections(self, ctx: Any, deps: Any, mode: str) -> list[str]:
        # Missing game_memory (legacy/test deps, plain-chat assemblies) -> inject
        # nothing; the node still opened the span for an active/offline turn,
        # which preserves today's timing semantics byte for byte (golden #2(d)).
        game_memory = getattr(deps, "game_memory", None)
        if game_memory is None:
            return []
        game_id, playthrough_id, session_id = _resolve_game_target(ctx.request, game_memory, mode)
        if not game_id:
            return []
        # Phase 2: identity is resolved live per turn and passed down --
        # prompt_sections holds no default fallbacks and never resolves identity.
        return _build_game_context_sections(
            mode, game_memory, ctx.request, deps, game_id, playthrough_id, session_id,
            character_scope_from_config(deps.config),
        )


# The module-level instance the TurnDeps galgame auto-fill (and future explicit
# assembly registration) points at.
galgame_contributor = GalgameContextContributor()
