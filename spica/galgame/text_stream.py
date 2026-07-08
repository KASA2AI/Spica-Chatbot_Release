"""Stable-line tracking for the OCR text stream (Phase 7, §10.3). Pure, Qt-free.

A galgame dialogue box is OCR'd repeatedly. This turns that noisy, repeating,
mid-typewriter stream into clean "stable lines":

- a line must read the same (or highly similar) for ``stability_required`` cycles
  before it counts as stable -- so a half-typed sentence (which keeps changing) is
  never committed, only the final settled text is;
- one-character OCR jitter on a settled line is absorbed (similarity, not equality);
- while the same line stays on screen it is NOT re-emitted.

The tracker only tracks TEXT (for stability). The owning session carries the
speaker alongside and turns NEW_STABLE into a pending->committed StoryLine flow.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from enum import Enum


class StableOutcome(str, Enum):
    EMPTY = "empty"  # nothing readable
    PENDING = "pending"  # changed / still settling (e.g. typewriter) -- not stable yet
    SAME = "same"  # same as the current stable line -- do not re-emit
    NEW_STABLE = "new_stable"  # a new line just settled -> commit the previous, write this


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def similar(a: str, b: str, threshold: float) -> bool:
    if a == b:
        return True
    return SequenceMatcher(None, a, b).ratio() >= threshold


class StableLineTracker:
    def __init__(self, stability_required: int = 2, similarity_threshold: float = 0.9) -> None:
        self._required = max(1, int(stability_required))
        self._threshold = float(similarity_threshold)
        self._candidate: str | None = None
        self._count = 0
        self._stable: str | None = None

    def feed(self, text: str) -> StableOutcome:
        text = clean_text(text)
        if not text:
            return StableOutcome.EMPTY
        # Same line still on screen (jitter absorbed) -> no re-emit (§10.6).
        if self._stable is not None and similar(text, self._stable, self._threshold):
            self._candidate = text
            self._count = self._required
            return StableOutcome.SAME
        if self._candidate is not None and similar(text, self._candidate, self._threshold):
            self._count += 1
        else:
            self._candidate = text
            self._count = 1
        if self._count >= self._required:
            self._stable = text
            return StableOutcome.NEW_STABLE
        return StableOutcome.PENDING


# Speaker parsing: ordered, first match wins. Bracket markup FIRST (it is
# unambiguous speaker notation), then the prefix-delimiter form (the original
# single regex, behaviour-pinned by the pre-existing tests).
#
# The bracket pattern is shaped by the real LimeLight capture (1118 OCR'd
# lines, 2026-06-11): the closing bracket is OPTIONAL because OCR drops it
# (18/721 lines came out as "【月望 「..."), and the dialogue's closing quote
# is routinely misread (」 -> 』 ] or 1) -- the speaker must cut clean; the
# dialogue may keep its noise (summaries/teasing need WHO + roughly WHAT).
# Bracket name excludes whitespace so a LOST closing bracket still bounds the
# name at the first space ("【大梦 因为…" -> 大梦; "【月望 「可是" -> 月望).
_BRACKET_SPEAKER = re.compile(
    r"^[【\[［]\s*(?P<name>[^】\]］「『（(:：\s]{1,12})[】\]］]?\s*[:：]?\s*(?P<text>.+)$"
)
# Prefix name is TIGHT on purpose: no whitespace, no sentence punctuation, max
# 6 chars (the 1118-line replay's longest real name is 3). Narration that embeds
# a quote or colon ("隐同学走向新进店的顾客： 去为他们点单。") must NOT mint a
# fake speaker -- a fake speaker misleads future teasing, a missed one merely
# degrades to narration, so precision beats recall here.
_PARSE_FROM_TEXT = re.compile(
    r"^(?P<name>[^「『（(:：\s，。、！？；…·～“”]{1,6})\s*(?P<delim>[「『（(:：])(?P<text>.+?)[」』）)]?$"
)

_QUOTE_OPENERS = "「『“"
# Trailing quote as REALLY produced by OCR: the clean closers plus the misreads
# observed in the capture. Only stripped when the text began with an opener, so
# a genuine trailing digit in narration is never eaten.
_DIRTY_QUOTE_CLOSERS = "」』”]｝}1"


def _strip_dialogue_quotes(text: str) -> str:
    text = text.strip()
    if text and text[0] in _QUOTE_OPENERS:
        text = text[1:]
        if text and text[-1] in _DIRTY_QUOTE_CLOSERS:
            text = text[:-1]
    return text.strip()


def _parse_speaker_from_text(text: str) -> tuple[str | None, str]:
    match = _BRACKET_SPEAKER.match(text)
    if match:
        # An empty dialogue (a name-only typewriter frame like "【雪鹰】「")
        # stays empty: the tracker then reports EMPTY and nothing is written.
        dialogue = clean_text(_strip_dialogue_quotes(match.group("text")))
        return clean_text(match.group("name")) or None, dialogue
    match = _PARSE_FROM_TEXT.match(text)
    if match:
        # A real dialogue's closing quote sits at the END (the regex already
        # consumed it). A closer left in the MIDDLE means the line is narration
        # embedding a quotation ("这家伙说的『注意』到底是指什么") -- reject, a
        # fake speaker misleads future teasing while None is just narration.
        if match.group("delim") in "「『" and any(c in match.group("text") for c in "」』"):
            return None, text
        # OCR drops the OPENING bracket too ("雪鹰】 「…", 50/1118 real lines):
        # the prefix path then captures the orphan closer -- strip bracket
        # residue from the name edges.
        name = clean_text(match.group("name")).strip("【】[]［］")
        if name.isdigit():
            # A clock readout in the dialog crop ("10: 12。") is not a speaker.
            return None, text
        return name or None, clean_text(match.group("text"))
    return None, text


def resolve_speaker(strategy: str, raw_speaker: str | None, raw_text: str) -> tuple[str | None, str]:
    """Resolve (speaker, dialogue_text) per OCRProfile.speaker_strategy (§18.2)."""
    text = clean_text(raw_text)
    if strategy == "region":
        speaker = clean_text(raw_speaker or "") or None
        if speaker is not None:
            return speaker, text
        # Region empty -- either an uncalibrated profile (LimeLight ships
        # strategy="region" with speaker_name_region=None, so the speaker was
        # NEVER resolved) or a narration frame. Fall back to text parsing:
        # persisted profiles benefit without migration or recalibration, and
        # narration carries no speaker markup so it still resolves to None.
        return _parse_speaker_from_text(text)
    if strategy == "parse_from_text":
        return _parse_speaker_from_text(text)
    # narration / narration_or_unknown / unknown -> no speaker
    return None, text
