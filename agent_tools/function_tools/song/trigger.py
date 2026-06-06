from __future__ import annotations

from agent_tools.function_tools.song.intent import SongAction, SongContext, SongIntent, SongState
from agent_tools.function_tools.song.intent_router import SongIntentRouter
from agent_tools.function_tools.song.models import SongRequest


def parse_song_intent(
    user_text: str,
    state: SongState = SongState.IDLE,
    context: SongContext | None = None,
) -> SongIntent:
    return SongIntentRouter().route(user_text, state, context)


def build_song_request_from_intent(intent: SongIntent) -> SongRequest | None:
    if intent.action != SongAction.SING:
        return None
    song_query = (intent.query or intent.title or "").strip()
    if not song_query:
        return None
    return SongRequest(
        query=song_query,
        title=(intent.title or None),
        artist=(intent.artist or None),
        user_text=intent.original_text,
    )
