"""Galgame domain layer.

Qt-free (CLAUDE.md #1). Session / FSM / OCR live in later phases.

NB: this package ``__init__`` re-exports the pure data models only. It must NOT
re-export ``manual.ManualGameMemory`` -- that facade imports ``GameMemoryPort``,
and since ``spica.ports.game_memory`` imports ``spica.galgame.models`` (which runs
this ``__init__``), re-exporting the facade here forms an import cycle. Import the
facade directly from ``spica.galgame.manual`` instead.
"""

from spica.galgame.models import (
    CharacterRelation,
    ChoiceEvent,
    CompanionBeat,
    GameProfile,
    GameProgressState,
    LaunchProfile,
    OCRProfile,
    OCRRegion,
    PlaySession,
    StoryLine,
    StoryLineStatus,
    StoryLineStatusError,
    StorySummary,
    WindowMatchRule,
    game_conversation_id,
    utc_now_iso,
)

__all__ = [
    "CharacterRelation",
    "ChoiceEvent",
    "CompanionBeat",
    "GameProfile",
    "GameProgressState",
    "LaunchProfile",
    "OCRProfile",
    "OCRRegion",
    "PlaySession",
    "StoryLine",
    "StoryLineStatus",
    "StoryLineStatusError",
    "StorySummary",
    "WindowMatchRule",
    "game_conversation_id",
    "utc_now_iso",
]
