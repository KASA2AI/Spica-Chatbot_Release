"""Character/memory scoping (OO migration Phase 2).

The SINGLE home for two things that used to be scattered:

- the identity default fallbacks ("spica" / DEFAULT_INTERLOCUTOR_NAME) --
  ``DEFAULT_CHARACTER_ID`` lives here; the user-side default deliberately
  REUSES ``character_loader.DEFAULT_INTERLOCUTOR_NAME`` so "麦" keeps exactly one
  home too;
- the read/write/clear key symmetry for per-character memory --
  ``MemoryScopeStrategy`` is what makes "retrieve and commit use the same scope"
  a structural fact instead of a comment-level discipline.

LIVE-READ semantics (v2 correction, decided): every method resolves
``config.character`` AT CALL TIME -- the strategy holds only the config
reference, never a value snapshot. ``ChatEngine.set_interlocutor_name`` renames
by mutating that same ``AppConfig`` in place, so a rename is visible to the very
next turn's scope. For the same reason the resolved scope is NOT parked on
``TurnDeps`` (freezing user_id there would change post-rename CompanionBeat
retrieval; PersonaRuntime revisits this).

The ``"::"`` key format has ONE definition: ``scoped_conversation_id`` in
``spica/adapters/memory/sqlite.py`` (an established runtime->adapters import,
same as deps.py / chat_engine.py -- reused here, adapter unmodified).

Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from spica.adapters.memory.sqlite import scoped_conversation_id
from spica.config.schema import AppConfig
from spica.conversation.character_loader import DEFAULT_INTERLOCUTOR_NAME
from spica.ports.memory import MemoryScope

# The single home of the character-id fallback (Phase 2 exit condition: the
# bare-literal identity fallback pattern survives nowhere else under spica/).
DEFAULT_CHARACTER_ID = "spica"


@dataclass(frozen=True)
class CharacterScope:
    """Resolved character identity for one moment in time (frozen VALUE type --
    the live source is always a fresh ``character_scope_from_config`` call)."""

    character_id: str
    user_id: str


def character_scope_from_config(config: AppConfig) -> CharacterScope:
    """Resolve the current identity from config, applying the canonical defaults.

    Called per use (never cached) so an in-place ``config.character`` rename is
    reflected immediately -- this is the live-read half of the Phase 2 design.
    """
    return CharacterScope(
        character_id=str(config.character.character_id or DEFAULT_CHARACTER_ID),
        user_id=str(config.character.interlocutor_name or DEFAULT_INTERLOCUTOR_NAME),
    )


class MemoryScopeStrategy:
    """The one place that derives memory keys/scopes from a turn's identity.

    Three consumers, three methods:
    - ``recent_key`` -- the recent-context bucket (stages read + memory_commit
      write): ``{character_id}::{conversation_id}``. THE Phase 2 behaviour
      change -- previously the bare conversation_id, which let two characters
      sharing a conversation silently share short-term context.
    - ``ltm_scope`` -- the long-term MemoryScope (stages retrieve + memory_commit
      commit): triple-identical to the pre-Phase-2 hand-built scopes, including
      the §27① ``effective_memory_conversation_id`` fallback. Zero LTM change.
    - ``clear_targets`` -- what ``ChatEngine.clear_memory`` must clear on both
      sides. Today both slots coincide (same scoped id); kept as a pair so a
      future divergence has a typed seam instead of a silent split.

    Construction is free (holds only the config reference), so stages /
    memory_commit build one per call from ``deps.config``; ChatEngine keeps a
    single instance over its own (same) AppConfig object.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def _scope(self) -> CharacterScope:
        return character_scope_from_config(self._config)

    def recent_key(self, request: Any) -> str:
        return scoped_conversation_id(self._scope().character_id, request.conversation_id)

    def ltm_scope(self, request: Any) -> MemoryScope:
        scope = self._scope()
        return MemoryScope(
            character_id=scope.character_id,
            user_id=scope.user_id,
            conversation_id=request.effective_memory_conversation_id,
        )

    def clear_targets(self, conversation_id: str) -> tuple[str, str]:
        scoped = scoped_conversation_id(self._scope().character_id, conversation_id)
        return (scoped, scoped)
