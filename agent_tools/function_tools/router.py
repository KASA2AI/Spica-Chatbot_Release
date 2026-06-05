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
    "browser",
    "webpage",
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
_TIME_TERMS = ("几点", "现在时间", "当前时间", "今天几号", "日期", "星期几", "time", "date")
_WEATHER_TERMS = ("天气", "气温", "下雨", "下雪", "空气质量", "weather", "temperature")
_CALCULATOR_TERMS = ("计算", "算一下", "等于多少", "平方", "开方", "calculator")
_LEGACY_TOOL_NAMES = {
    "time": {"get_current_time", "current_time", "time", "lookup_time"},
    "weather": {"get_weather", "weather", "lookup_weather"},
    "calculator": {"calculator", "calculate", "run_calculator", "math_calculator"},
}


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
        return tool_functions[name](**parsed_args)
    except TypeError as exc:
        return tool_error("TOOL_ARGUMENTS_MISMATCH", f"工具参数不匹配：{exc}")
    except Exception as exc:
        return tool_error("TOOL_EXECUTION_ERROR", f"工具执行失败：{exc}")


def should_use_tools(user_text: str) -> bool:
    return bool(_tool_names_for_text(user_text))


def tool_schemas_for_user_text(user_text: str, schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    wanted_names = _tool_names_for_text(user_text)
    if not wanted_names:
        return []
    return [schema for schema in schemas if _schema_name(schema) in wanted_names]


def is_screen_intent_explicit(user_text: str) -> bool:
    text = _normalize_text(user_text)
    if not text:
        return False
    has_target = any(term in text for term in _SCREEN_TARGET_TERMS)
    has_action = any(term in text for term in _SCREEN_ACTION_TERMS)
    return has_target and has_action


def _tool_names_for_text(user_text: str) -> set[str]:
    text = _normalize_text(user_text)
    names: set[str] = set()
    if is_screen_intent_explicit(text):
        names.add("inspect_screen")
    if _is_time_intent(text):
        names.update(_LEGACY_TOOL_NAMES["time"])
    if _is_weather_intent(text):
        names.update(_LEGACY_TOOL_NAMES["weather"])
    if _is_calculator_intent(text):
        names.update(_LEGACY_TOOL_NAMES["calculator"])
    return names


def _is_time_intent(text: str) -> bool:
    return any(term in text for term in _TIME_TERMS)


def _is_weather_intent(text: str) -> bool:
    return any(term in text for term in _WEATHER_TERMS)


def _is_calculator_intent(text: str) -> bool:
    if any(term in text for term in _CALCULATOR_TERMS):
        return True
    return bool(re.search(r"\d+(?:\.\d+)?\s*[+\-*/×÷]\s*\d+", text))


def _schema_name(schema: dict[str, Any]) -> str:
    name = schema.get("name")
    if isinstance(name, str):
        return name
    function = schema.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    return ""


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip().lower())
