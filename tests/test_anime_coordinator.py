"""Phase 1 tests: source orchestration (main->fallback, error mapping, P1-10)."""

from __future__ import annotations

from spica.anime.coordinator import (
    CANCELLED,
    MATCHED,
    NOT_FOUND,
    RESOLVE_TIMEOUT,
    SOURCE_ERROR,
    resolve_episode,
)
from spica.anime.models import AnimeCandidate, AnimeResource
from spica.anime.resolver import parse_query, parse_source_title
from spica.ports.anime_source import AnimeSourceError


class Clock:
    """Deterministic monotonic clock (seconds); sources advance it in search()."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeSource:
    def __init__(self, name, titles=None, error=None, elapsed=0.0,
                 materialize_error=None, materialize_elapsed=0.0, clock=None,
                 on_search=None):
        self.name = name
        self._titles = titles or []
        self._error = error
        self._elapsed = elapsed                      # simulated search duration
        self._materialize_error = materialize_error
        self._materialize_elapsed = materialize_elapsed
        self._clock = clock
        self._on_search = on_search                  # side-effect hook after search

    def search(self, title_query, *, deadline=None):
        self.seen_deadline = deadline            # recorded for the F6 tests
        if self._clock is not None:
            self._clock.now += self._elapsed
        if self._error is not None:
            raise self._error
        out = [
            AnimeCandidate(source=self.name, locator=f"{self.name}:loc",
                           parsed=parse_source_title(t), display_title=t)
            for t in self._titles
        ]
        if self._on_search is not None:
            self._on_search()
        return out

    def materialize(self, candidate):
        if self._clock is not None:
            self._clock.now += self._materialize_elapsed
        if self._materialize_error is not None:
            raise self._materialize_error
        return AnimeResource(episode_key="k", source=self.name,
                             locator=candidate.locator,
                             display_title=candidate.display_title)


LOLI_S3E1 = "[LoliHouse] 无职转生 3期 / Mushoku Tensei S3 - 01 [1080p][简繁内封]"
ANI_S3E1 = "[ANi] 无职转生 第三季 - 01 [1080P][Baha][CHT]"


def test_main_source_match_wins():
    main = FakeSource("bilibili", [LOLI_S3E1])
    fb = FakeSource("mikan", [ANI_S3E1])
    r = resolve_episode(parse_query("无职转生第三季第一集"), [main, fb])
    assert r.outcome == MATCHED
    assert r.source == "bilibili"


def test_falls_back_when_main_has_no_match():
    main = FakeSource("bilibili", ["[X] 间谍过家家 - 01 [1080p]"])  # wrong anime
    fb = FakeSource("mikan", [LOLI_S3E1])
    r = resolve_episode(parse_query("无职转生第三季第一集"), [main, fb])
    assert r.outcome == MATCHED
    assert r.source == "mikan"


def test_falls_back_when_main_errors():
    main = FakeSource("bilibili", error=AnimeSourceError("RISK_CONTROL"))
    fb = FakeSource("mikan", [LOLI_S3E1])
    r = resolve_episode(parse_query("无职转生第三季第一集"), [main, fb])
    assert r.outcome == MATCHED
    assert r.source == "mikan"


def test_all_sources_error_is_source_error():
    # network down everywhere -> SOURCE_ERROR, NOT not_found (P1-10)
    main = FakeSource("bilibili", error=AnimeSourceError("NET"))
    fb = FakeSource("mikan", error=AnimeSourceError("NET"))
    r = resolve_episode(parse_query("无职转生第三季第一集"), [main, fb])
    assert r.outcome == SOURCE_ERROR


def test_reachable_but_empty_is_not_found():
    main = FakeSource("bilibili", [])
    fb = FakeSource("mikan", [])
    r = resolve_episode(parse_query("无职转生第三季第一集"), [main, fb])
    assert r.outcome == NOT_FOUND


def test_ambiguous_remembered_across_sources():
    # no clean match anywhere, but a season-ambiguous hit -> surface it to ask
    main = FakeSource("bilibili", [
        "[LoliHouse] 无职转生 / Mushoku Tensei - 01 [1080p][简繁]",
        "[LoliHouse] 无职转生 3期 - 01 [1080p][简繁]",
    ])
    r = resolve_episode(parse_query("无职转生第一集"), [main])
    assert r.outcome == "ambiguous"
    assert len(r.match.candidates) == 2


# -- materialize / resource (finding #3) -------------------------------------

def test_matched_returns_materialized_resource():
    main = FakeSource("bilibili", [LOLI_S3E1])
    r = resolve_episode(parse_query("无职转生第三季第一集"), [main])
    assert r.outcome == MATCHED
    assert r.resource is not None
    assert r.resource.locator == "bilibili:loc"


def test_materialize_error_falls_back_to_next_source():
    main = FakeSource("bilibili", [LOLI_S3E1],
                      materialize_error=AnimeSourceError("GONE"))
    fb = FakeSource("mikan", [LOLI_S3E1])
    r = resolve_episode(parse_query("无职转生第三季第一集"), [main, fb])
    assert r.outcome == MATCHED
    assert r.source == "mikan"
    # the failed main materialize is kept in the trail (finding #8)
    assert any(e.source == "bilibili" and e.code == "GONE" for e in r.errors)


# -- error trail (finding #8) ------------------------------------------------

def test_error_trail_preserves_codes():
    main = FakeSource("bilibili", error=AnimeSourceError("RISK_CONTROL"))
    fb = FakeSource("mikan", error=AnimeSourceError("HTTP_503"))
    r = resolve_episode(parse_query("无职转生第三季第一集"), [main, fb])
    assert r.outcome == SOURCE_ERROR
    assert [(e.source, e.code) for e in r.errors] == [
        ("bilibili", "RISK_CONTROL"), ("mikan", "HTTP_503")]


# -- budget / per-source timeout / cancellation (finding #2) -----------------

def test_resolve_timeout_when_budget_exceeded():
    clock = Clock()
    # a slow main source burns the whole budget before the fallback is tried
    main = FakeSource("bilibili", ["[X] 间谍过家家 - 01 [1080p]"], elapsed=50, clock=clock)
    fb = FakeSource("mikan", [LOLI_S3E1], elapsed=50, clock=clock)
    r = resolve_episode(parse_query("无职转生第三季第一集"), [main, fb],
                        budget_seconds=40, clock=clock)
    assert r.outcome == RESOLVE_TIMEOUT


def test_per_source_timeout_skips_slow_source():
    clock = Clock()
    slow = FakeSource("bilibili", [LOLI_S3E1], elapsed=30, clock=clock)
    fast = FakeSource("mikan", [LOLI_S3E1], elapsed=1, clock=clock)
    r = resolve_episode(parse_query("无职转生第三季第一集"), [slow, fast],
                        per_source_timeout=10, clock=clock)
    assert r.outcome == MATCHED
    assert r.source == "mikan"
    assert any(e.source == "bilibili" and e.code == "TIMEOUT" for e in r.errors)


def test_deadline_passed_to_sources():
    # F6: each source receives the REMAINING budget capped by per_source_timeout
    clock = Clock()
    main = FakeSource("bilibili", ["[X] 间谍过家家 - 01 [1080p]"],  # no match
                      elapsed=8, clock=clock)
    fb = FakeSource("mikan", [LOLI_S3E1], elapsed=1, clock=clock)
    r = resolve_episode(parse_query("无职转生第三季第一集"), [main, fb],
                        budget_seconds=15, per_source_timeout=10, clock=clock)
    assert r.outcome == MATCHED
    assert main.seen_deadline == 10          # min(per_source 10, remaining 15)
    assert fb.seen_deadline == 7             # min(per_source 10, remaining 15-8)


def test_deadline_none_when_no_budget_knobs():
    main = FakeSource("bilibili", [LOLI_S3E1])
    r = resolve_episode(parse_query("无职转生第三季第一集"), [main])
    assert r.outcome == MATCHED
    assert main.seen_deadline is None


def test_cancelled_short_circuits():
    main = FakeSource("bilibili", [LOLI_S3E1])
    r = resolve_episode(parse_query("无职转生第三季第一集"), [main],
                        cancelled=lambda: True)
    assert r.outcome == CANCELLED


def test_matchable_but_over_budget_is_timeout_not_matched():
    # review tail #1: a source that RETURNS a matchable candidate but has already
    # blown the total budget must yield RESOLVE_TIMEOUT, never MATCHED.
    clock = Clock()
    src = FakeSource("bilibili", [LOLI_S3E1], elapsed=50, clock=clock)
    r = resolve_episode(parse_query("无职转生第三季第一集"), [src],
                        budget_seconds=40, clock=clock)
    assert r.outcome == RESOLVE_TIMEOUT
    assert r.resource is None


def test_over_budget_during_materialize_is_timeout():
    # the AFTER-materialize checkpoint: even with the resource already fetched,
    # if materialize blew the budget we return RESOLVE_TIMEOUT (hard ceiling).
    clock = Clock()
    src = FakeSource("bilibili", [LOLI_S3E1], materialize_elapsed=100, clock=clock)
    r = resolve_episode(parse_query("无职转生第三季第一集"), [src],
                        budget_seconds=40, clock=clock)
    assert r.outcome == RESOLVE_TIMEOUT


def test_cancel_after_search_before_match():
    # the AFTER-search checkpoint: search of a matchable source ran, then cancel
    # trips -> CANCELLED (not MATCHED).
    searched = {"done": False}
    src = FakeSource("bilibili", [LOLI_S3E1],
                     on_search=lambda: searched.__setitem__("done", True))
    r = resolve_episode(parse_query("无职转生第三季第一集"), [src],
                        cancelled=lambda: searched["done"])
    assert r.outcome == CANCELLED


def test_single_source_materialize_fail_is_source_error():
    # review tail #2: matched but materialize fails with no fallback -> SOURCE_ERROR
    # (NOT NOT_FOUND), reason mentions materialize, trail keeps the code.
    src = FakeSource("bilibili", [LOLI_S3E1],
                     materialize_error=AnimeSourceError("MAGNET_GONE"))
    r = resolve_episode(parse_query("无职转生第三季第一集"), [src])
    assert r.outcome == SOURCE_ERROR
    assert "materialize" in r.reason
    assert any(e.source == "bilibili" and e.code == "MAGNET_GONE" for e in r.errors)
