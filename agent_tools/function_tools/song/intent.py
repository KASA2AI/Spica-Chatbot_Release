"""Song flow data model -- the PLAYBACK lifecycle only (post-B2).

B2 deleted the intent-routing stack; ``SongState`` now models the playback
lifecycle (the confirmation states INTENT_CONFIRMING / CANDIDATE_SELECTING died
with the pre-chat hijack -- "which song?" is a normal conversation now), and
``SongIntent`` only carries what the control fast path emits.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from agent_tools.function_tools.song.models import SongRequest


class SongAction(str, Enum):
    NONE = "none"
    SING = "sing"
    CANCEL = "cancel"
    PAUSE = "pause"
    RESUME = "resume"
    RESTART = "restart"


class SongState(str, Enum):
    IDLE = "idle"
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
    reason: str = ""
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
