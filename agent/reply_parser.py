from __future__ import annotations

import json
import re
from typing import Any


EMOTION_LABELS = {
    "happy": "喜/乐",
    "angry": "怒",
    "sad": "哀",
    "surprised": "惊",
}


def normalize_emotion(emotion: str | None) -> str:
    aliases = {
        "joy": "happy",
        "fun": "happy",
        "happy": "happy",
        "喜": "happy",
        "乐": "happy",
        "angry": "angry",
        "anger": "angry",
        "怒": "angry",
        "sad": "sad",
        "sorrow": "sad",
        "哀": "sad",
        "悲": "sad",
        "surprised": "surprised",
        "surprise": "surprised",
        "惊": "surprised",
        "驚": "surprised",
    }
    value = (emotion or "").strip().lower()
    return aliases.get(value, value if value in EMOTION_LABELS else "happy")


def guess_emotion(text: str) -> str:
    if any(token in text for token in ("許せ", "だめ", "駄目", "危険", "やめ", "怒")):
        return "angry"
    if any(token in text for token in ("悲", "残念", "つら", "辛", "ごめん", "すみません")):
        return "sad"
    if any(token in text for token in ("えっ", "まさか", "驚", "本当", "？", "?")):
        return "surprised"
    return "happy"


def parse_model_reply(output_text: str) -> dict[str, str]:
    raw_text = (output_text or "").strip()
    parsed: dict[str, Any] | None = None

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw_text, flags=re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                parsed = None

    if not isinstance(parsed, dict):
        answer = raw_text or "すみません、もう一度話しかけてください。"
        return {
            "answer": answer,
            "emotion": guess_emotion(answer),
            "emotion_reason": "模型没有返回合法 JSON，使用文本启发式兜底。",
        }

    answer = str(parsed.get("answer") or "").strip()
    if not answer:
        answer = "すみません、もう一度話しかけてください。"

    return {
        "answer": answer,
        "emotion": normalize_emotion(str(parsed.get("emotion") or "")),
        "emotion_reason": str(parsed.get("emotion_reason") or "模型按回复语气选择。").strip(),
    }
