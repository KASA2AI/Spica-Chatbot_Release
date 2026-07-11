from __future__ import annotations

import re
import unicodedata


BRACKET_SENTENCE_BOUNDARIES = set("。！？!?…")
BRACKET_PAUSE_MARKS = set("、，,；;：:")

# Display-only bilingual dialog markers (character.dialog_display_language ==
# "zh"): the model appends a ⟦中文⟧ translation after each Japanese sentence.
# The Japanese side stays the spoken/memory text; the ⟦⟧ side is display-only.
DIALOG_TRANSLATION_OPEN = "⟦"
DIALOG_TRANSLATION_CLOSE = "⟧"
MISSING_CHINESE_SUBTITLE = "（中文字幕暂时缺失。）"
MISSING_SPOKEN_DIALOG = "すみません、もう一度話しかけてください。"


def contains_japanese_script(text: str) -> bool:
    return any(
        any(
            marker in unicodedata.name(char, "")
            for marker in ("HIRAGANA", "KATAKANA", "KANA")
        )
        for char in text
    )


def spoken_channel_is_paired(source: str) -> bool:
    """Whether the last outside event is a matched ``spoken⟦subtitle⟧`` close."""
    inside_translation = False
    saw_translation = False
    last_was_matched_close = False
    for char in source:
        if inside_translation:
            if char == DIALOG_TRANSLATION_CLOSE:
                inside_translation = False
                last_was_matched_close = True
            continue
        if char == DIALOG_TRANSLATION_OPEN:
            inside_translation = True
            saw_translation = True
            last_was_matched_close = False
        elif not char.isspace():
            # Spoken tail or a stray close after a valid subtitle group.
            last_was_matched_close = False
    return saw_translation and not inside_translation and last_was_matched_close


def spoken_channel_or_fallback(text: str, *, paired_subtitle: bool = False) -> str:
    """Return a structurally trusted spoken channel or the Japanese fallback.

    A non-empty side from ``spoken⟦subtitle⟧`` is trusted because Han-only text
    cannot be classified as Chinese vs Japanese from Unicode alone. Unpaired
    text must contain Japanese script; otherwise it degrades conservatively.
    """
    spoken = text.strip()
    if spoken and (paired_subtitle or contains_japanese_script(spoken)):
        return spoken
    return MISSING_SPOKEN_DIALOG


def _display_subtitle_or_missing(text: str) -> str:
    subtitle = text.strip()
    if not subtitle or contains_japanese_script(subtitle):
        return MISSING_CHINESE_SUBTITLE
    return subtitle


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


def build_bilingual_display(text: str) -> str:
    """Per-sentence display string for zh mode.

    Walk ``日语。⟦中文。⟧日语。⟦中文。⟧`` and, for each ``⟦中文⟧`` the model gave,
    show that translation; where a Japanese sentence has NO following ``⟦⟧``, show
    a Chinese missing-subtitle notice instead of leaking the spoken Japanese into
    the zh display channel.
    """
    source = text or ""
    if DIALOG_TRANSLATION_OPEN not in source:
        return MISSING_CHINESE_SUBTITLE if source.strip() else ""
    parts: list[str] = []
    index = 0
    while index < len(source):
        open_at = source.find(DIALOG_TRANSLATION_OPEN, index)
        if open_at < 0:
            if source[index:].strip():
                parts.append(MISSING_CHINESE_SUBTITLE)
            break
        close_at = source.find(DIALOG_TRANSLATION_CLOSE, open_at + 1)
        translation = source[open_at + 1:] if close_at < 0 else source[open_at + 1:close_at]
        # The ⟦中文⟧ translates the ENTIRE preceding Japanese run: the model groups
        # several sentences under ONE translation (real output e.g.
        # "ふぅん……麦。こんな時間に珍しいわね。⟦哼……麦。这个时间来还真少见呢。⟧"), so show the
        # Chinese and drop that whole run. An EMPTY ⟦⟧ is an invalid subtitle,
        # so surface the Chinese notice rather than the Japanese run.
        parts.append(_display_subtitle_or_missing(translation))
        if close_at < 0:
            break
        index = close_at + 1
    display = "".join(parts).replace(DIALOG_TRANSLATION_CLOSE, "")
    return re.sub(r"\s+", " ", display).strip()


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
