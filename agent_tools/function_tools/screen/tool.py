from __future__ import annotations

from time import perf_counter
from typing import Any

from agent_tools.function_tools.screen.analyzer import analyze_screen_image_local
from agent_tools.function_tools.screen.capture import capture_full_screen
from agent_tools.function_tools.screen.config import load_screen_config
from agent_tools.function_tools.screen.schema import ScreenToolError


INSPECT_SCREEN_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "inspect_screen",
    "strict": True,
    "description": (
        "只有用户明确要求查看屏幕、桌面、显示器、当前画面、浏览器画面、任务栏、"
        "主屏幕报错或类似可见画面内容时才使用。只观察一次，不要后台持续截图。"
        "不要点击、输入或控制电脑。不要用于窗口捕获、区域选择、鼠标键盘控制或实时监控。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "enum": ["full_screen"],
                "description": "第一阶段只支持主显示器全屏截图。",
            },
            "question": {
                "type": "string",
                "description": "用户关于当前屏幕/桌面/显示器/画面的原始问题。",
            },
        },
        "required": ["target", "question"],
        "additionalProperties": False,
    },
}


def inspect_screen(target: str = "full_screen", question: str = "") -> str:
    from agent_tools.function_tools.router import is_screen_intent_explicit, tool_error, tool_success

    target = (target or "full_screen").strip()
    question = (question or "").strip()
    if target != "full_screen":
        return tool_error("SCREEN_INTENT_NOT_EXPLICIT", "第一阶段只支持 target=full_screen。")
    if not is_screen_intent_explicit(question):
        return tool_error(
            "SCREEN_INTENT_NOT_EXPLICIT",
            "inspect_screen 只能在用户明确要求查看屏幕、桌面、显示器或当前画面时调用。",
        )

    try:
        config = load_screen_config()
        started = perf_counter()
        capture = capture_full_screen()
        capture_ms = round((perf_counter() - started) * 1000.0, 3)
        capture_metadata = _capture_metadata_for_observation(capture.image, capture.metadata, config.capture_format)
        observation = analyze_screen_image_local(
            capture.image,
            target,
            question,
            config=config,
            capture=capture_metadata,
            performance={"capture_ms": capture_ms},
            question_type=classify_screen_question(question),
        )
        return tool_success(observation)
    except ScreenToolError as exc:
        return tool_error(exc.code, exc.message)
    except Exception as exc:
        return tool_error("SCREEN_ANALYSIS_FAILED", f"本地屏幕分析失败：{exc}")


def classify_screen_question(question: str) -> str:
    text = (question or "").lower()
    if any(token in text for token in ("几个", "多少个", "有多少", "count", "how many")):
        return "counting"
    if any(token in text for token in ("报错", "错误", "异常", "error", "warning", "警告")):
        return "diagnosis"
    if any(token in text for token in ("出自", "哪个动漫", "是什么", "是谁", "网站", "识别", "identify")):
        return "identification"
    if any(token in text for token in ("在干嘛", "正在", "打开", "浏览", "doing")):
        return "activity"
    return "general_observation"


def _capture_metadata_for_observation(image: Any, capture_metadata: dict[str, Any], image_format: str) -> dict[str, Any]:
    metadata = dict(capture_metadata or {})
    metadata.update(
        {
            "mode": metadata.get("captured_scope") or "full_screen",
            "width": int(getattr(image, "width", 0) or 0),
            "height": int(getattr(image, "height", 0) or 0),
            "image_format": image_format,
            "mime_type": f"image/{image_format}",
            "image": {
                "original_resolution": {
                    "width": int(getattr(image, "width", 0) or 0),
                    "height": int(getattr(image, "height", 0) or 0),
                },
                "sent_resolution": {
                    "width": int(getattr(image, "width", 0) or 0),
                    "height": int(getattr(image, "height", 0) or 0),
                },
                "downscaled": False,
                "format": image_format,
            },
        }
    )
    return metadata
