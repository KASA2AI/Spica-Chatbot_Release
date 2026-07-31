"""Shared anime-title aliases.

The immutable defaults keep direct domain calls backwards compatible, while
``default_title_aliases`` gives the typed configuration layer an independent,
mutable copy.  Runtime callers may replace the mapping through
``anime.title_aliases`` without making the pure resolver read configuration.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType

TitleAliases = Mapping[str, Sequence[str]]

_TITLE_IDENTITY_PUNCTUATION_RE = re.compile(
    r"[\s，,。.·・:：!！?？'\"「」『』()（）\-~～/]+")

DEFAULT_TITLE_ALIASES: TitleAliases = MappingProxyType({
    "无职转生": (
        "無職転生",
        "Mushoku Tensei",
    ),
    "关于我转生变成史莱姆这档事": (
        "转生史莱姆",
        "転生したらスライムだった件",
        "Tensei Shitara Slime Datta Ken",
    ),
    "与你相恋到生命尽头": (
        "只愿深入爱河",
        "きみが死ぬまで恋をしたい",
    ),
    "少女怪兽焦糖恋心": (
        "少女怪兽焦糖味",
        "乙女怪獣キャラメリゼ",
    ),
    "在超市后门吸烟的二人": (
        "躲在超市后门抽烟的两人",
        "躲在超市后门吸烟的二人",
        "スーパーの裏でヤニ吸うふたり",
    ),
    "PLANNOSAURUS 认真古生物部": (
        "PLANNOSAURUS 真古生遗物部",
        "真古生遗物部",
        "プラノサウルス ガチコセイブツ部",
    ),
})


def normalize_title_identity(title: str) -> str:
    """Normalize a title for alias identity and collision checks."""
    return _TITLE_IDENTITY_PUNCTUATION_RE.sub("", title).lower()


def normalized_title_alias_groups(
    title_aliases: TitleAliases,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Validate and normalize aliases into canonical identity groups.

    Empty normalized values would make replacement unsafe, while one normalized
    title owned by two groups would make episode keys depend on YAML ordering.
    Reject both at the shared domain boundary so direct resolver callers receive
    the same protection as typed configuration.
    """
    groups: list[tuple[str, tuple[str, ...]]] = []
    owners: dict[str, str] = {}
    canonicals: set[str] = set()
    for canonical, aliases in title_aliases.items():
        if not isinstance(canonical, str):
            raise ValueError("anime title alias canonical name must be text")
        canonical_norm = normalize_title_identity(canonical)
        if not canonical_norm:
            raise ValueError(
                "anime title alias canonical name has no title characters")
        if canonical_norm in canonicals:
            raise ValueError(
                "anime title alias canonical names must be unique")
        if isinstance(aliases, (str, bytes)):
            raise ValueError("anime title aliases must be a sequence of names")
        if not aliases:
            raise ValueError(
                "anime title alias group must contain at least one alias")
        canonicals.add(canonical_norm)

        members: list[str] = []
        for member in (canonical, *aliases):
            if not isinstance(member, str):
                raise ValueError("anime title alias must be text")
            member_norm = normalize_title_identity(member)
            if not member_norm:
                raise ValueError(
                    "anime title alias has no title characters")
            for known_norm, owner in owners.items():
                if (owner != canonical_norm
                        and (member_norm in known_norm
                             or known_norm in member_norm)):
                    raise ValueError(
                        "overlapping anime title aliases belong to "
                        "multiple groups")
            owners[member_norm] = canonical_norm
            if member_norm not in members:
                members.append(member_norm)
        groups.append((canonical_norm, tuple(members)))
    return tuple(groups)


def default_title_aliases() -> dict[str, list[str]]:
    """Return a fresh configuration-safe copy of the built-in alias mapping."""
    return {
        canonical: list(aliases)
        for canonical, aliases in DEFAULT_TITLE_ALIASES.items()
    }
