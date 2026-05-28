from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Callable


TOOL_SCHEMAS = [
    {
        "type": "function",
        "name": "get_time",
        "description": "查询某个城市或位置的当前参考时间。",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "城市或位置名称，例如：上海、北京、本地。",
                }
            },
            "required": ["city"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_weather",
        "description": "查询城市天气。当前是学习用模拟数据。",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "城市名，例如：上海、北京、深圳。",
                }
            },
            "required": ["city"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "calculator",
        "description": "计算简单四则运算表达式。",
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "数学表达式，例如：2*(3+4)。",
                }
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


def should_use_tools(user_text: str) -> bool:
    text = (user_text or "").lower()
    keywords = (
        "时间",
        "现在几点",
        "几点了",
        "何時",
        "weather",
        "天气",
        "天気",
        "计算",
        "计算器",
    )
    if any(keyword in text for keyword in keywords):
        return True
    return bool(re.search(r"\d+\s*[\+\-\*/%]\s*\d+", text))


def tool_success(data: dict[str, Any]) -> str:
    return json.dumps({"ok": True, "data": data, "error": None}, ensure_ascii=False)


def tool_error(code: str, message: str) -> str:
    return json.dumps({"ok": False, "data": None, "error": {"code": code, "message": message}}, ensure_ascii=False)


def get_time(city: str = "本地") -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return tool_success({"city": city, "time": now, "timezone": "local", "source": "system_clock"})


def get_weather(city: str) -> str:
    fake_weather = {
        "北京": {"condition": "晴", "temperature": "18-29°C", "wind": "北风 2 级"},
        "上海": {"condition": "多云", "temperature": "20-27°C", "wind": "东南风 3 级"},
        "深圳": {"condition": "阵雨", "temperature": "24-30°C", "wind": "南风 2 级"},
        "杭州": {"condition": "小雨", "temperature": "19-25°C", "wind": "东北风 2 级"},
    }
    weather = fake_weather.get(city)
    if weather is None:
        return tool_error("WEATHER_CITY_NOT_FOUND", f"{city}天气数据暂未接入，当前示例只返回模拟结果。")
    return tool_success({"city": city, "weather": weather, "source": "mock_data"})


def calculator(expression: str) -> str:
    allowed_chars = set("0123456789+-*/(). %")
    if not expression or any(ch not in allowed_chars for ch in expression):
        return tool_error("INVALID_EXPRESSION", "表达式不合法，只支持数字和 + - * / ( ) %。")
    try:
        result = eval(expression, {"__builtins__": {}}, {})
    except Exception as exc:
        return tool_error("CALCULATION_FAILED", f"计算失败：{exc}")
    return tool_success({"expression": expression, "result": result})


def default_tool_functions() -> dict[str, Callable[..., str]]:
    return {
        "get_time": get_time,
        "get_weather": get_weather,
        "calculator": calculator,
    }


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
