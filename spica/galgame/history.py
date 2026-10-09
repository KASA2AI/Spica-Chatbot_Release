"""A bounded game-history card from the existing game store, published by the host.

The card preserves shared play, game names, progress, recent cast, relationships
and plot with game framing. Appearance lists identify who appeared, never who
is the protagonist or what gender they are. Unknown identities remain unknown;
no new model call or inferred character ranking is added here.

Whole optional segments fit within the card budget. Route guesses remain marked
as guesses; only the player's confirmation produces a confirmed route.
"""

from __future__ import annotations

from typing import Any

from spica.galgame.models import utc_now_iso
from spica.ports.game_memory import GameMemoryPort

# §13.5: an unconfirmed (LLM-guessed) route below this confidence is NOT worth a
# claim in character memory -- omit rather than guess wrong about "which 线".
ROUTE_CONFIDENCE_THRESHOLD = 0.6
# The existing bounded card is copied whole into the personal history episode.
CARD_MAX_CHARS = 220
# Recent summaries supply names and the latest plot snippet.
_HISTORY_SUMMARY_WINDOW = 3


def _truncate(text: Any, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _appearing_characters(summaries: Any) -> list[str]:
    names = []
    for summary in summaries or []:
        for raw in getattr(summary, "characters", None) or []:
            name = str(raw).strip()
            if name and name not in names:
                names.append(name)
                if len(names) == 3:
                    return names
    return names


def _chapter_phrase(progress: Any) -> str:
    chapter = (getattr(progress, "chapter", None) or {}) if progress is not None else {}
    title = chapter.get("title")
    return f"，玩到{_truncate(title, 14)}" if title else ""


def _route_phrase(progress: Any) -> str:
    route = (getattr(progress, "route", None) or {}) if progress is not None else {}
    name = route.get("name")
    if not name:
        return ""
    if route.get("confirmed"):
        return f"，已确认走{_truncate(name, 12)}线"
    if float(route.get("confidence") or 0.0) >= ROUTE_CONFIDENCE_THRESHOLD:
        return f"，似乎在{_truncate(name, 12)}线"
    return ""  # low-confidence guess: say nothing rather than guess wrong (§13.5)


def _relation_phrases(relations: Any, limit: int = 2) -> list[str]:
    usable = [
        relation
        for relation in (relations or [])
        if relation.character_a and relation.character_b and relation.relation_summary
    ]
    usable.sort(key=lambda relation: -float(relation.confidence or 0.0))
    return [
        f"。游戏里的{_truncate(relation.character_a, 10)}和{_truncate(relation.character_b, 10)}"
        f"是{_truncate(relation.relation_summary, 20)}"
        for relation in usable[:limit]
    ]


def build_play_history_card(
    *,
    display_name: str,
    game_id: str | None = None,
    progress: Any = None,
    relations: Any = None,
    summaries: Any = None,
    played_at: str | None = None,
    user_name: str = "麦",
    max_chars: int = CARD_MAX_CHARS,
) -> str:
    """Greedy priority assembly: the head (game name, both scripts) + date tail
    always fit; optional segments join in priority order and an overflowing
    segment is dropped whole -- <= CARD_MAX_CHARS by construction."""
    date = str(played_at or utc_now_iso())[:10]
    head = f"{_truncate(user_name, 6)}和我一起玩了游戏《{_truncate(display_name, 24)}》"
    if game_id and str(game_id).strip().lower() != str(display_name or "").strip().lower():
        head += f"（{_truncate(game_id, 12)}）"  # latin token for English-worded queries
    tail = f"。（{date}）"

    segments: list[str] = []
    characters = _appearing_characters(summaries)
    if characters:
        segments.append("。最近出场角色：" + "、".join(_truncate(name, 12) for name in characters))
    progress_phrase = f"{_chapter_phrase(progress)}{_route_phrase(progress)}"
    if progress_phrase:
        segments.append(progress_phrase)
    segments.extend(_relation_phrases(relations, limit=2))
    latest = (list(summaries)[0] if summaries else None)
    latest_text = getattr(latest, "summary_zh", "") if latest is not None else ""
    if latest_text:
        segments.append(f"。最近剧情：{_truncate(latest_text, 56)}")

    budget = max_chars - len(tail)
    card = head
    for segment in segments:
        if len(card) + len(segment) <= budget:
            card += segment
    if card.endswith("。"):  # a segment text ending in 。 would double with the tail's
        card = card[:-1]
    return card + tail


def compose_play_history(
    game_memory: GameMemoryPort,
    game_id: str,
    playthrough_id: str = "default",
    *,
    user_name: str = "麦",
    max_chars: int = CARD_MAX_CHARS,
) -> str | None:
    """Read the store + assemble the card; ``None`` when nothing was ever played
    (no progress, no relations, no summaries -- a bare GameProfile is just a name,
    not an experience worth a memory)."""
    profile = game_memory.get_game_profile(game_id)
    progress = game_memory.get_progress_state(game_id, playthrough_id)
    relations = game_memory.character_relations(game_id, playthrough_id)
    summaries = game_memory.recent_summaries(game_id, playthrough_id, limit=_HISTORY_SUMMARY_WINDOW)
    if progress is None and not relations and not summaries:
        return None
    display_name = (profile.display_name if profile is not None else "") or game_id
    played_at = (progress.last_played_at if progress is not None else "") or None
    return build_play_history_card(
        display_name=display_name,
        game_id=game_id,
        progress=progress,
        relations=relations,
        summaries=summaries,
        played_at=played_at,
        user_name=user_name,
        max_chars=max_chars,
    )
