from __future__ import annotations

import re


BRACKET_SENTENCE_BOUNDARIES = set("。！？!?…")
BRACKET_PAUSE_MARKS = set("、，,；;：:")

# Display-only bilingual dialog markers (character.dialog_display_language ==
# "zh"): the model appends a ⟦中文⟧ translation after each Japanese sentence.
# The Japanese side stays the spoken/memory text; the ⟦⟧ side is display-only.
DIALOG_TRANSLATION_OPEN = "⟦"
DIALOG_TRANSLATION_CLOSE = "⟧"


def split_dialog_translation(text: str) -> tuple[str, str]:
    """Split ``日本語。⟦中文。⟧`` pairs into (spoken_japanese, chinese_subtitle).

    Text without ``⟦`` is returned untouched as ``(text, "")`` -- the ja-mode
    byte-identity short-circuit. An unclosed ``⟦`` (stream cut mid-translation)
    treats the rest as translation; a stray ``⟧`` is dropped from the spoken side.
    """
    source = text or ""
    if DIALOG_TRANSLATION_OPEN not in source:
        return source, ""
    spoken_parts: list[str] = []
    subtitle_parts: list[str] = []
    index = 0
    while index < len(source):
        open_at = source.find(DIALOG_TRANSLATION_OPEN, index)
        if open_at < 0:
            spoken_parts.append(source[index:])
            break
        spoken_parts.append(source[index:open_at])
        close_at = source.find(DIALOG_TRANSLATION_CLOSE, open_at + 1)
        if close_at < 0:
            subtitle_parts.append(source[open_at + 1:])
            break
        subtitle_parts.append(source[open_at + 1:close_at])
        index = close_at + 1
    spoken = "".join(spoken_parts).replace(DIALOG_TRANSLATION_CLOSE, "")
    spoken = re.sub(r"\s+", " ", spoken).strip()
    subtitle = re.sub(r"\s+", " ", "".join(subtitle_parts)).strip()
    return spoken, subtitle


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


def build_tts_text(display_text: str) -> str:
    """Clean display text into something suitable to read aloud (TTS).

    Moved here from agent/streaming_pipeline.py in Phase 6C: text normalization
    (including the Japanese math read-aloud substitutions) belongs in the
    normalizer, not the streaming orchestrator.
    """
    text = display_text or ""
    text = re.sub(r"`+", "", text)
    text = _replace_math_for_speech(text)
    text = re.sub(r"\$[^$]*\$", "", text)
    text = re.sub(r"\\\([^)]*\\\)", "", text)
    text = re.sub(r"\\\[[^\]]*\\\]", "", text)
    text = re.sub(r"\be\^\{[^}]*\}", "", text)
    text = re.sub(r"[A-Za-z0-9_\\]+(?:\s*[=+\-*/^<>]\s*[A-Za-z0-9_\\{}().]+)+", "", text)
    text = _strip_formula_brackets(text)
    text = normalize_square_brackets_for_speech(text)
    text = text.replace("『", "").replace("』", "").replace("「", "").replace("」", "")
    text = re.sub(r"[#*_~>|]+", "", text)
    text = re.sub(r"\s*=\s*", "は", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?<=[ぁ-んァ-ン一-龯])\s+(?=[ぁ-んァ-ン一-龯])", "", text)
    text = re.sub(r"\s+([。！？!?、，,；;：:])", r"\1", text)
    text = re.sub(r"([、，,；;：:])\s+", r"\1", text)
    text = re.sub(r"[、，,；;：:]+([。！？!?])", r"\1", text)
    text = re.sub(r"[、，,；;：:]+$", "。", text)
    text = re.sub(r"。+", "。", text)
    return text.strip() or (display_text or "").strip()


def _replace_math_for_speech(text: str) -> str:
    text = re.sub(
        r"e\s*\^\s*\{\s*-\s*i\s*(?:ω|\\omega)\s*t\s*\}",
        "イーのマイナスアイオメガティー乗",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"f\s*['′’]\s*\(?\s*x\s*\)?",
        "エフダッシュエックス",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bf\s*\(\s*x\s*\)",
        "エフエックス",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\ba\s+f\s*\(?\s*x\s*\)?",
        "エーかけるエフエックス",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\bfx\b", "エフエックス", text, flags=re.IGNORECASE)
    text = re.sub(r"→\s*2\s*x\b", "は二エックス", text, flags=re.IGNORECASE)
    text = re.sub(r"\b2\s*x\b", "二エックス", text, flags=re.IGNORECASE)
    text = re.sub(r"\bx\s*\^\s*2\b", "エックスの二乗", text, flags=re.IGNORECASE)
    text = text.replace("→", "から")
    text = re.sub(r"(導関数|微分係数)\s*は\s*エフダッシュエックス", r"\1のエフダッシュエックス", text)
    return text


def _strip_formula_brackets(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        inner = match.group(1)
        if re.search(r"[=+\-*/^{}\\]|[A-Za-z]{2,}\d*", inner):
            return ""
        return inner

    text = re.sub(r"（([^（）]*)）", replace, text)
    text = re.sub(r"\(([^()]*)\)", replace, text)
    return text
