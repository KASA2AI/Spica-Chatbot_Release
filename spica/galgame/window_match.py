"""Pure window-matching/scoring (§17.3). Qt-free, platform-independent.

Lives in the domain (NOT the locator adapter) so Bottles/Windows share one scorer
and it is unit-testable without a real window system.

Rules (do NOT oversell as a strong match -- Wine/Bottles process name + WM_CLASS
are unreliable):

- ``title_keywords`` is the PRIMARY filter: a candidate must hit >=1 keyword to
  qualify. If no keywords are configured, ALL candidates qualify -- forcing an
  explicit user pick/confirm (never an auto-guess).
- ``process_name`` / ``app_id`` / ``last_full_title`` are AUXILIARY tiebreakers
  only -- they re-order qualified candidates, never promote a window that did not
  match a title keyword.
- ``last_full_title`` is historical reference, NOT a match key: the window title
  changes with route/chapter, so matching keys off keywords, not the full title.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from spica.galgame.models import WindowMatchRule
from spica.ports.window_locator import WindowCandidate


class WindowMatchOutcome(str, Enum):
    NONE = "none"
    UNIQUE = "unique"
    MULTIPLE = "multiple"


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: WindowCandidate
    score: float
    keyword_hits: int


def score_candidates(candidates: list[WindowCandidate], rule: WindowMatchRule) -> list[ScoredCandidate]:
    keywords = [k for k in (rule.title_keywords or []) if k]
    scored: list[ScoredCandidate] = []
    for candidate in candidates:
        title = (candidate.title or "").lower()
        if keywords:
            hits = sum(1 for keyword in keywords if keyword.lower() in title)
            if hits == 0:
                continue  # PRIMARY filter: no keyword hit -> not a candidate
        else:
            hits = 0  # no keywords configured -> everything qualifies (force user pick)
        score = float(hits)
        # AUXILIARY tiebreakers only -- small deltas that never outweigh a keyword hit
        # and never promote a non-title-matching window (filtered out above).
        if rule.process_name and candidate.process_name and rule.process_name.lower() == candidate.process_name.lower():
            score += 0.3
        if rule.app_id and candidate.app_id and rule.app_id.lower() == candidate.app_id.lower():
            score += 0.2
        if rule.last_full_title and candidate.title and rule.last_full_title == candidate.title:
            score += 0.1
        scored.append(ScoredCandidate(candidate=candidate, score=score, keyword_hits=hits))
    scored.sort(key=lambda item: item.score, reverse=True)
    return scored


def title_matches_rule(title: str, rule: WindowMatchRule) -> bool:
    """True if a window TITLE matches the game's WindowMatchRule by keyword -- the
    SAME matcher binding uses (``score_candidates``), reused here so the safety
    check judges focus by title, NOT by volatile wine/Bottles window ids (§17.3).

    Empty keywords -> False: with nothing to match on we cannot verify focus, so we
    conservatively treat it as "not the game" (never relax "绝不误截", §7.1)."""
    keywords = [k for k in (rule.title_keywords or []) if k]
    if not keywords:
        return False
    return bool(score_candidates([WindowCandidate(window_id="", title=title)], rule))


def classify(scored: list[ScoredCandidate]) -> WindowMatchOutcome:
    if not scored:
        return WindowMatchOutcome.NONE
    if len(scored) == 1:
        return WindowMatchOutcome.UNIQUE
    return WindowMatchOutcome.MULTIPLE
