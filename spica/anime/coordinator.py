"""Source orchestration (Phase 1) -- main->fallback, budget, error trail. Pure.

Sequences the sources (bilibili main, mikan fallback), runs the resolver over
each source's candidates, materializes the ONE chosen candidate, and maps
outcomes so the host can pick the right ToolError (P1-10):
- a clean ``matched`` from any source wins immediately (and is materialized);
- ``ambiguous`` / ``need_episode`` is remembered but the coordinator keeps trying
  other sources for a clean match first, then surfaces it (ask the user);
- ALL sources raising (network / risk-control) -> ``source_error``;
- sources reachable but nothing found -> ``not_found``;
- the total ``budget_seconds`` / per-source timeout is exceeded -> ``resolve_timeout``
  (ANIME_RESOLVE_TIMEOUT upstream, P1-8); a ``cancelled`` predicate -> ``cancelled``.

No network here -- the injected ``AnimeSourcePort`` adapters do I/O and enforce
the real per-call cancellation; this layer expresses the budget via an injected
``clock`` (default ``time.monotonic``) so it stays deterministically testable.
Per-source errors are kept in a trail, not swallowed (finding #8). Qt-free (#1).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Callable, Literal, Sequence

from spica.anime.models import AnimeResource, EpisodeRef, MatchResult
from spica.anime.resolver import canonical_episode_key, resolve
from spica.ports.anime_source import AnimeSourceError, AnimeSourcePort

MATCHED = "matched"
AMBIGUOUS = "ambiguous"
NEED_EPISODE = "need_episode"
NOT_FOUND = "not_found"
SOURCE_ERROR = "source_error"
RESOLVE_TIMEOUT = "resolve_timeout"
CANCELLED = "cancelled"

CoordOutcome = Literal[
    "matched", "ambiguous", "need_episode", "not_found",
    "source_error", "resolve_timeout", "cancelled",
]


@dataclass(frozen=True)
class SourceError:
    source: str
    code: str


@dataclass(frozen=True)
class CoordinatorResult:
    outcome: CoordOutcome
    match: MatchResult | None = None
    resource: AnimeResource | None = None    # set only on MATCHED (finding #3)
    source: str = ""                         # which source produced the match
    reason: str = ""
    errors: tuple[SourceError, ...] = ()     # per-source error trail (finding #8)


def resolve_episode(
    ref: EpisodeRef,
    sources: Sequence[AnimeSourcePort],
    *,
    quality: str = "1080p",
    subtitle_pref: list[str] | None = None,
    budget_seconds: float | None = None,
    per_source_timeout: float | None = None,
    clock: Callable[[], float] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> CoordinatorResult:
    clock = clock or time.monotonic
    start = clock()
    errors: list[SourceError] = []
    any_reachable = False
    remembered: tuple[str, MatchResult] | None = None
    # matched somewhere but the resource couldn't be fetched -> a source-side
    # failure, NOT "not found" (must not degrade to NOT_FOUND, review tail #2).
    matched_but_unmaterialized = False

    def over_budget() -> bool:
        return budget_seconds is not None and (clock() - start) > budget_seconds

    def done(outcome, **kw):
        return CoordinatorResult(outcome, errors=tuple(errors), **kw)

    def _abort() -> CoordinatorResult | None:
        """Budget/cancel are hard ceilings, checked at EVERY step (review tail
        #1). Cancel wins over budget when both trip."""
        if cancelled is not None and cancelled():
            return done(CANCELLED, reason="cancelled")
        if over_budget():
            return done(RESOLVE_TIMEOUT, reason="resolve budget exceeded")
        return None

    for src in sources:
        if aborted := _abort():                       # before search
            return aborted

        t0 = clock()
        # in-search deadline (F6): hand the source its remaining budget, capped
        # by the per-source timeout, so the ADAPTER can stop between HTTP calls.
        # The outer post-hoc checks below keep their exact semantics.
        remaining = (None if budget_seconds is None
                     else max(0.0, budget_seconds - (t0 - start)))
        if per_source_timeout is not None and remaining is not None:
            deadline = min(per_source_timeout, remaining)
        elif per_source_timeout is not None:
            deadline = per_source_timeout
        else:
            deadline = remaining
        try:
            candidates = src.search(ref.title_query, deadline=deadline)
        except AnimeSourceError as e:
            errors.append(SourceError(src.name, e.code))
            continue

        if aborted := _abort():                       # after search, before resolve
            return aborted
        # per-source timeout: expressed via the injected clock (the real
        # cancellation lives in the adapter). A too-slow source is not relied on.
        if per_source_timeout is not None and (clock() - t0) > per_source_timeout:
            errors.append(SourceError(src.name, "TIMEOUT"))
            continue

        any_reachable = True
        if not candidates:
            continue

        res = resolve(ref, candidates, quality=quality, subtitle_pref=subtitle_pref)
        if res.status == "matched" and res.chosen is not None:
            if aborted := _abort():                   # before materialize
                return aborted
            try:
                resource = src.materialize(res.chosen)
            except AnimeSourceError as e:
                errors.append(SourceError(src.name, e.code))
                matched_but_unmaterialized = True
                continue  # matched but couldn't fetch -> maybe another source can
            if aborted := _abort():                   # after materialize, before MATCHED
                return aborted
            # single key generation point (F2): the adapter returns a placeholder
            # key; the canonical key derives from the QUERY title (alias-folded),
            # season from the ref (fallback: the chosen candidate's season), and
            # the CONCRETE episode (a LATEST ref has resolved to one by now).
            key_season = (ref.season if ref.season is not None
                          else res.chosen.parsed.season)
            resource = replace(resource, episode_key=canonical_episode_key(
                ref.title_query, key_season, res.chosen.parsed.episode))
            return done(MATCHED, match=res, resource=resource,
                        source=src.name, reason=res.reason)
        if res.status in (AMBIGUOUS, NEED_EPISODE) and remembered is None:
            remembered = (src.name, res)

    if remembered is not None:
        name, res = remembered
        return done(res.status, match=res, source=name, reason=res.reason)
    if aborted := _abort():
        return aborted
    if matched_but_unmaterialized:
        return done(SOURCE_ERROR, reason="matched but materialize failed")
    if not any_reachable:
        return done(SOURCE_ERROR, reason="all sources unreachable")
    return done(NOT_FOUND, reason="reachable but no match")
