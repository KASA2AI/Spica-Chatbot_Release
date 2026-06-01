from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from agent_tools.function_tools.song.models import SongRequest


class SongAction(str, Enum):
    NONE = "none"
    SING = "sing"
    SEARCH = "search"
    CONFIRM = "confirm"
    CANCEL = "cancel"
    PAUSE = "pause"
    RESUME = "resume"
    RESTART = "restart"
    CHANGE = "change"
    HELP = "help"
    REJECT = "reject"


class SongState(str, Enum):
    IDLE = "idle"
    INTENT_CONFIRMING = "intent_confirming"
    CANDIDATE_SELECTING = "candidate_selecting"
    PREPARING = "preparing"
    READY = "ready"
    PLAYING = "playing"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    ERROR = "error"


@dataclass
class SongIntent:
    action: SongAction
    confidence: float
    query: str | None = None
    title: str | None = None
    artist: str | None = None
    candidate_index: int | None = None
    reason: str = ""
    needs_confirmation: bool = False
    source: str = "rule"
    original_text: str = ""


@dataclass
class SongContext:
    state: SongState = SongState.IDLE
    pending_request: SongRequest | None = None
    pending_audio_path: str | None = None
    last_request: SongRequest | None = None
    last_audio_path: str | None = None
    auto_play: bool = True
