from __future__ import annotations

import base64
import json
from typing import Any

from agent_tools.function_tools.screen.config import ScreenVisionConfig, load_screen_vision_config
from agent_tools.function_tools.screen.schema import ScreenToolError, normalize_screen_observation


_SYSTEM_PROMPT = """
You are Spica's one-shot screen observation module.
Return only strict JSON. Do not use Markdown.
Analyze the screenshot only to answer the user's question.
Do not perform full-document OCR. Extract only important visible text relevant to the question.
If you notice passwords, tokens, verification codes, private chats, or similarly sensitive content, summarize the type of content without transcribing it exactly.
Do not claim access to windows, mouse, keyboard, history, files, or live monitoring. This is a single screenshot, which may be full-screen or a manually selected region.
""".strip()


def analyze_screen_image(
    *,
    jpeg_bytes: bytes,
    config: ScreenVisionConfig,
    user_question: str,
    question_type: str,
    target: str,
    capture: dict[str, Any],
) -> dict[str, Any]:
    if not config.api_key:
        raise ScreenToolError(
            "SCREEN_API_NOT_CONFIGURED",
            f"未配置 {config.api_key_env}，无法调用独立 screen vision API。",
        )

    try:
        from openai import OpenAI  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ScreenToolError("SCREEN_ANALYSIS_FAILED", "缺少 openai 客户端依赖，请安装 openai。") from exc

    data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes).decode("ascii")
    client = OpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=config.request_timeout_seconds,
    )
    request = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _build_user_prompt(user_question, question_type, target, capture)},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": data_url,
                            "detail": config.image_detail,
                        },
                    },
                ],
            },
        ],
        "temperature": 0,
    }

    try:
        response = _create_chat_completion(client, request)
        content = _extract_message_content(response)
        parsed = _parse_json_object(content)
        if isinstance(parsed.get("data"), dict):
            parsed = parsed["data"]
        return normalize_screen_observation(
            parsed,
            user_question=user_question,
            question_type=question_type,
            target=target,
            capture=capture,
        )
    except ScreenToolError:
        raise
    except Exception as exc:
        raise ScreenToolError("SCREEN_ANALYSIS_FAILED", f"屏幕视觉分析失败：{exc}") from exc


def analyze_screen_attachment(*, attachment: dict[str, Any], user_question: str) -> dict[str, Any]:
    config = load_screen_vision_config()
    image_bytes = attachment.get("image_bytes")
    if not isinstance(image_bytes, (bytes, bytearray)):
        raise ScreenToolError("SCREEN_ANALYSIS_FAILED", "pending screenshot 缺少 JPEG 图片数据。")

    target = str(attachment.get("target") or "selected_region")
    source = str(attachment.get("source") or "manual_region_selection")
    capture = {
        "captured_scope": target,
        "source": source,
        "window": None,
        "region": attachment.get("region") if isinstance(attachment.get("region"), dict) else None,
        "image": {
            "original_resolution": attachment.get("original_resolution"),
            "sent_resolution": attachment.get("sent_resolution"),
            "downscaled": bool(attachment.get("downscaled", False)),
            "format": str(attachment.get("format") or "jpeg"),
            "quality": attachment.get("quality"),
        },
        "captured_at": attachment.get("captured_at"),
        "mime_type": attachment.get("mime_type") or "image/jpeg",
    }
    return analyze_screen_image(
        jpeg_bytes=bytes(image_bytes),
        config=config,
        user_question=user_question,
        question_type=_classify_screen_question(user_question),
        target=target,
        capture=capture,
    )


def _create_chat_completion(client: Any, request: dict[str, Any]) -> Any:
    try:
        return client.chat.completions.create(**request, response_format={"type": "json_object"})
    except TypeError:
        return client.chat.completions.create(**request)
    except Exception as exc:
        if "response_format" not in str(exc):
            raise
        return client.chat.completions.create(**request)


def _build_user_prompt(user_question: str, question_type: str, target: str, capture: dict[str, Any]) -> str:
    return f"""
User question: {user_question}
Question type: {question_type}
Target: {target}
Capture metadata: {json.dumps(capture, ensure_ascii=False)}

Output exactly one JSON object with this fixed structure. Keep all keys present. Use null where unknown.
The object must be the value of data, not a wrapper with ok/error.

{{
  "schema_version": "screen_observation.v1",
  "type": "screen_observation",
  "request": {{
    "user_question": "{_json_string(user_question)}",
    "question_type": "{_json_string(question_type)}",
    "target": "{_json_string(target)}"
  }},
  "capture": {{}},
  "answer": {{
    "direct_answer": "",
    "confidence": 0.0
  }},
  "scene": {{}},
  "visible_apps": [],
  "visible_text": {{}},
  "objects": [],
  "ui_elements": [],
  "counts": [],
  "identification": null,
  "diagnosis": null,
  "game": null,
  "spatial_hints": [],
  "ambiguity": [],
  "followup": {{
    "context_for_next_turn": "",
    "needs_followup_capture": false,
    "suggested_capture": null
  }},
  "privacy": {{}},
  "limitations": []
}}

Rules:
- answer.direct_answer must be a concise answer to the user question.
- followup.context_for_next_turn must summarize reusable context for the main chat model.
- capture.captured_scope and capture.source must match the provided capture metadata.
- Do not request or imply live monitoring, clicking, typing, region selection, or window capture.
- If uncertain, state uncertainty in ambiguity and lower confidence.
""".strip()


def _json_string(value: str) -> str:
    return json.dumps(value or "", ensure_ascii=False)[1:-1]


def _extract_message_content(response: Any) -> str:
    choices = list(getattr(response, "choices", []) or [])
    if not choices:
        raise ScreenToolError("SCREEN_ANALYSIS_FAILED", "视觉 API 没有返回 choices。")
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", "") if message is not None else ""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif hasattr(item, "text"):
                parts.append(str(getattr(item, "text") or ""))
        content = "".join(parts)
    content = str(content or "").strip()
    if not content:
        raise ScreenToolError("SCREEN_ANALYSIS_FAILED", "视觉 API 返回内容为空。")
    return content


def _parse_json_object(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            raise ScreenToolError("SCREEN_ANALYSIS_FAILED", "视觉 API 没有返回合法 JSON。")
        try:
            parsed = json.loads(content[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ScreenToolError("SCREEN_ANALYSIS_FAILED", f"视觉 API 返回 JSON 解析失败：{exc}") from exc
    if not isinstance(parsed, dict):
        raise ScreenToolError("SCREEN_ANALYSIS_FAILED", "视觉 API 返回的 JSON 顶层不是对象。")
    return parsed


def _classify_screen_question(question: str) -> str:
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
