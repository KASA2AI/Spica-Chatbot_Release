from __future__ import annotations

from agent_tools.function_tools.song.intent import SongAction, SongContext, SongIntent, SongState
from agent_tools.function_tools.song.intent_rules import update_pending_song_hint_from_intent
from agent_tools.function_tools.song.intent_router import SongIntentRouter
from agent_tools.function_tools.song.trigger import build_song_request_from_intent


def _router() -> SongIntentRouter:
    return SongIntentRouter(
        {
            "intent": {
                "enabled": True,
                "thresholds": {
                    "direct_execute": 0.9,
                    "confirm": 0.7,
                    "llm_fallback_min": 0.45,
                    "llm_fallback_max": 0.75,
                },
                "llm_fallback": {"enabled": False},
            }
        }
    )


def test_high_confidence_sing_triggers() -> None:
    router = _router()

    intent = router.route("spica唱歌 青花瓷")
    assert intent.action == SongAction.SING
    assert intent.title == "青花瓷"

    intent = router.route("唱一下 周杰伦 的 稻香")
    assert intent.action == SongAction.SING
    assert intent.artist == "周杰伦"
    assert intent.title == "稻香"

    intent = router.route("播放《恋爱循环》")
    assert intent.action == SongAction.SING
    assert intent.title == "恋爱循环"

    intent = router.route("用你的声音唱 Lemon")
    assert intent.action == SongAction.SING
    assert intent.title == "Lemon"

    intent = router.route("我想听恋爱循环")
    assert intent.action == SongAction.SING
    assert intent.title == "恋爱循环"

    intent = router.route("我想听周杰伦的稻香")
    assert intent.action == SongAction.SING
    assert intent.artist == "周杰伦"
    assert intent.title == "稻香"

    intent = router.route("spica 我要听恋爱循环")
    assert intent.action == SongAction.SING
    assert intent.title == "恋爱循环"

    intent = router.route("spica,我加班累了，我想听sprail。")
    assert intent.action == SongAction.SING
    assert intent.title == "sprail"

    intent = router.route("我还想听風と行く道大原ゆい子")
    assert intent.action == SongAction.SING
    assert intent.title == "風と行く道大原ゆい子"


def test_negative_examples_are_rejected() -> None:
    router = _router()
    for text in (
        "你会唱歌吗？",
        "这首歌讲了什么？",
        "帮我写一首歌",
        "唱歌功能怎么用",
        "来点建议",
        "来点解释",
        "来点安慰",
        "播放视频",
        "播放录音",
        "播放下一段",
        "我想听你的建议",
        "我想听你讲讲这个问题",
    ):
        assert router.route(text).action == SongAction.REJECT


def test_search_requests_need_confirmation() -> None:
    router = _router()

    intent = router.route("唱一首 周杰伦的歌")
    assert intent.action == SongAction.SEARCH
    assert intent.needs_confirmation

    intent = router.route("唱一首周杰伦的歌")
    assert intent.action == SongAction.SEARCH
    assert intent.needs_confirmation

    intent = router.route("我想听周杰伦的歌")
    assert intent.action == SongAction.SEARCH
    assert intent.needs_confirmation

    intent = router.route("来点周杰伦")
    assert intent.action == SongAction.SEARCH
    assert intent.needs_confirmation

    intent = router.route("来点摇滚")
    assert intent.action == SongAction.SEARCH
    assert intent.needs_confirmation


def test_search_hint_extraction() -> None:
    router = _router()
    context = SongContext()

    intent = router.route("来点周杰伦")
    update_pending_song_hint_from_intent(context, intent)
    assert context.pending_song_raw_query == "周杰伦"
    assert context.pending_song_artist == "周杰伦"
    assert context.pending_song_style is None

    intent = router.route("来点摇滚")
    update_pending_song_hint_from_intent(context, intent)
    assert context.pending_song_raw_query == "摇滚"
    assert context.pending_song_artist is None
    assert context.pending_song_style == "摇滚"


def test_intent_confirming_followup_song_title_merges_pending_artist() -> None:
    router = _router()
    context = SongContext()

    first_intent = router.route("来点周杰伦")
    assert first_intent.action == SongAction.SEARCH
    context.state = SongState.INTENT_CONFIRMING
    update_pending_song_hint_from_intent(context, first_intent)
    assert context.pending_song_artist == "周杰伦"

    for text in ("稻香", "我想听稻香", "来一首稻香", "请唱稻香"):
        intent = router.route(text, SongState.INTENT_CONFIRMING, context)
        assert intent.action == SongAction.SING
        assert intent.artist == "周杰伦"
        assert intent.title == "稻香"
        assert "稻香" in (intent.query or "")
        assert "周杰伦" in (intent.query or "")


def test_intent_confirming_explicit_artist_overrides_pending_artist() -> None:
    router = _router()
    context = SongContext(
        state=SongState.INTENT_CONFIRMING,
        pending_song_raw_query="周杰伦",
        pending_song_artist="周杰伦",
    )

    intent = router.route("陈奕迅的十年", SongState.INTENT_CONFIRMING, context)
    assert intent.action == SongAction.SING
    assert intent.artist == "陈奕迅"
    assert intent.title == "十年"
    assert "周杰伦" not in (intent.query or "")


def test_intent_confirming_followup_style_stays_out_of_artist() -> None:
    router = _router()
    context = SongContext()

    first_intent = router.route("来点摇滚")
    assert first_intent.action == SongAction.SEARCH
    context.state = SongState.INTENT_CONFIRMING
    update_pending_song_hint_from_intent(context, first_intent)

    intent = router.route("稻香", SongState.INTENT_CONFIRMING, context)
    assert intent.action == SongAction.SING
    assert intent.title == "稻香"
    assert intent.artist is None
    assert "稻香" in (intent.query or "")
    assert "摇滚" in (intent.query or "")


def test_intent_confirming_cancel_and_chat_clear_pending_hint() -> None:
    router = _router()
    for text in ("算了", "不听了"):
        context = SongContext(
            state=SongState.INTENT_CONFIRMING,
            pending_song_raw_query="周杰伦",
            pending_song_artist="周杰伦",
        )
        intent = router.route(text, SongState.INTENT_CONFIRMING, context)
        assert intent.action == SongAction.CANCEL
        assert context.pending_song_raw_query is None
        assert context.pending_song_artist is None
        assert context.pending_song_style is None
        assert context.state == SongState.IDLE

    context = SongContext(
        state=SongState.INTENT_CONFIRMING,
        pending_song_raw_query="周杰伦",
        pending_song_artist="周杰伦",
    )
    intent = router.route("你刚刚说到哪了", SongState.INTENT_CONFIRMING, context)
    assert intent.action in {SongAction.NONE, SongAction.REJECT}
    assert intent.action != SongAction.SING
    assert context.pending_song_raw_query is None
    assert context.pending_song_artist is None
    assert context.pending_song_style is None
    assert context.state == SongState.IDLE


def test_control_intents_are_state_sensitive() -> None:
    router = _router()
    assert router.route("暂停一下", SongState.PLAYING).action == SongAction.PAUSE
    assert router.route("暂停一下", SongState.PREPARING).action == SongAction.PAUSE
    assert router.route("继续", SongState.PAUSED).action == SongAction.RESUME
    assert router.route("别唱了", SongState.PREPARING).action == SongAction.CANCEL
    assert router.route("继续", SongState.IDLE).action == SongAction.NONE
    assert router.route("暂停一下", SongState.IDLE).action == SongAction.NONE


def test_followup_selection_requires_candidate_state() -> None:
    router = _router()
    intent = router.route("第二个", SongState.CANDIDATE_SELECTING)
    assert intent.action == SongAction.CONFIRM
    assert intent.candidate_index == 1

    assert router.route("第二个", SongState.IDLE).action == SongAction.NONE


def test_change_song_intent() -> None:
    router = _router()
    intent = router.route("换成青花瓷", SongState.PLAYING)
    assert intent.action == SongAction.CHANGE
    assert "青花瓷" in (intent.query or intent.title or "")

    intent = router.route("换一首", SongState.PLAYING)
    assert intent.action == SongAction.CHANGE
    assert intent.needs_confirmation or intent.query is None


def test_router_without_llm_key_does_not_crash(monkeypatch) -> None:
    monkeypatch.delenv("SONG_INTENT_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    router = SongIntentRouter(
        {
            "intent": {
                "enabled": True,
                "thresholds": {
                    "direct_execute": 0.9,
                    "confirm": 0.7,
                    "llm_fallback_min": 0.45,
                    "llm_fallback_max": 0.75,
                },
                "llm_fallback": {
                    "enabled": True,
                    "api_key_env": "SONG_INTENT_OPENAI_API_KEY",
                    "base_url_env": "SONG_INTENT_OPENAI_BASE_URL",
                },
            }
        }
    )

    assert router.route("spica唱歌 青花瓷").action == SongAction.SING


def test_build_song_request_from_intent() -> None:
    request = build_song_request_from_intent(
        SongIntent(
            action=SongAction.SING,
            confidence=0.95,
            query="青花瓷",
            title="青花瓷",
            original_text="spica唱歌 青花瓷",
        )
    )
    assert request is not None
    assert request.query == "青花瓷"
    assert request.title == "青花瓷"

    for action in (SongAction.SEARCH, SongAction.REJECT, SongAction.NONE):
        assert build_song_request_from_intent(SongIntent(action=action, confidence=0.9)) is None

    assert build_song_request_from_intent(SongIntent(action=SongAction.SING, confidence=0.9)) is None
