"""PromptContextContributor protocol (OO migration Phase 3).

The seam that turns "inject domain context into the prompt" from a three-high-
risk-file edit into "a new file in the domain package + registration":

- ``mode(request)`` is the GATE. It takes ONLY the TurnRequest -- never ctx or
  deps -- so a contributor structurally cannot do DB reads or open spans while
  deciding whether a plain chat turn concerns it (the narrow signature is the
  structural defence; a wide one would only be a disciplinary one). Domain
  runtime state reaches the gate the legitimate way: a binding published into
  the request (e.g. ``GameTurnBinding`` -> ``game_context_request``).
- ``sections(ctx, deps, mode)`` is the DISPLAY half: return the prompt sections
  to append. Missing domain state (e.g. ``deps.game_memory is None``) must
  return ``[]`` -- the node keeps today's timing semantics regardless.

Registration: ``TurnDeps.context_contributors`` (a tuple). ``None`` triggers the
galgame compatibility auto-fill in ``TurnDeps.__post_init__`` (never grows a
second entry); explicit ``()`` disables injection; future domains must register
the FULL tuple explicitly via assembly. The consuming node is
``stages.contribute_context_node`` (permanent alias:
``retrieve_game_context_node``), which iterates contributors in tuple order --
multi-contributor ordering/telemetry semantics are deferred to the Phase 8
design (Open Questions #2); ``priority`` is declared now so the protocol does
not change shape then.

Structural typing: implementations (e.g.
``spica/galgame/context_contributor.py``) do NOT need to import this module.
Typing-only imports here -- no spica dependencies, no new package edges.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

ContextMode = Literal["active", "offline", "none"]


@runtime_checkable
class PromptContextContributor(Protocol):
    """One domain's gated prompt-context injection."""

    name: str
    priority: int

    def mode(self, request: Any) -> ContextMode:
        """Pure request-field gate: does this turn concern the domain, and how?"""
        ...

    def sections(self, ctx: Any, deps: Any, mode: ContextMode) -> list[str]:
        """The prompt sections to append for this turn (``[]`` = inject nothing)."""
        ...
