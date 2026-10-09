"""Local wake phrases and acknowledgement. Detection is acoustic, not text matching."""

from __future__ import annotations

import re
import unicodedata

from spica.core.character import CharacterPackage
from spica.core.proactive import ProactiveTurnRequest


def wake_reply_request(keyword: str) -> ProactiveTurnRequest:
    return ProactiveTurnRequest(
        source="desktop.voice_wake",
        directive=(
            f"用户刚刚呼叫了当前角色的唤醒词「{keyword}」，还没有提出问题。"
            "这是一次日常招呼：以当前角色的口吻，用一句有实际含义的简短回应或接话问句表示听到了，然后等用户继续说。"
            "不要只回复单个语气词；只说一个短句，正文不超过12个字符。"
            "例如日语的「どうしたの？」「呼んだ？」「うん、聞いてるよ。」或中文的「我在，怎么啦？」「叫我有什么事？」。"
            "根据角色性格自然措辞，避免重复最近的唤醒回应；示例用于说明简短程度，不要固定复读。"
            "保持当前语音和显示语言，翻译只对应这一句。"
            "不延续之前的话题，不自我介绍，不解释功能，不列举建议，不补充寒暄或动作描写。"
        ),
    )


def parse_wake_words(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(word.strip() for word in re.split(r"[,，、;；\n]", text) if word.strip()))


def default_wake_words(character: CharacterPackage) -> tuple[str, ...]:
    """Use authored defaults first, then pronounceable names from older cards.

    The local Chinese/English detector cannot consume kana. Filter only inferred
    names here; explicitly configured words still receive the detector's normal
    validation and visible error instead of being silently replaced.
    """
    if character.wake_words:
        return tuple(dict.fromkeys(word.strip() for word in character.wake_words if word.strip()))
    if character.character_id.casefold() == "spica":
        return ("斯皮卡", "Spica")

    def pronounceable_names(names: tuple[str, ...]) -> tuple[str, ...]:
        words = (unicodedata.normalize("NFKC", name).strip() for name in names)
        return tuple(dict.fromkeys(
            word for word in words
            if word and re.fullmatch(r"[A-Za-z' \-\u4e00-\u9fff]+", word)
        ))

    return pronounceable_names((character.char_name, *character.nicknames)) or pronounceable_names(
        tuple(re.split(r"[/／]", character.name)),
    )
