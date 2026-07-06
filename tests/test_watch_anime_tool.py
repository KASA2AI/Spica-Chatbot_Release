"""Phase 3: watch_anime tool shim + schema shape + host-closure flow branches."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_tools.function_tools.screen.schema import ScreenToolError
from spica.adapters.llm.openai_compatible import to_chat_completions_tools
from spica.adapters.tools.watch_anime import WATCH_ANIME_SCHEMA, WatchAnimeTool
from spica.anime import watch_flow
from spica.anime.coordinator import (
    AMBIGUOUS,
    CANCELLED,
    CoordinatorResult,
    MATCHED,
    NEED_EPISODE,
    NOT_FOUND,
    RESOLVE_TIMEOUT,
    SOURCE_ERROR,
)
from spica.anime.library import AnimeLibrary, LibraryEntry, episode_key
from spica.anime.models import AnimeCandidate, AnimeResource, MatchResult
from spica.anime.resolver import parse_source_title
from spica.anime.watch_flow import WatchAnimeError, merge_episode_ref, run_watch_request
from spica.ports.media_player import MediaPlayerError


# -- schema shape (review #6: test the actual tool-chain format) -------------

def test_schema_is_strict_with_nullable_episode():
    props = WATCH_ANIME_SCHEMA["parameters"]["properties"]
    assert WATCH_ANIME_SCHEMA["strict"] is True
    assert props["query"]["type"] == "string"
    assert "null" in props["episode"]["type"]          # optional -> nullable
    assert "integer" in props["episode"]["type"]
    assert "string" in props["episode"]["type"]         # "latest"
    # P1-11②: the 「放吧」flag is optional -> nullable boolean, still required
    assert "null" in props["use_recent_unplayed"]["type"]
    assert "boolean" in props["use_recent_unplayed"]["type"]
    assert set(WATCH_ANIME_SCHEMA["parameters"]["required"]) == {
        "query", "episode", "use_recent_unplayed"}
    assert WATCH_ANIME_SCHEMA["parameters"]["additionalProperties"] is False


def test_schema_converts_to_chat_completions_nested():
    nested = to_chat_completions_tools([WATCH_ANIME_SCHEMA])[0]
    assert nested["type"] == "function"
    assert nested["function"]["name"] == "watch_anime"
    assert nested["function"]["strict"] is True
    ep = nested["function"]["parameters"]["properties"]["episode"]["type"]
    assert "null" in ep and "integer" in ep and "string" in ep


# -- shim: pure forwarding + ANIME_QUERY_EMPTY -------------------------------

def test_shim_forwards_to_closure():
    seen = {}
    tool = WatchAnimeTool(
        lambda q, e, r: seen.update(query=q, episode=e, recent=r) or {"ok": 1})
    out = tool.run(query="  无职转生  ", episode=1)
    assert seen == {"query": "无职转生", "episode": 1, "recent": False}
    assert out == {"ok": 1}


def test_shim_empty_query_raises_query_empty():
    # the plain-path contract is UNCHANGED by P1-11②: no flag -> empty query errors
    tool = WatchAnimeTool(lambda q, e, r: {"never": True})
    with pytest.raises(ScreenToolError) as ei:
        tool.run(query="   ", episode=None)
    assert ei.value.code == "ANIME_QUERY_EMPTY"
    with pytest.raises(ScreenToolError):
        tool.run(query="", episode=None, use_recent_unplayed=False)


def test_shim_recent_unplayed_exempts_empty_query():
    seen = {}
    tool = WatchAnimeTool(
        lambda q, e, r: seen.update(query=q, episode=e, recent=r) or {})
    tool.run(query="", episode=None, use_recent_unplayed=True)
    assert seen == {"query": "", "episode": None, "recent": True}


def test_shim_blank_episode_string_becomes_none():
    seen = {}
    tool = WatchAnimeTool(lambda q, e, r: seen.update(episode=e) or {})
    tool.run(query="x", episode="")
    assert seen["episode"] is None


# -- merge_episode_ref -------------------------------------------------------

def test_merge_episode_override_and_latest():
    assert merge_episode_ref("无职转生第三季", 5).episode == 5
    assert merge_episode_ref("无职转生第三季", "latest").episode == "latest"
    assert merge_episode_ref("无职转生第三季第一集", None).episode == 1   # from query


# -- flow: ready gate --------------------------------------------------------

def _cfg(enabled=True):
    return SimpleNamespace(
        enabled=enabled, quality="1080p", subtitle_preference=["简繁", "简体"],
        resolve_budget_seconds=45.0, source_timeout_seconds=15.0)


def _kw(**over):
    base = dict(
        query="无职转生第三季第一集", episode=None, config=_cfg(), sources=[],
        library=AnimeLibrary(), play_file=lambda p: None, emit=lambda ev: None,
        is_ready=lambda: True, new_id=lambda: "REQ", now=lambda: "T0")
    base.update(over)
    return base


def test_flow_disabled_raises():
    with pytest.raises(WatchAnimeError) as ei:
        run_watch_request(**_kw(config=_cfg(enabled=False)))
    assert ei.value.code == "ANIME_DISABLED"


def test_flow_not_ready_raises():
    with pytest.raises(WatchAnimeError) as ei:
        run_watch_request(**_kw(is_ready=lambda: False))
    assert ei.value.code == "ANIME_NOT_READY"


# -- flow: matched -> emit + fire-and-ack (no download) ----------------------

def _resource():
    return AnimeResource(episode_key="无职转生|s3|e1", source="mikan",
                         locator="magnet:?xt=urn:btih:" + "a" * 40,
                         display_title="无职转生 S3E1", size_bytes=700)


def test_flow_matched_emits_and_acks(monkeypatch):
    monkeypatch.setattr(watch_flow, "resolve_episode",
                        lambda *a, **k: CoordinatorResult(MATCHED, resource=_resource()))
    emitted = []
    out = run_watch_request(**_kw(emit=emitted.append))
    assert len(emitted) == 1
    assert emitted[0].episode_key == "无职转生|s3|e1"
    assert emitted[0].request_id == "REQ"
    assert out["status"] == "downloading"
    assert out["episode_key"] == "无职转生|s3|e1"


@pytest.mark.parametrize("outcome,code", [
    (NEED_EPISODE, "ANIME_NEED_EPISODE"),
    (NOT_FOUND, "ANIME_NOT_FOUND"),
    (SOURCE_ERROR, "ANIME_SOURCE_ERROR"),
    (RESOLVE_TIMEOUT, "ANIME_RESOLVE_TIMEOUT"),
    (CANCELLED, "ANIME_CANCELLED"),
])
def test_flow_outcome_to_code(monkeypatch, outcome, code):
    monkeypatch.setattr(watch_flow, "resolve_episode",
                        lambda *a, **k: CoordinatorResult(outcome))
    with pytest.raises(WatchAnimeError) as ei:
        run_watch_request(**_kw())
    assert ei.value.code == code


def test_flow_ambiguous_lists_candidates(monkeypatch):
    cand = AnimeCandidate(source="mikan", locator="m",
                          parsed=parse_source_title("[X] 无职转生 - 01 [1080p]"),
                          display_title="无职转生 S1E1")
    match = MatchResult(status="ambiguous", candidates=(cand,))
    monkeypatch.setattr(watch_flow, "resolve_episode",
                        lambda *a, **k: CoordinatorResult(AMBIGUOUS, match=match))
    with pytest.raises(WatchAnimeError) as ei:
        run_watch_request(**_kw())
    assert ei.value.code == "ANIME_AMBIGUOUS"
    assert "无职转生 S1E1" in ei.value.message


# -- flow: single-flight busy gate (F8) ---------------------------------------

def test_flow_busy_when_download_in_flight(monkeypatch):
    monkeypatch.setattr(watch_flow, "resolve_episode", lambda *a, **k: pytest.fail(
        "busy must be checked before resolve"))
    with pytest.raises(WatchAnimeError) as ei:
        run_watch_request(**_kw(
            in_flight=lambda: {"progress": 0.42, "title": "无职转生 S3E1"}))
    assert ei.value.code == "ANIME_DOWNLOAD_BUSY"
    assert "42" in ei.value.message
    assert "无职转生 S3E1" in ei.value.message


def test_flow_library_hit_wins_over_busy(monkeypatch):
    # a library hit plays directly -- an in-flight download is irrelevant (the
    # busy gate sits AFTER the fast path, F8)
    monkeypatch.setattr(watch_flow, "resolve_episode",
                        lambda *a, **k: pytest.fail("must not resolve on a hit"))
    played = []
    out = run_watch_request(**_kw(
        library=_lib_with_ep1(), play_file=played.append,
        in_flight=lambda: {"progress": 0.1, "title": "别的番"}))
    assert out["status"] == "playing"
    assert played == ["/dl/ep1.mkv"]


# -- flow: library hit -> play via port --------------------------------------

def _lib_with_ep1():
    key = episode_key("无职转生", 3, 1)
    entry = LibraryEntry(episode_key=key, title="无职转生 S3E1", season=3, episode=1,
                         file_path="/dl/ep1.mkv", size_bytes=700, source="mikan")
    return AnimeLibrary([entry])


def test_flow_library_hit_plays_via_port(monkeypatch):
    monkeypatch.setattr(watch_flow, "resolve_episode",
                        lambda *a, **k: pytest.fail("must not resolve on a hit"))
    played = []
    out = run_watch_request(**_kw(library=_lib_with_ep1(), play_file=played.append))
    assert played == ["/dl/ep1.mkv"]
    assert out["status"] == "playing"


def test_flow_fast_path_key_is_canonical(monkeypatch):
    # F2: the fast-path dedup key must fold aliases/romaji like the coordinator
    # does -- a romaji query for an episode stored as 无职转生|s3|e1 must HIT.
    monkeypatch.setattr(watch_flow, "resolve_episode",
                        lambda *a, **k: pytest.fail("must not resolve on a hit"))
    played = []
    out = run_watch_request(**_kw(query="Mushoku Tensei S3E1", episode=None,
                                  library=_lib_with_ep1(),
                                  play_file=played.append))
    assert played == ["/dl/ep1.mkv"]
    assert out["status"] == "playing"


def test_flow_playback_error_maps_code():
    def boom(_p):
        raise MediaPlayerError("UNSAFE_PATH", "bad")
    with pytest.raises(WatchAnimeError) as ei:
        run_watch_request(**_kw(library=_lib_with_ep1(), play_file=boom))
    assert ei.value.code == "ANIME_PLAYBACK_ERROR"


# -- Phase 4: every play marks played (pointer consumption) --------------------

def test_flow_library_hit_marks_played():
    marked = []
    out = run_watch_request(**_kw(library=_lib_with_ep1(),
                                  mark_played=marked.append))
    assert out["status"] == "playing"
    assert marked == [episode_key("无职转生", 3, 1)]


def test_flow_playback_error_does_not_mark_played():
    def boom(_p):
        raise MediaPlayerError("UNSAFE_PATH", "bad")
    marked = []
    with pytest.raises(WatchAnimeError):
        run_watch_request(**_kw(library=_lib_with_ep1(), play_file=boom,
                                mark_played=marked.append))
    assert marked == []


# -- Phase 4: 「放吧」explicit escape (P1-11②) ---------------------------------

def test_flow_recent_unplayed_plays_and_marks():
    played, marked = [], []
    out = run_watch_request(**_kw(query="", library=_lib_with_ep1(),
                                  play_file=played.append,
                                  mark_played=marked.append,
                                  use_recent_unplayed=True))
    assert played == ["/dl/ep1.mkv"]
    assert marked == [episode_key("无职转生", 3, 1)]
    assert out["status"] == "playing"


def test_flow_recent_unplayed_nothing_pending():
    with pytest.raises(WatchAnimeError) as ei:
        run_watch_request(**_kw(query="", use_recent_unplayed=True))
    assert ei.value.code == "ANIME_NOTHING_PENDING"


def test_flow_recent_unplayed_ignores_played_entries():
    lib = _lib_with_ep1()
    lib.mark_played(episode_key("无职转生", 3, 1))
    with pytest.raises(WatchAnimeError) as ei:
        run_watch_request(**_kw(query="", library=lib, use_recent_unplayed=True))
    assert ei.value.code == "ANIME_NOTHING_PENDING"


# -- Phase 4: 「放吧」fuzzy pointer (P1-11②) -----------------------------------

def test_flow_pointer_title_only_rephrase_plays(monkeypatch):
    # 「把无职转生放了吧」: title-only query naming the fresh download plays it
    # WITHOUT resolving (and without NEED_EPISODE bouncing).
    monkeypatch.setattr(watch_flow, "resolve_episode",
                        lambda *a, **k: pytest.fail("pointer hit must not resolve"))
    played, marked = [], []
    out = run_watch_request(**_kw(query="无职转生", episode=None,
                                  library=_lib_with_ep1(),
                                  play_file=played.append,
                                  mark_played=marked.append))
    assert played == ["/dl/ep1.mkv"]
    assert marked == [episode_key("无职转生", 3, 1)]
    assert out["status"] == "playing"


def test_flow_pointer_different_title_falls_through(monkeypatch):
    # a DIFFERENT anime, episode-less: the plain ask-which-episode contract is
    # untouched by the pointer.
    monkeypatch.setattr(watch_flow, "resolve_episode",
                        lambda *a, **k: CoordinatorResult(NEED_EPISODE))
    with pytest.raises(WatchAnimeError) as ei:
        run_watch_request(**_kw(query="莉可丽丝", episode=None,
                                library=_lib_with_ep1()))
    assert ei.value.code == "ANIME_NEED_EPISODE"


def test_flow_pointer_episode_mismatch_falls_through(monkeypatch):
    # a concrete DIFFERENT episode must resolve, never replay the pointer
    monkeypatch.setattr(watch_flow, "resolve_episode",
                        lambda *a, **k: CoordinatorResult(NOT_FOUND))
    with pytest.raises(WatchAnimeError) as ei:
        run_watch_request(**_kw(query="无职转生第三季第二集",
                                library=_lib_with_ep1()))
    assert ei.value.code == "ANIME_NOT_FOUND"


def test_flow_pointer_latest_never_matches(monkeypatch):
    # 「最新一集」may be NEWER than the downloaded one -> must re-resolve
    monkeypatch.setattr(watch_flow, "resolve_episode",
                        lambda *a, **k: CoordinatorResult(NOT_FOUND))
    with pytest.raises(WatchAnimeError) as ei:
        run_watch_request(**_kw(query="无职转生", episode="latest",
                                library=_lib_with_ep1()))
    assert ei.value.code == "ANIME_NOT_FOUND"


def test_flow_pointer_consumed_after_played(monkeypatch):
    # once played, the pointer is gone: a title-only rephrase resolves again
    monkeypatch.setattr(watch_flow, "resolve_episode",
                        lambda *a, **k: CoordinatorResult(NEED_EPISODE))
    lib = _lib_with_ep1()
    lib.mark_played(episode_key("无职转生", 3, 1))
    with pytest.raises(WatchAnimeError) as ei:
        run_watch_request(**_kw(query="无职转生", episode=None, library=lib))
    assert ei.value.code == "ANIME_NEED_EPISODE"
