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

_TERMINATORS = set("。！？!?")
_CLOSERS = set("」』）)]”’\"'")


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
    def __init__(self, min_chars: int = 18, max_chars: int = 96) -> None:
        self.min_chars = max(1, int(min_chars))
        self.max_chars = max(self.min_chars, int(max_chars))
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

    def _consume_part(self, part: str, force: bool = False) -> list[str]:
        part = self._clean_text(part)
        if not part:
            return []

        candidate_len = len(self.current) + len(part)
        if self.current and (
            candidate_len <= self.max_chars
            or (len(self.current) < self.min_chars and candidate_len <= self.max_chars + self.min_chars)
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
        if len(compact) < self.min_chars:
            return False
        return compact not in {"もちろん。", "はい。", "ええ。", "そうですね。"}

    def _split_overlong(self, sentence: str) -> list[str]:
        sentence = self._clean_text(sentence)
        if len(sentence) <= self.max_chars:
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
