from __future__ import annotations

from agent_tools.function_tools.song.intent import SongAction, SongIntent, SongState
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
    for text in ("你会唱歌吗？", "这首歌讲了什么？", "帮我写一首歌", "唱歌功能怎么用"):
        assert router.route(text).action == SongAction.REJECT


def test_search_requests_need_confirmation() -> None:
    router = _router()

    intent = router.route("唱一首 周杰伦的歌")
    assert intent.action == SongAction.SEARCH
    assert intent.needs_confirmation

    intent = router.route("我想听周杰伦的歌")
    assert intent.action == SongAction.SEARCH
    assert intent.needs_confirmation

    intent = router.route("来点摇滚")
    assert intent.action == SongAction.SEARCH
    assert intent.needs_confirmation


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
