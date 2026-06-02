from __future__ import annotations

from dataclasses import dataclass, replace
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
    pending_song_raw_query: str | None = None
    pending_song_artist: str | None = None
    pending_song_style: str | None = None
    last_request: SongRequest | None = None
    last_audio_path: str | None = None
    auto_play: bool = True


def clear_pending_song_hint(context: SongContext | None) -> None:
    if context is None:
        return
    context.pending_song_raw_query = None
    context.pending_song_artist = None
    context.pending_song_style = None


def merge_pending_song_hint(intent: SongIntent, context: SongContext | None) -> SongIntent:
    if intent.action != SongAction.SING or context is None:
        return intent

    pending_artist = _clean_hint(context.pending_song_artist)
    pending_style = _clean_hint(context.pending_song_style)
    title = _clean_hint(intent.title)
    artist = _clean_hint(intent.artist) or pending_artist
    query = _clean_hint(intent.query)

    if title and artist:
        query = f"{title} {artist}"
    elif title:
        query = title
    elif query and artist and artist not in query:
        query = f"{query} {artist}"

    if pending_style and not artist:
        if query and pending_style not in query:
            query = f"{query} {pending_style}"
        elif not query and title:
            query = f"{title} {pending_style}"

    return replace(intent, query=query, title=title, artist=artist)


def _clean_hint(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None
