from __future__ import annotations

import re


BRACKET_SENTENCE_BOUNDARIES = set("。！？!?…")
BRACKET_PAUSE_MARKS = set("、，,；;：:")


def normalize_square_brackets_for_speech(text: str) -> str:
    source = text or ""

    def previous_visible(index: int) -> str:
        cursor = index - 1
        while cursor >= 0 and source[cursor].isspace():
            cursor -= 1
        return source[cursor] if cursor >= 0 else ""

    def next_visible(index: int) -> str:
        cursor = index
        while cursor < len(source) and source[cursor].isspace():
            cursor += 1
        return source[cursor] if cursor < len(source) else ""

    def replace(match: re.Match[str]) -> str:
        inner = re.sub(r"\s+", " ", match.group(1)).strip()
        if not inner:
            return ""

        before = previous_visible(match.start())
        after = next_visible(match.end())
        prefix = "" if not before or before in BRACKET_SENTENCE_BOUNDARIES or before in BRACKET_PAUSE_MARKS else "、"
        if inner[-1] in BRACKET_SENTENCE_BOUNDARIES or inner[-1] in BRACKET_PAUSE_MARKS:
            suffix = ""
        elif not after or after in BRACKET_SENTENCE_BOUNDARIES:
            suffix = "。"
        else:
            suffix = "、"
        return f"{prefix}{inner}{suffix}"

    normalized = re.sub(r"(?<!\\)\[([^\[\]]*)\]", replace, source)
    normalized = re.sub(r"(?<!\\)\[", "、", normalized)
    normalized = re.sub(r"(?<!\\)\]", "。", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = re.sub(r"\s+([。！？!?、，,；;：:])", r"\1", normalized)
    normalized = re.sub(r"([、，,；;：:])\s+", r"\1", normalized)
    normalized = re.sub(r"[、，,；;：:]{2,}", "、", normalized)
    normalized = re.sub(r"[、，,；;：:]+([。！？!?])", r"\1", normalized)
    normalized = re.sub(r"([。！？!?])、", r"\1", normalized)
    normalized = re.sub(r"。{2,}", "。", normalized)
    return normalized
