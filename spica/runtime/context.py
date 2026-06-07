"""Typed turn request (core C3a).

``TurnRequest`` is the typed entry for one turn -- the raw fields a caller used to
hand ``ChatEngine`` positionally, frozen into one object. C3a introduces it as the
public request shape while the runtime internals still run on ``AgentState``
(dismantled in C3c); ChatEngine builds a ``TurnRequest`` and bridges it to an
``AgentState`` for now.

The richer ``TurnContext`` (typed per-stage sub-objects replacing the AgentState
blackboard) lands in C3c; this module only defines the request half today.

Pure: no ``agent`` import, Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TurnRequest:
    """Everything a caller specifies to drive one turn."""

    user_input: str
    conversation_id: str = "default"
    emotion_override: str | None = None
    interaction_mode: str = "chat"
    include_user_time_context: bool = True
    screen_attachment: dict[str, Any] | None = None
    tts_param_overrides: dict[str, Any] | None = None
    visual_overrides: dict[str, Any] = field(default_factory=dict)
