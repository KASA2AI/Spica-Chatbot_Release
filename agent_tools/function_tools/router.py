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


def _tool_names_for_text(user_text: str) -> set[str]:
    names: set[str] = set()
    if is_screen_intent_explicit(user_text):
        names.add("inspect_screen")
    if is_song_intent_possible(user_text):
        names.add("sing_song")
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
