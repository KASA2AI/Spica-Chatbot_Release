from __future__ import annotations

from agent_tools.function_tools.song.intent import SongAction, SongContext, SongState
from agent_tools.function_tools.song.intent_rules import update_pending_song_hint_from_intent
from agent_tools.function_tools.song.trigger import build_song_request_from_intent, parse_song_intent


def test_parse_song_intent_explicit_song() -> None:
    intent = parse_song_intent("spica唱歌 青花瓷")
    assert intent.action == SongAction.SING
    assert intent.title == "青花瓷"


def test_parse_song_intent_followup_merges_pending_hint() -> None:
    context = SongContext()
    first_intent = parse_song_intent("来点周杰伦", context=context)
    assert first_intent.action == SongAction.SEARCH

    context.state = SongState.INTENT_CONFIRMING
    update_pending_song_hint_from_intent(context, first_intent)

    intent = parse_song_intent("我想听稻香", SongState.INTENT_CONFIRMING, context)
    assert intent.action == SongAction.SING
    assert intent.title == "稻香"
    assert intent.artist == "周杰伦"
    assert "稻香" in (intent.query or "")
    assert "周杰伦" in (intent.query or "")


def test_build_song_request_from_intent_uses_merged_query() -> None:
    context = SongContext(
        state=SongState.INTENT_CONFIRMING,
        pending_song_raw_query="周杰伦",
        pending_song_artist="周杰伦",
    )
    intent = parse_song_intent("稻香", SongState.INTENT_CONFIRMING, context)
    request = build_song_request_from_intent(intent)

    assert request is not None
    assert request.title == "稻香"
    assert request.artist == "周杰伦"
    assert request.query == "稻香 周杰伦"
