"""Spica / legacy character compatibility data (Phase 4).

This module is the single home for Spica-specific names and the legacy role-card
name mapping. Keeping them here -- explicitly "compatibility data" -- lets the
generic prompt-building code (``prompt_builder`` / ``character_loader``) stay
character-agnostic and use ``{{char}}`` / ``{{user}}`` templates instead of
hard-coded ``スピカ`` / ``麦`` literals (CLAUDE.md Phase 4 acceptance).

When Phase 7 introduces real CharacterPackages, these defaults become per-package
data and this shim can be retired.
"""

from __future__ import annotations

import re

# Default identities for the current (Spica) character.
DEFAULT_INTERLOCUTOR_NAME = "麦"
DEFAULT_CHARACTER_NAME = "スピカ"

# Fallback values for the Spica role-card meta block.
SPICA_META_DEFAULTS = {"name": "辻倉朱比華", "source": "anemoi -アネモイ-"}


def _normalize_name(name: str | None) -> str:
    cleaned = re.sub(r"\s+", " ", name or "").strip()
    return cleaned or DEFAULT_INTERLOCUTOR_NAME


def replace_mugi_references(text: str, name: str | None = None) -> str:
    """Map the Spica role card's 速川麦 / Mugi / 麦 references to the interlocutor.

    Self-contained (no import of character_loader) so the dependency runs
    one-way: character_loader -> character_compat.
    """
    replacement = _normalize_name(name)
    if not text:
        return ""

    text = text.replace("速川麦", replacement)
    text = text.replace("Mugi", replacement)
    text = text.replace("むぎいいん", f"{replacement}ああん")
    # Replace the character name 麦, while preserving ordinary wheat words like 小麦, 麦田, 麦畑, 麦子.
    return re.sub(r"(?<!小)麦(?![子田畑克])", replacement, text)
