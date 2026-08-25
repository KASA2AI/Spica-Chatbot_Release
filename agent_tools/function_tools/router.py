from __future__ import annotations

import json
import re
from typing import Any, Callable


def tool_success(data: dict[str, Any]) -> str:
    return json.dumps({"ok": True, "data": data, "error": None}, ensure_ascii=False)


def tool_error(code: str, message: str) -> str:
    return json.dumps({"ok": False, "data": None, "error": {"code": code, "message": message}}, ensure_ascii=False)


from agent_tools.function_tools.screen import INSPECT_SCREEN_SCHEMA, inspect_screen  # noqa: E402


TOOL_SCHEMAS: list[dict[str, Any]] = [INSPECT_SCREEN_SCHEMA]

_SCREEN_TARGET_TERMS = (
    "屏幕",
    "显示器",
    "桌面",
    "画面",
    "截图",
    "当前窗口",
    "浏览器",
    "网站",
    "网页",
    "任务栏",
    "游戏画面",
    "主屏幕",
    "screen",
    "display",
    "desktop",
    "screenshot",
    "current window",
    "main screen",
    "game screen",
    "browser",
    "website",
    "webpage",
    "monitor",
    "taskbar",
)
_SCREEN_ACTION_TERMS = (
    "看",
    "看看",
    "看一下",
    "帮我看",
    "识别",
    "判断",
    "是什么",
    "有几个",
    "多少个",
    "出自哪里",
    "出自哪个",
    "在干嘛",
    "报错",
    "打开了几个",
    "正在浏览",
    "view",
    "look",
    "inspect",
    "identify",
    "what is",
    "how many",
    "error",
)


# watch_game_screen deliberately has NO wordlist here (trigger-layer refactor):
# its offer gate is the DETERMINISTIC companion-play state (the registry's
# ``available`` predicate, wired by the host), and "call or not" is the LLM's
# structured tool-call decision via the description. A wordlist guessing intent
# was both leaky (natural phrasings missed) and noisy.


def default_tool_functions() -> dict[str, Callable[..., str]]:
    return {"inspect_screen": inspect_screen}


def run_local_tool(tool_functions: dict[str, Callable[..., str]], name: str, arguments: str) -> str:
    if name not in tool_functions:
        return tool_error("UNKNOWN_TOOL", f"未知工具：{name}")
    try:
        parsed_args: dict[str, Any] = json.loads(arguments or "{}")
    except json.JSONDecodeError as exc:
        return tool_error("INVALID_TOOL_ARGUMENTS_JSON", f"工具参数不是合法 JSON：{exc}")
    try:
        if name == "inspect_screen":
            from agent_tools.function_tools.screen.analyzer import clear_last_screen_analysis_metadata

            clear_last_screen_analysis_metadata()
        return tool_functions[name](**parsed_args)
    except TypeError as exc:
        return tool_error("TOOL_ARGUMENTS_MISMATCH", f"工具参数不匹配：{exc}")
    except Exception as exc:
        return tool_error("TOOL_EXECUTION_ERROR", f"工具执行失败：{exc}")


def should_use_tools(user_text: str) -> bool:
    return bool(tool_schemas_for_user_text(user_text, TOOL_SCHEMAS))


def tool_schemas_for_user_text(user_text: str, schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    wanted_names = _tool_names_for_text(user_text)
    if not wanted_names:
        return []
    return [schema for schema in schemas if _schema_name(schema) in wanted_names]


def is_screen_intent_explicit(user_text: str) -> bool:
    text = _normalize_text(user_text)
    if not text:
        return False
    compact_text = re.sub(r"\s+", "", text)
    has_target = any(_contains_intent_term(text, compact_text, term) for term in _SCREEN_TARGET_TERMS)
    has_action = any(_contains_intent_term(text, compact_text, term) for term in _SCREEN_ACTION_TERMS)
    return has_target and has_action


# B2 (P2): sing_song SUPPLY pre-filter terms. Deliberately GENEROUS -- this list
# only gates whether the tool schema is offered to the probe (a false hit costs
# one probe round trip; a miss just means rephrasing); it never hijacks or
# swallows the message (the B1 lesson, applied as supply instead of verdict).
_SONG_SUPPLY_TERMS = (
    "唱", "歌", "曲", "音乐", "听", "来一首", "来首", "播放",
    "cover", "翻唱", "sing", "song", "music",
)


def is_song_intent_possible(user_text: str) -> bool:
    text = _normalize_text(user_text)
    if not text:
        return False
    return any(term in text for term in _SONG_SUPPLY_TERMS)


# watch_anime is an expensive act tool: a false offer can make the model perform
# a source lookup and then start a second LLM round.  Keep its supply gate much
# stricter than ordinary semantic routing.  Incomplete requests such as
# "我想看动漫" stay in normal dialogue so Spica can ask for a title/episode;
# the tool is only needed once the request is actionable.
_ANIME_NUMBER = r"(?:\d+|[零〇一二两三四五六七八九十百千]+)"
_ANIME_CJK_EPISODE_RE = re.compile(
    rf"(?:第?{_ANIME_NUMBER}(?:集|话|話)|最新(?:一)?(?:集|话|話))"
)
_ANIME_EN_EPISODE_RE = re.compile(r"\b(?:episode|ep\.?)[\s:#-]*\d+\b")
_ANIME_EPISODE_ONLY_RE = re.compile(
    rf"^(?:第?{_ANIME_NUMBER}季)?"
    rf"(?:第?{_ANIME_NUMBER}(?:集|话|話)|最新(?:一)?(?:集|话|話))$"
)
_ANIME_TITLE_EPISODE_ONLY_RE = re.compile(
    rf"^(?:《[^》]{{1,40}}》|[a-z0-9\u3040-\u30ff\u3400-\u9fff·・—_-]{{2,48}})"
    rf"(?:第?{_ANIME_NUMBER}季)?"
    rf"(?:第?{_ANIME_NUMBER}(?:集|话|話)|最新(?:一)?(?:集|话|話))$"
)
_ANIME_EN_TITLE_EPISODE_ONLY_RE = re.compile(
    r"^[a-z0-9][a-z0-9 ._'-]{1,48}\s+(?:episode|ep\.?)\s*\d+$"
)
_ANIME_REQUEST_ACTION_TERMS = (
    "想看", "要看", "一起看", "陪我看", "补番", "想追", "要追", "追番",
    "想播放", "要播放",
    "watch", "play", "見たい", "観たい", "見よう", "観よう", "再生",
)
_ANIME_NON_REQUEST_TERMS = (
    "不想看", "不要看", "别看", "不看了", "不用看", "看完", "看过", "刚看",
    "不要下", "别下", "不下了", "取消下载", "停止下载",
    "已经看", "正在看", "好看吗", "好不好看", "怎么样", "评价", "聊聊", "讨论",
    "剧情", "讲什么", "说什么", "翻译", "这句话", "台词", "假设", "如果", "推荐",
    "为什么", "喜欢", "最爱", "讨厌", "看起来", "看上去", "值得看", "值得追",
    "什么时候播", "何时播", "播放时间", "播出时间", "放送时间",
    "正在播放", "无法播放", "不能播放", "播放不了", "播不了", "放不了",
    "見ない", "見終わ", "見ている", "再生しない",
    "don't watch", "do not watch", "watched", "review", "translate",
)
_ANIME_RECENT_DOWNLOAD_TERMS = (
    "刚下好", "刚下载好", "下载好的", "下完的", "刚才下的", "刚刚下的",
)
_ANIME_RECENT_PLAY_COMMANDS = {
    "放吧", "播吧", "播放吧", "现在看", "现在看吧", "开始看", "开始看吧",
}
_ANIME_JA_PAST_RE = re.compile(r"(?:見|観)た(?!い)")
_ANIME_CJK_ACTION_RE = re.compile(
    r"^(?:(?:spica|丝碧卡)[，,:：]?)?"
    r"(?:请|麻烦你?|帮我|给我|我想|我要|想|要|可以(?:帮我)?|"
    r"能不能(?:帮我)?|我们|咱们|现在|开始|继续|再|来|一起|陪我)?"
    r"(?:播放|播(?:一下)?|放(?:一下)?|看(?:一下|看)?)"
)
_ANIME_OBJECT_ACTION_RE = re.compile(
    r"^把.+(?:播放|播|放)(?:一下|了|吧|出来)?$"
)

_ANIME_CANCEL_ACTION_TERMS = (
    "取消", "停止", "停下", "别下", "不要下", "不下了", "cancel", "stop",
)
_ANIME_CANCEL_TARGET_TERMS = (
    "下载", "动漫", "动画", "番", "那集", "这一集", "任务", "download", "anime",
)
_ANIME_CANCEL_SHORT_COMMANDS = {
    "取消吧", "停下吧", "别下了", "不要下了", "不下了", "停止下载", "取消下载",
}


def _command_text(text: str) -> str:
    compact = re.sub(r"\s+", "", text)
    return re.sub(r"[。！!？?～~，,、…]+$", "", compact)


def is_watch_anime_intent_explicit(user_text: str) -> bool:
    """Conservatively decide whether ``watch_anime`` may be offered.

    This is a supply decision, not the final semantic decision.  False negatives
    merely make Spica ask a clarifying question; false positives can perform an
    expensive lookup and force a second model round, so ambiguity fails closed.
    """

    text = _normalize_text(user_text)
    if not text:
        return False
    command = _command_text(text)
    if (
        any(term in text for term in _ANIME_NON_REQUEST_TERMS)
        or _ANIME_JA_PAST_RE.search(text)
    ):
        return False
    if command in _ANIME_RECENT_PLAY_COMMANDS:
        return True

    has_recent_download = any(
        term in command for term in _ANIME_RECENT_DOWNLOAD_TERMS
    )
    has_play_action = any(
        term in text for term in ("播放", "播", "放", "看", "play", "watch", "再生")
    )
    if has_recent_download and (has_play_action or "那集" in command):
        return True

    has_episode = bool(
        _ANIME_CJK_EPISODE_RE.search(command)
        or _ANIME_EN_EPISODE_RE.search(text)
    )
    if not has_episode:
        return False
    if _ANIME_EPISODE_ONLY_RE.fullmatch(command):
        return True
    if _ANIME_TITLE_EPISODE_ONLY_RE.fullmatch(command):
        return True
    if _ANIME_EN_TITLE_EPISODE_ONLY_RE.fullmatch(text):
        return True
    return (
        any(term in text for term in _ANIME_REQUEST_ACTION_TERMS)
        or bool(_ANIME_CJK_ACTION_RE.search(command))
        or bool(_ANIME_OBJECT_ACTION_RE.search(command))
    )


def is_cancel_anime_download_intent_explicit(user_text: str) -> bool:
    """Only offer the active-download cancel tool for an explicit stop request."""

    text = _normalize_text(user_text)
    if not text:
        return False
    command = _command_text(text)
    if command in _ANIME_CANCEL_SHORT_COMMANDS:
        return True
    return (
        any(term in text for term in _ANIME_CANCEL_ACTION_TERMS)
        and (
            any(term in text for term in _ANIME_CANCEL_TARGET_TERMS)
            or _ANIME_CJK_EPISODE_RE.search(command)
            or _ANIME_EN_EPISODE_RE.search(text)
        )
    )


def _tool_names_for_text(user_text: str) -> set[str]:
    names: set[str] = set()
    watch_anime = is_watch_anime_intent_explicit(user_text)
    if is_screen_intent_explicit(user_text):
        names.add("inspect_screen")
    # "播放" is shared vocabulary.  A concrete episode request is stronger
    # evidence than the song router's deliberately generous media term, so do
    # not make the model choose between two act tools for the same request.
    if is_song_intent_possible(user_text) and not watch_anime:
        names.add("sing_song")
    if watch_anime:
        names.add("watch_anime")
    if is_cancel_anime_download_intent_explicit(user_text):
        names.add("cancel_anime_download")
    return names


def _schema_name(schema: dict[str, Any]) -> str:
    name = schema.get("name")
    if isinstance(name, str):
        return name
    function = schema.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    return ""


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _contains_intent_term(text: str, compact_text: str, term: str) -> bool:
    normalized_term = _normalize_text(term)
    if not normalized_term:
        return False
    if normalized_term in text:
        return True
    compact_term = re.sub(r"\s+", "", normalized_term)
    return compact_term in compact_text
