"""Phase 2: Bilibili space adapter -- fully mocked HTTP, no network."""

from __future__ import annotations

import pytest

from spica.adapters.anime_source.bilibili_space import (
    BilibiliSpaceSource,
    _episode_for_part,
    _mixin_key,
    _sign,
)
from spica.anime.models import AnimeCandidate, episode_key
from spica.anime.resolver import (
    parse_query,
    parse_source_title,
    part_source_title,
    resolve,
)
from spica.ports.anime_source import AnimeSourceError

MUSHOKU = "【4K超清】无职转生 第三季 01-02话（每周更新）"
SLIME = "【4K超清】关于我转生变成史莱姆这档事 第四季 第13话"
SPECIAL = "【4K超清】无职转生 第三季 总集篇（每周更新）"
PV = "【4K超清】无职转生 第三季 PV/先导预告（每周更新）"


class FakeResp:
    def __init__(self, json_data=None, status_code=200, text=""):
        self._json = json_data
        self.status_code = status_code
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeCookies:
    def __init__(self):
        self.jar: dict[str, str] = {}

    def set(self, k, v, **kw):
        self.jar[k] = v


class FakeSession:
    """Routes bilibili endpoints; ``arc`` / ``view`` are per-test callables."""

    def __init__(self, arc, view=None):
        self.headers: dict[str, str] = {}
        self.cookies = FakeCookies()
        self.calls: list[tuple[str, dict | None]] = []
        self._arc = arc
        self._view = view or (lambda params: FakeResp({"code": 0, "data": {"pages": [
            {"page": 1, "part": "01"}, {"page": 2, "part": "02"}]}}))

    def get(self, url, params=None, timeout=None, **kw):
        self.calls.append((url, params))
        if "finger/spi" in url:
            return FakeResp({"data": {"b_3": "BUVID3", "b_4": "BUVID4"}})
        if url.endswith("/nav"):
            return FakeResp({"data": {"wbi_img": {   # real keys are 32-hex
                "img_url": "https://i0.hdslb.com/bfs/wbi/" + "7cd08494" * 4 + ".png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "4932caff" * 4 + ".png"}}})
        if "arc/search" in url:
            return self._arc(params)
        if "web-interface/view" in url:
            return self._view(params)
        raise AssertionError(f"unrouted URL: {url}")

    def post(self, *a, **k):
        raise AssertionError("bilibili adapter must not POST")

    def n_calls(self, needle):
        return sum(1 for u, _ in self.calls if needle in u)


def _nosleep(_s):
    """Injected for every search-exercising test: the adapter now throttles
    (F9) and real sleeps would slow the suite."""


def _vlist(*videos):
    return FakeResp({"code": 0, "data": {"list": {"vlist": list(videos)}}})


def _one_page_arc(video, mid="3493112693394137"):
    def arc(params):
        if str(params.get("mid")) != mid:
            return _vlist()          # other space: empty
        return _vlist(video) if params.get("pn") == 1 else _vlist()
    return arc


# -- pure helpers ------------------------------------------------------------

def test_sign_produces_wrid_and_wts():
    mkey = _mixin_key("7cd08494" * 4, "4932caff" * 4)   # 32-hex keys
    signed = _sign({"mid": "1", "pn": 1}, mkey, now=123)
    assert signed["wts"] == 123
    assert len(signed["w_rid"]) == 32          # md5 hex


def test_episode_mapping_fallback_order():
    # part-title episode wins
    assert _episode_for_part(MUSHOKU, "02", 2, None, 1, multi_part=True) == 2
    # no part number -> collection single episode
    assert _episode_for_part(SLIME, "正片", 1, 13, None, multi_part=False) == 13
    # no single -> range start + page offset
    assert _episode_for_part(MUSHOKU, "", 2, None, 1, multi_part=True) == 2
    # nothing, real multi-part collection -> page index (last resort)
    assert _episode_for_part("某视频", "", 3, None, None, multi_part=True) == 3
    # nothing, single-part video -> None: dropped, never fabricated ep1 (F1)
    assert _episode_for_part("某视频", "", 1, None, None, multi_part=False) is None


# -- search / expansion ------------------------------------------------------

def test_collection_expands_to_per_part_episodes():
    sess = FakeSession(_one_page_arc(
        {"bvid": "BV1fmMP6NEvw", "title": MUSHOKU}))
    src = BilibiliSpaceSource(["3493112693394137"], session=sess, max_pages=2,
                              sleep=_nosleep)
    cands = src.search("无职转生")
    by_ep = {c.parsed.episode: c.locator for c in cands}
    assert by_ep == {1: "BV1fmMP6NEvw:1", 2: "BV1fmMP6NEvw:2"}
    assert all(c.parsed.season == 3 for c in cands)


def test_single_episode_video_not_mapped_to_ep1():
    # 第13话 single video with a generic part title must stay ep13, not ep1
    sess = FakeSession(
        _one_page_arc({"bvid": "BVslime", "title": SLIME}),
        view=lambda params: FakeResp({"code": 0, "data": {
            "pages": [{"page": 1, "part": "正片"}]}}))
    src = BilibiliSpaceSource(["3493112693394137"], session=sess, max_pages=2,
                              sleep=_nosleep)
    cands = src.search("关于我转生变成史莱姆这档事")
    assert len(cands) == 1
    assert cands[0].parsed.episode == 13
    assert cands[0].parsed.season == 4


def test_risk_control_retry_then_success():
    state = {"n": 0}

    def arc(params):
        state["n"] += 1
        if state["n"] == 1:
            return FakeResp({"code": -352, "message": "风控校验失败"})
        return _vlist({"bvid": "BV1fmMP6NEvw", "title": MUSHOKU}) \
            if params.get("pn") == 1 else _vlist()
    sess = FakeSession(arc)
    src = BilibiliSpaceSource(["3493112693394137"], session=sess, max_pages=2,
                              sleep=_nosleep)
    cands = src.search("无职转生")
    assert len(cands) == 2                       # recovered after retry
    assert sess.n_calls("arc/search") >= 2       # retried
    assert sess.n_calls("finger/spi") >= 2       # re-seeded buvid on retry


def test_cookie_injected_not_from_env():
    sess = FakeSession(_one_page_arc({"bvid": "BV1", "title": MUSHOKU}))
    BilibiliSpaceSource(["3493112693394137"],
                        cookie="SESSDATA=abc; bili_jct=xyz", session=sess)
    # cookies must land in the JAR so buvid re-seeds coexist (F5) -- a manual
    # Cookie header would make http.cookiejar skip injection entirely.
    assert sess.cookies.jar == {"SESSDATA": "abc", "bili_jct": "xyz"}
    assert "Cookie" not in sess.headers


def test_cookie_and_buvid_both_sent_on_prepared_request():
    # F5 repro on a REAL requests session (offline: prepare only, nothing sent):
    # the configured cookie AND the seeded buvid3 fingerprint must BOTH reach
    # the outgoing Cookie header.
    requests = pytest.importorskip("requests")
    src = BilibiliSpaceSource(["1"], cookie="SESSDATA=abc; bili_jct=xyz")
    # what _seed_buvid does with the finger/spi response
    src._http.cookies.set("buvid3", "B3VALUE", domain=".bilibili.com")
    prep = src._http.prepare_request(requests.Request(
        "GET", "https://api.bilibili.com/x/space/wbi/arc/search"))
    header = prep.headers.get("Cookie") or ""
    assert "SESSDATA=abc" in header
    assert "bili_jct=xyz" in header
    assert "buvid3=B3VALUE" in header


def test_space_failure_continues_to_next():
    def arc(params):
        if str(params.get("mid")) == "111":
            return FakeResp({"code": -352})       # first space: always risk-control
        return _vlist({"bvid": "BV1fmMP6NEvw", "title": MUSHOKU}) \
            if params.get("pn") == 1 else _vlist()
    sess = FakeSession(arc)
    src = BilibiliSpaceSource(["111", "3493112693394137"], session=sess,
                              max_pages=2, sleep=_nosleep)
    cands = src.search("无职转生")
    assert len(cands) == 2                        # from the second space
    assert all(c.locator.startswith("BV1fmMP6NEvw") for c in cands)


def test_all_spaces_fail_raises_risk_control():
    sess = FakeSession(lambda params: FakeResp({"code": -352}))
    src = BilibiliSpaceSource(["111"], session=sess, max_pages=2, sleep=_nosleep)
    with pytest.raises(AnimeSourceError) as ei:
        src.search("无职转生")
    assert ei.value.code == "RISK_CONTROL"


def test_materialize_keeps_bvid_part():
    src = BilibiliSpaceSource(["1"], session=FakeSession(lambda p: _vlist()))
    cand = AnimeCandidate(
        source="bilibili", locator="BV1fmMP6NEvw:2",
        parsed=part_source_title(MUSHOKU, episode=2, season=3),
        display_title=MUSHOKU)
    res = src.materialize(cand)
    assert res.locator == "BV1fmMP6NEvw:2"
    assert res.source == "bilibili"


# -- F9: throttling -- retry backoff + inter-page sleep (injectable) ----------

def test_retry_backoff_sleeps_between_retries():
    naps: list[float] = []
    state = {"n": 0}

    def arc(params):
        state["n"] += 1
        if state["n"] == 1:
            return FakeResp({"code": -352})            # first attempt: risk control
        return _vlist({"bvid": "BV1fmMP6NEvw", "title": MUSHOKU})

    sess = FakeSession(arc)
    src = BilibiliSpaceSource(["3493112693394137"], session=sess, max_pages=1,
                              sleep=naps.append)
    src.search("无职转生")
    assert len(naps) == 1                              # one retry -> one backoff
    assert 0.5 <= naps[0] <= 1.5                       # random backoff range (§5.2)


def test_inter_page_throttle_sleeps_between_pages():
    naps: list[float] = []
    # page 1 has NO matching video, so pagination advances to page 2 (where the
    # inter-page throttle fires). An early match now stops paging -- see
    # test_early_match_returns_before_pagination_burns_deadline -- so this test
    # deliberately withholds a page-1 match to exercise the throttle.
    def arc(params):
        if params.get("pn") == 1:
            return _vlist({"bvid": "BVx1", "title": "【游戏实况】某主播录像"})
        return _vlist()                               # page 2 empty -> ends
    sess = FakeSession(arc)
    src = BilibiliSpaceSource(["3493112693394137"], session=sess, max_pages=2,
                              sleep=naps.append)
    src.search("无职转生")
    assert naps == [0.5]                               # exactly one pn=2 pre-sleep


# -- F6: in-search deadline -- checked before EVERY HTTP request --------------

def test_deadline_exhausted_stops_before_arc_search():
    clock = {"now": 0.0}

    class SlowSession(FakeSession):
        def get(self, url, params=None, timeout=None, **kw):
            clock["now"] += 5.0                  # each request burns 5s
            return super().get(url, params=params, timeout=timeout, **kw)

    sess = SlowSession(_one_page_arc({"bvid": "BV1", "title": MUSHOKU}))
    src = BilibiliSpaceSource(["3493112693394137"], session=sess, max_pages=2,
                              sleep=_nosleep, clock=lambda: clock["now"])
    with pytest.raises(AnimeSourceError) as ei:
        src.search("无职转生", deadline=8.0)     # finger(5s)+nav(5s) burn it
    assert ei.value.code == "TIMEOUT"
    assert sess.n_calls("arc/search") == 0       # the arc API was never hit


# -- F12: year/season spans must not feed the range-start episode mapping -----

def test_range_start_ignores_year_and_season_spans():
    from spica.adapters.anime_source.bilibili_space import _range_start
    assert _range_start("【4K超清】某番 2024-25 秋季 01-02话（每周更新）") == 1
    assert _range_start("【4K超清】某番 24-25赛季 01-02话") == 1


# -- F11: mid-pagination failure keeps already-fetched pages ------------------

def test_partial_pages_kept_when_later_page_fails():
    def arc(params):
        if params.get("pn") == 1:
            return _vlist({"bvid": "BV1fmMP6NEvw", "title": MUSHOKU})
        return FakeResp({"code": -352})       # page 2: persistent risk control

    sess = FakeSession(arc)
    src = BilibiliSpaceSource(["3493112693394137"], session=sess, max_pages=3,
                              max_retries=2, sleep=_nosleep)
    cands = src.search("无职转生")            # must NOT raise: page 1 survived
    assert {c.parsed.episode for c in cands} == {1, 2}


# -- F1: specials / single-part fallback must not fabricate episode 1 ---------

def _multi_video_arc(videos, mid="3493112693394137"):
    def arc(params):
        if str(params.get("mid")) != mid:
            return _vlist()
        return _vlist(*videos) if params.get("pn") == 1 else _vlist()
    return arc


def _view_by_bvid(pages_by_bvid):
    def view(params):
        return FakeResp({"code": 0,
                         "data": {"pages": pages_by_bvid[params["bvid"]]}})
    return view


def test_special_video_skipped_collection_wins():
    # a coexisting 总集篇 single-part video must never shadow the real ep1 (F1)
    sess = FakeSession(
        _multi_video_arc([{"bvid": "BVcoll", "title": MUSHOKU},
                          {"bvid": "BVspec", "title": SPECIAL}]),
        view=_view_by_bvid({
            "BVcoll": [{"page": 1, "part": "01"}, {"page": 2, "part": "02"}],
            "BVspec": [{"page": 1, "part": "正片"}]}))
    src = BilibiliSpaceSource(["3493112693394137"], session=sess, max_pages=2,
                              sleep=_nosleep)
    cands = src.search("无职转生")
    r = resolve(parse_query("无职转生第三季第一集"), cands)
    assert r.status == "matched"
    assert r.chosen.locator == "BVcoll:1"


def test_special_alone_never_matches_episode_request():
    sess = FakeSession(
        _multi_video_arc([{"bvid": "BVspec", "title": SPECIAL}]),
        view=_view_by_bvid({"BVspec": [{"page": 1, "part": "正片"}]}))
    src = BilibiliSpaceSource(["3493112693394137"], session=sess, max_pages=2,
                              sleep=_nosleep)
    cands = src.search("无职转生")
    assert cands == []                        # special video is never expanded
    r = resolve(parse_query("无职转生第三季第一集"), cands)
    assert r.status != "matched"


def test_pv_single_part_without_episode_dropped():
    # PV isn't caught by the special wordlist -> the single-part
    # no-episode-evidence rule must drop it instead of fabricating ep1 (F1)
    sess = FakeSession(
        _multi_video_arc([{"bvid": "BVpv", "title": PV}]),
        view=_view_by_bvid({"BVpv": [{"page": 1, "part": "正片"}]}))
    src = BilibiliSpaceSource(["3493112693394137"], session=sess, max_pages=2,
                              sleep=_nosleep)
    cands = src.search("无职转生")
    assert cands == []
    r = resolve(parse_query("无职转生第三季第一集"), cands)
    assert r.status != "matched"


# -- F2: single key generation point (coordinator), stable across all paths ---

_LOLI_TITLE = ("[LoliHouse] 无职转生 3期 / Mushoku Tensei S3 - 01 "
               "[WebRip 1080p HEVC-10bit AAC][简繁内封字幕]")
# real Phase-0 sample (docs/anime_watch/probes/sample_mikan.json): the FULL name
# the old cross-source test never exercised -- this is the F2 repro.
_SKY_TITLE = ("[Skymoon-Raws] 无职转生，到了异世界就拿出真本事 第3季 / "
              "Mushoku Tensei 3rd Season - 01 [ViuTV][WEB-DL][CHT][1080p][AVC AAC]")
_IH = "fe2aafd45d8b9e077b22968a8c65b91d4a25cadf"


def _mikan_source(item_title):
    from spica.adapters.anime_source.mikan import MikanRssSource

    rss = f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>Mikan</title>
<item>
  <title>{item_title}</title>
  <link>https://mikanani.me/Home/Episode/{_IH}</link>
  <enclosure type="application/x-bittorrent" length="1"
    url="https://mikanani.me/Download/x/{_IH}.torrent" />
</item>
</channel></rss>"""

    class _Sess:
        def get(self, url, timeout=None, **kw):
            import types
            return types.SimpleNamespace(text=rss, status_code=200)

    return MikanRssSource(["https://mikanani.me"], session=_Sess())


def _bilibili_source():
    sess = FakeSession(_one_page_arc({"bvid": "BV1fmMP6NEvw", "title": MUSHOKU}))
    return BilibiliSpaceSource(["3493112693394137"], session=sess, max_pages=2,
                              sleep=_nosleep)


def test_episode_key_same_across_query_and_all_source_paths():
    from spica.anime.coordinator import resolve_episode

    ref = parse_query("无职转生第三季第一集")
    # (1) the query fast path (what watch_flow uses for the library dedup)
    k_query = episode_key(ref.title_query, ref.season, ref.episode)
    # (2) mikan, LoliHouse short name  (3) mikan, Skymoon FULL name
    k_loli = resolve_episode(ref, [_mikan_source(_LOLI_TITLE)]).resource.episode_key
    k_sky = resolve_episode(ref, [_mikan_source(_SKY_TITLE)]).resource.episode_key
    # (4) bilibili collection expansion
    k_bili = resolve_episode(ref, [_bilibili_source()]).resource.episode_key
    assert k_query == k_loli == k_sky == k_bili == "无职转生|s3|e1"


def test_adapter_materialize_returns_placeholder_key():
    # adapters no longer invent dedup keys -- the coordinator overwrites (F2)
    src = BilibiliSpaceSource(["1"], session=FakeSession(lambda p: _vlist()))
    bili = src.materialize(AnimeCandidate(
        source="bilibili", locator="BV1fmMP6NEvw:1",
        parsed=part_source_title(MUSHOKU, episode=1, season=3),
        display_title=MUSHOKU))
    assert bili.episode_key == ""
    mikan = _mikan_source(_LOLI_TITLE)
    cand = mikan.search("无职转生")[0]
    assert mikan.materialize(cand).episode_key == ""


def test_expansion_still_produces_ep1_ep2_after_name_fix():
    # the name-fragment fix must not disturb the per-part expansion
    sess = FakeSession(_one_page_arc({"bvid": "BV1fmMP6NEvw", "title": MUSHOKU}))
    src = BilibiliSpaceSource(["3493112693394137"], session=sess, max_pages=2,
                              sleep=_nosleep)
    cands = src.search("无职转生")
    assert {c.parsed.episode: c.locator for c in cands} == {
        1: "BV1fmMP6NEvw:1", 2: "BV1fmMP6NEvw:2"}


# -- deadline-burn regression (fallback bug): an early name-matched page must be
#    expanded + returned BEFORE later-page pagination exhausts the per-source
#    deadline. Old code collected ALL pages first, then expanded -- so a hit on
#    an early page was lost when later paging burned the budget and the
#    subsequent _pagelist() had none left, and the coordinator fell to mikan. ----

_UNRELATED = "【游戏实况】某主播的直播录像 合集"


def test_early_match_returns_before_pagination_burns_deadline():
    clock = {"now": 0.0}

    class SlowSession(FakeSession):
        def get(self, url, params=None, timeout=None, **kw):
            clock["now"] += 2.0            # every request burns 2s of the budget
            return super().get(url, params=params, timeout=timeout, **kw)

    def arc(params):
        # page 2 carries the real 无职转生 collection; every OTHER page returns a
        # non-matching video so a full-scan keeps paginating until the deadline
        # is gone, then its later _pagelist() finds no budget left.
        return (_vlist({"bvid": "BV1fmMP6NEvw", "title": MUSHOKU})
                if params.get("pn") == 2
                else _vlist({"bvid": f"BVunrel{params.get('pn')}",
                             "title": _UNRELATED}))

    sess = SlowSession(arc)
    src = BilibiliSpaceSource(["3493112693394137"], session=sess, max_pages=10,
                              sleep=_nosleep, clock=lambda: clock["now"])
    cands = src.search("无职转生", deadline=15.0)
    by_ep = {c.parsed.episode: c.locator for c in cands}
    assert by_ep.get(1) == "BV1fmMP6NEvw:1"      # the hit is returned, not lost
    assert by_ep.get(2) == "BV1fmMP6NEvw:2"
    r = resolve(parse_query("无职转生第三季第一集"), cands)
    assert r.status == "matched" and r.chosen.locator == "BV1fmMP6NEvw:1"
    # stopped paginating right after the match (p1 miss + p2 hit), not all 10
    assert sess.n_calls("arc/search") == 2


def test_no_match_keeps_scanning_pages_then_returns_empty():
    # early-stop must NOT trigger without a candidate: pagination proceeds
    # normally and the source yields nothing (coordinator then falls back).
    def arc(params):
        pn = params.get("pn")
        if pn in (1, 2):
            return _vlist({"bvid": f"BVx{pn}", "title": _UNRELATED})
        return _vlist()                    # page 3 empty -> pagination ends
    sess = FakeSession(arc)
    src = BilibiliSpaceSource(["3493112693394137"], session=sess, max_pages=5,
                              sleep=_nosleep)
    assert src.search("无职转生") == []
    assert sess.n_calls("arc/search") == 3          # scanned p1,p2, empty p3


def test_first_page_failure_raises_not_silent_empty():
    # nothing fetched -> the space really failed -> raise (never a silent empty
    # that a caller could mistake for "reachable but no such anime").
    sess = FakeSession(lambda params: FakeResp({"code": -352}))
    src = BilibiliSpaceSource(["3493112693394137"], session=sess, max_pages=3,
                              max_retries=2, sleep=_nosleep)
    with pytest.raises(AnimeSourceError) as ei:
        src.search("无职转生")
    assert ei.value.code == "RISK_CONTROL"


def test_later_page_failure_after_no_match_returns_empty_not_raise():
    # F11 under early-stop: a page>=2 failure (page 1 fetched but matched
    # nothing) must NOT raise -- return empty so the coordinator can fall back.
    def arc(params):
        if params.get("pn") == 1:
            return _vlist({"bvid": "BVx1", "title": _UNRELATED})
        return FakeResp({"code": -352})    # page 2: persistent risk control
    sess = FakeSession(arc)
    src = BilibiliSpaceSource(["3493112693394137"], session=sess, max_pages=3,
                              max_retries=2, sleep=_nosleep)
    assert src.search("无职转生") == []             # graceful, no raise
