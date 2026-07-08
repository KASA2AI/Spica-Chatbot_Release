"""Streaming text -> play units (Phase 6C).

Pure components moved verbatim out of agent/streaming_pipeline.py:
- ``JsonAnswerExtractor`` incrementally pulls the ``answer`` string out of a
  partial JSON model reply.
- ``PlayUnitSplitter`` cuts streamed answer text into playable units by
  punctuation / length.

No threading, no I/O -- just parsing. Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import re

from spica.conversation.text_normalizer import (
    DIALOG_TRANSLATION_CLOSE,
    DIALOG_TRANSLATION_OPEN,
)

_TERMINATORS = set("。！？!?")
_CLOSERS = set("」』）)]”’\"'")
# Quote-bracket spans whose INNER sentence terminators must not end a play unit
# (zh bilingual mode). When Spica quotes a galgame line she wraps it in these, e.g.
# 理理は【今日はいい天気ね。】と言った。⟦…⟧ -- the 。 inside 【】 is quoted content, not
# a sentence boundary, so cutting there would split the Japanese run from its
# trailing ⟦中文⟧ translation (subtitle desync). Depth-tracked alongside ⟦⟧ in
# _take_complete_bilingual_sentences (spoken side -- NOT stripped for sizing).
_QUOTE_OPENERS = set("【「『")
_QUOTE_CLOSERS = set("】」』")
# One ⟦中文⟧ translation span (unclosed tail included) -- bilingual mode only.
_TRANSLATION_SPAN_RE = re.compile(
    f"{DIALOG_TRANSLATION_OPEN}[^{DIALOG_TRANSLATION_CLOSE}]*{DIALOG_TRANSLATION_CLOSE}?"
)


class JsonAnswerExtractor:
    """Incrementally extracts the JSON answer string from a partial model reply."""

    def __init__(self) -> None:
        self.answer = ""

    def feed(self, raw_text: str) -> str:
        current = self._extract_answer(raw_text)
        if current.startswith(self.answer):
            delta = current[len(self.answer):]
        else:
            delta = current
        self.answer = current
        return delta

    def _extract_answer(self, raw_text: str) -> str:
        match = re.search(r'"answer"\s*:\s*"', raw_text or "")
        if not match:
            return ""

        chars: list[str] = []
        index = match.end()
        while index < len(raw_text):
            char = raw_text[index]
            if char == '"':
                break
            if char != "\\":
                chars.append(char)
                index += 1
                continue

            if index + 1 >= len(raw_text):
                break
            escape = raw_text[index + 1]
            if escape == "u":
                hex_value = raw_text[index + 2:index + 6]
                if len(hex_value) < 4 or not re.fullmatch(r"[0-9a-fA-F]{4}", hex_value):
                    break
                chars.append(chr(int(hex_value, 16)))
                index += 6
                continue
            chars.append(
                {
                    '"': '"',
                    "\\": "\\",
                    "/": "/",
                    "b": "\b",
                    "f": "\f",
                    "n": "\n",
                    "r": "\r",
                    "t": "\t",
                }.get(escape, escape)
            )
            index += 2
        return "".join(chars)


class PlayUnitSplitter:
    def __init__(
        self,
        min_chars: int = 18,
        max_chars: int = 96,
        bilingual_brackets: bool = False,
    ) -> None:
        self.min_chars = max(1, int(min_chars))
        self.max_chars = max(self.min_chars, int(max_chars))
        # Bilingual display mode (character.dialog_display_language == "zh"):
        # a ⟦中文⟧ translation follows each Japanese sentence. When True,
        # terminators inside ⟦⟧ never cut, a ⟦⟧ right after a terminator stays
        # attached to that sentence, and unit sizing counts the Japanese side
        # only. When False (default) every path below is the original code.
        self.bilingual_brackets = bool(bilingual_brackets)
        self.buffer = ""
        self.current = ""
        self.completed_sentence_count = 0

    def feed(self, text: str) -> list[str]:
        self.buffer += text or ""
        units: list[str] = []
        for sentence in self._take_complete_sentences():
            self.completed_sentence_count += 1
            for part in self._split_overlong(sentence):
                units.extend(self._consume_part(part, force=False))
        return units

    def flush(self) -> list[str]:
        units: list[str] = []
        tail = self._clean_text(self.buffer)
        self.buffer = ""
        if tail:
            for part in self._split_overlong(tail):
                units.extend(self._consume_part(part, force=False))
        if self.current:
            units.append(self.current)
            self.current = ""
        return [unit for unit in units if unit]

    def _take_complete_sentences(self) -> list[str]:
        if self.bilingual_brackets:
            return self._take_complete_bilingual_sentences()
        sentences: list[str] = []
        index = 0
        while index < len(self.buffer):
            if self.buffer[index] not in _TERMINATORS:
                index += 1
                continue

            end = index + 1
            while end < len(self.buffer) and self.buffer[end] in _CLOSERS:
                end += 1
            sentence = self._clean_text(self.buffer[:end])
            if sentence:
                sentences.append(sentence)
            self.buffer = self.buffer[end:]
            index = 0
        return sentences

    def _take_complete_bilingual_sentences(self) -> list[str]:
        # Bilingual variant: a terminator inside ⟦⟧ OR inside a quote bracket
        # (【】「」『』 -- Spica quoting a galgame line) never ends a sentence, and a
        # ⟦translation⟧ right after the terminator belongs to that sentence. A
        # sentence at the exact buffer end is NOT emitted yet -- the next delta
        # may open its ⟦⟧ (flush() releases the tail at end of stream).
        sentences: list[str] = []
        index = 0
        depth = 0
        while index < len(self.buffer):
            char = self.buffer[index]
            if char == DIALOG_TRANSLATION_OPEN or char in _QUOTE_OPENERS:
                depth += 1
                index += 1
                continue
            if char == DIALOG_TRANSLATION_CLOSE or char in _QUOTE_CLOSERS:
                depth = max(0, depth - 1)
                index += 1
                continue
            if depth > 0 or char not in _TERMINATORS:
                index += 1
                continue

            end = index + 1
            while end < len(self.buffer) and self.buffer[end] in _CLOSERS:
                end += 1
            probe = end
            while probe < len(self.buffer) and self.buffer[probe].isspace():
                probe += 1
            if probe >= len(self.buffer):
                break  # a ⟦translation⟧ may still follow -- wait for more stream
            if self.buffer[probe] == DIALOG_TRANSLATION_OPEN:
                close = self.buffer.find(DIALOG_TRANSLATION_CLOSE, probe + 1)
                if close < 0:
                    break  # translation still streaming -- wait
                end = close + 1
            sentence = self._clean_text(self.buffer[:end])
            if sentence:
                sentences.append(sentence)
            self.buffer = self.buffer[end:]
            index = 0
            depth = 0
        return sentences

    def _visible_len(self, text: str) -> int:
        # Unit sizing counts the SPOKEN (Japanese) side only: ⟦中文⟧ spans are
        # display-only and must not distort the min/max pacing tuned for TTS.
        if not self.bilingual_brackets:
            return len(text)
        return len(_TRANSLATION_SPAN_RE.sub("", text or ""))

    def _consume_part(self, part: str, force: bool = False) -> list[str]:
        part = self._clean_text(part)
        if not part:
            return []

        candidate_len = self._visible_len(self.current) + self._visible_len(part)
        if self.current and (
            candidate_len <= self.max_chars
            or (self._visible_len(self.current) < self.min_chars and candidate_len <= self.max_chars + self.min_chars)
        ):
            self.current += part
        elif self.current:
            completed = self.current
            self.current = ""
            return [completed] + self._consume_part(part, force=force)
        else:
            self.current = part

        if force or self._can_emit(self.current):
            completed = self.current
            self.current = ""
            return [completed]
        return []

    def _can_emit(self, text: str) -> bool:
        compact = re.sub(r"\s+", "", text or "")
        if self.bilingual_brackets:
            compact = _TRANSLATION_SPAN_RE.sub("", compact)
        if len(compact) < self.min_chars:
            return False
        return compact not in {"もちろん。", "はい。", "ええ。", "そうですね。"}

    def _split_overlong(self, sentence: str) -> list[str]:
        sentence = self._clean_text(sentence)
        if self._visible_len(sentence) <= self.max_chars:
            return [sentence]
        if self.bilingual_brackets and DIALOG_TRANSLATION_OPEN in sentence:
            # Keep the 日语⟦中文⟧ pair atomic: sub-splitting by pause marks would
            # cut inside ⟦⟧. The TTS engine re-chunks internally anyway.
            return [sentence]

        parts = [
            match.group(0)
            for match in re.finditer(r"[^、，,；;]+[、，,；;]*", sentence)
            if match.group(0)
        ]
        if len(parts) <= 1:
            return [sentence[index:index + self.max_chars] for index in range(0, len(sentence), self.max_chars)]

        chunks: list[str] = []
        current = ""
        for part in parts:
            if current and len(current) + len(part) > self.max_chars:
                chunks.append(current)
                current = part
            else:
                current += part
        if current:
            chunks.append(current)
        return chunks

    def _clean_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()
