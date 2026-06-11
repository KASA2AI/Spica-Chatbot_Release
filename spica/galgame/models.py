"""Galgame domain data models (Phase 1).

The 12 models from ``GALGAME_COMPANION_PLAN.md`` §9, as plain stdlib dataclasses.
This layer is pure data: NO SQL, NO session/FSM logic, NO Qt (CLAUDE.md #1).

Conventions locked in Phase 1:

- **Timestamps** are ISO-8601 *naive-UTC* strings, byte-identical in shape to
  ``memory/store.py`` (``datetime.utcnow().isoformat(timespec="seconds")``). Use
  ``utc_now_iso()`` here; never ``datetime.now()``, never a tz-aware suffix.
- **``route_key``** (v2 multi-route key) is materialized on exactly three models --
  ``PlaySession`` / ``StorySummary`` / ``GameProgressState`` -- and is **always
  ``None`` in v1** (§9 note, §13.6).
- **``StoryLine.status``** is a typed enum; transitions go through
  ``StoryLine.with_status`` which pins the legal graph and raises on anything else.

Nested JSON objects (``launch_profiles`` / ``window_match`` / ``ocr_profile`` /
``chapter`` / ``route`` / ``options`` / ``scope`` / ...) are kept as ``dict`` /
``list`` fields matching §9's JSON shape; the standalone ``LaunchProfile`` /
``WindowMatchRule`` / ``OCRProfile`` / ``OCRRegion`` dataclasses define the shape
of those dicts and serialize into them.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


# -- shared helpers -----------------------------------------------------------

def utc_now_iso() -> str:
    """Naive-UTC ISO-8601 second-precision string -- same口径 as memory/store.py."""
    return datetime.utcnow().isoformat(timespec="seconds")


def game_conversation_id(game_id: str, playthrough_id: str = "default") -> str:
    """The galgame-namespaced conversation_id (§15.3 / §25.1.2)."""
    return f"galgame::{game_id}::playthrough::{playthrough_id}"


def _field_names(cls: type) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


@dataclass
class _Model:
    """Mixin: enum-safe ``to_dict`` + forward-compatible ``from_dict``.

    ``from_dict`` ignores unknown keys so a v2 column never breaks a v1 read.
    Subclasses that hold an enum field override ``from_dict`` to re-hydrate it.
    """

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        return {key: (value.value if isinstance(value, Enum) else value) for key, value in data.items()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "_Model":
        names = _field_names(cls)
        return cls(**{key: value for key, value in data.items() if key in names})


# -- StoryLine status (§9.7) --------------------------------------------------

class StoryLineStatus(str, Enum):
    PENDING_CURRENT = "pending_current"
    COMMITTED = "committed"
    DISCARDED = "discarded"


class StoryLineStatusError(ValueError):
    """Raised when an illegal StoryLine status transition is attempted."""


# Legal directed edges, pinned (Phase 1). Anything else -- including reverse and
# same->same -- is illegal and raises (no silent no-op).
_ALLOWED_STATUS_TRANSITIONS: frozenset[tuple[StoryLineStatus, StoryLineStatus]] = frozenset(
    {
        (StoryLineStatus.PENDING_CURRENT, StoryLineStatus.COMMITTED),
        (StoryLineStatus.PENDING_CURRENT, StoryLineStatus.DISCARDED),
        (StoryLineStatus.COMMITTED, StoryLineStatus.DISCARDED),
    }
)


# -- §9.2 LaunchProfile -------------------------------------------------------

@dataclass
class LaunchProfile(_Model):
    platform: str = "linux"  # linux | windows
    launch_type: str = "desktop_entry"  # desktop_entry | command | exe | manual_bind
    launch_target: str | None = None
    command: str | None = None
    working_dir: str | None = None
    enabled: bool = True


# -- §9.3 WindowMatchRule -----------------------------------------------------

@dataclass
class WindowMatchRule(_Model):
    platform: str = "linux"  # linux | windows
    title_keywords: list[str] = field(default_factory=list)
    last_full_title: str | None = None
    process_name: str | None = None
    app_id: str | None = None
    confirmed_once: bool = False


# -- §9.5 OCRRegion -----------------------------------------------------------

@dataclass
class OCRRegion(_Model):
    x_ratio: float = 0.0
    y_ratio: float = 0.0
    w_ratio: float = 0.0
    h_ratio: float = 0.0
    pixel_rect: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    window_size_at_calibration: list[int] = field(default_factory=lambda: [0, 0])
    last_verified_at: str | None = None


# -- §9.4 OCRProfile ----------------------------------------------------------

@dataclass
class OCRProfile(_Model):
    languages: list[str] = field(default_factory=lambda: ["ja", "zh"])
    dialog_text_region: dict[str, Any] = field(default_factory=dict)
    speaker_name_region: dict[str, Any] | None = None
    speaker_strategy: str = "region"  # region | parse_from_text | narration_or_unknown
    stability_required_count: int = 2
    interval_seconds: float = 1.0
    similarity_threshold: float = 0.9
    raw_cache_retention_days: int = 7


# -- §9.1 GameProfile ---------------------------------------------------------

@dataclass
class GameProfile(_Model):
    game_id: str
    display_name: str
    created_at: str
    updated_at: str
    aliases: list[str] = field(default_factory=list)
    last_played_at: str | None = None
    active_playthrough_id: str = "default"
    launch_profiles: dict[str, Any] = field(default_factory=dict)
    window_match: dict[str, Any] = field(default_factory=dict)
    ocr_profile: dict[str, Any] = field(default_factory=dict)
    proactive_commentary: dict[str, Any] = field(default_factory=dict)


# -- §9.6 PlaySession (route_key materialized; v1 always None) -----------------

@dataclass
class PlaySession(_Model):
    session_id: str
    game_id: str
    started_at: str
    playthrough_id: str = "default"
    route_key: str | None = None  # v2 multi-route key; v1 恒 null
    ended_at: str | None = None
    state: str = "active"  # active | paused | ended | interrupted | crashed
    ocr_line_count: int = 0
    summary_count: int = 0


# -- §9.7 StoryLine -----------------------------------------------------------

@dataclass
class StoryLine(_Model):
    line_id: str
    session_id: str
    game_id: str
    text: str
    timestamp: str
    playthrough_id: str = "default"
    speaker: str | None = None
    source: str = "ocr"  # ocr | manual
    confidence: float = 0.0
    raw_hash: str = ""
    status: StoryLineStatus = StoryLineStatus.PENDING_CURRENT

    def with_status(self, new_status: StoryLineStatus | str) -> "StoryLine":
        """Return a copy with ``status`` advanced. Illegal transitions raise.

        Legal graph (pinned): pending_current -> committed, pending_current ->
        discarded, committed -> discarded. Reverse / same->same all raise.
        """
        target = StoryLineStatus(new_status)
        if (self.status, target) not in _ALLOWED_STATUS_TRANSITIONS:
            raise StoryLineStatusError(
                f"illegal StoryLine status transition: {self.status.value} -> {target.value}"
            )
        return dataclasses.replace(self, status=target)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StoryLine":
        names = _field_names(cls)
        payload = {key: value for key, value in data.items() if key in names}
        if payload.get("status") is not None:
            payload["status"] = StoryLineStatus(payload["status"])
        return cls(**payload)


# -- §9.8 StorySummary (route_key materialized; v1 always None) ----------------

@dataclass
class StorySummary(_Model):
    summary_id: str
    game_id: str
    playthrough_id: str = "default"
    route_key: str | None = None  # v2 multi-route key; v1 恒 null
    session_id: str = ""
    source_line_ids: list[str] = field(default_factory=list)
    summary_zh: str = ""
    key_original_lines: list[str] = field(default_factory=list)
    characters: list[str] = field(default_factory=list)
    major_events: list[str] = field(default_factory=list)
    unresolved_threads: list[str] = field(default_factory=list)
    route_guess: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    source: str = "auto_summary"  # auto_summary | user_correction | manual_note
    revision: int = 1


# -- §9.9 GameProgressState (route_key materialized; v1 always None) -----------

@dataclass
class GameProgressState(_Model):
    game_id: str
    playthrough_id: str = "default"
    route_key: str | None = None  # v2 multi-route key; v1 恒 null
    last_played_at: str = ""
    chapter: dict[str, Any] = field(default_factory=dict)  # {"title", "confidence"}
    route: dict[str, Any] = field(default_factory=dict)  # {"confirmed","name","confidence","evidence"}
    location: str | None = None
    current_scene_summary: str = ""
    major_events: list[str] = field(default_factory=list)
    unresolved_threads: list[str] = field(default_factory=list)
    last_ocr_anchor: dict[str, Any] = field(default_factory=dict)  # {"speaker","text","timestamp"}


# -- §9.10 CharacterRelation --------------------------------------------------

@dataclass
class CharacterRelation(_Model):
    relation_id: str
    game_id: str
    playthrough_id: str = "default"
    character_a: str = ""
    character_b: str = ""
    relation_summary: str = ""
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0
    updated_at: str = ""
    source: str = "auto_summary"  # auto_summary | user_correction


# -- §9.11 ChoiceEvent --------------------------------------------------------

@dataclass
class ChoiceEvent(_Model):
    choice_id: str
    game_id: str
    playthrough_id: str = "default"
    session_id: str = ""
    timestamp: str = ""
    options: list[dict[str, Any]] = field(default_factory=list)  # [{"index","text"}]
    selected_option_index: int | None = None
    selected_option_text: str | None = None
    selection_source: str | None = None  # user_reported | inferred | null
    confidence: float = 0.0
    screen_analysis_summary: str = ""


# -- §9.12 CompanionBeat ------------------------------------------------------

@dataclass
class CompanionBeat(_Model):
    beat_id: str
    game_id: str
    playthrough_id: str = "default"
    session_id: str | None = None
    type: str = "shared_observation"  # reaction|joke|user_preference|shared_observation|correction
    content: str = ""
    source: str = "user"  # user | spica | auto
    created_at: str = ""
    scope: dict[str, Any] = field(default_factory=dict)  # {"character_id","user_id","game_id"}


__all__ = [
    "utc_now_iso",
    "game_conversation_id",
    "StoryLineStatus",
    "StoryLineStatusError",
    "LaunchProfile",
    "WindowMatchRule",
    "OCRRegion",
    "OCRProfile",
    "GameProfile",
    "PlaySession",
    "StoryLine",
    "StorySummary",
    "GameProgressState",
    "CharacterRelation",
    "ChoiceEvent",
    "CompanionBeat",
]
