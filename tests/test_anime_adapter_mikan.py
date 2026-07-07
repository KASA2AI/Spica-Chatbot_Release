"""Phase 2: Mikan RSS adapter -- fully mocked HTTP, no network."""

from __future__ import annotations

import urllib.parse

import pytest

from spica.adapters.anime_source.mikan import MikanRssSource
from spica.ports.anime_source import AnimeSourceError

_IH1 = "fe2aafd45d8b9e077b22968a8c65b91d4a25cadf"
_IH_BATCH = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

RSS = f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>Mikan - 无职转生</title>
<item>
  <title>[LoliHouse] 无职转生 3期 / Mushoku Tensei S3 - 01 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]</title>
  <link>https://mikanani.me/Home/Episode/{_IH1}</link>
  <enclosure type="application/x-bittorrent" length="744918848"
    url="https://mikanani.me/Download/20260705/{_IH1}.torrent" />
</item>
<item>
  <title>[某组] 无职转生 第三季 01-12 合集 [1080p][简繁]</title>
  <link>https://mikanani.me/Home/Episode/{_IH_BATCH}</link>
  <enclosure length="1" url="https://mikanani.me/Download/x/{_IH_BATCH}.torrent" />
</item>
</channel></rss>"""

EMPTY_RSS = '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
# a mirror serving an HTML error page -> malformed XML (unclosed <br>)
BAD_XML = "<html><body>502 Bad Gateway<br></body></html>"


class FakeResp:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code


class FakeSession:
    def __init__(self, routes):
        self.routes = routes          # substr -> (text, status) | Exception
        self.calls: list[str] = []

    def get(self, url, timeout=None, **kw):
        self.calls.append(url)
        for substr, val in self.routes.items():
            if substr in url:
                if isinstance(val, Exception):
                    raise val
                text, status = val
                return FakeResp(text, status)
        raise AssertionError(f"unrouted URL: {url}")


def _src(routes, base_urls=("https://mikanani.me",)):
    sess = FakeSession(routes)
    return MikanRssSource(list(base_urls), session=sess), sess


def test_search_parses_and_builds_magnet():
    src, _ = _src({"RSS/Search": (RSS, 200)})
    cands = src.search("无职转生")
    assert len(cands) == 1                       # batch item filtered (D11)
    c = cands[0]
    assert c.locator == (f"magnet:?xt=urn:btih:{_IH1}"
                         f"&dn={urllib.parse.quote(c.display_title)}")
    assert c.size_bytes == 744918848
    assert c.parsed.season == 3 and c.parsed.episode == 1


def test_batch_item_filtered():
    src, _ = _src({"RSS/Search": (RSS, 200)})
    titles = [c.display_title for c in src.search("无职转生")]
    assert not any("合集" in t for t in titles)


def test_no_torrent_or_arbitrary_url_fetched():
    src, sess = _src({"RSS/Search": (RSS, 200)})
    src.search("无职转生")
    assert all("RSS/Search" in u for u in sess.calls)   # never the .torrent url
    assert not any(".torrent" in u for u in sess.calls)


def test_base_url_fallback():
    sess = FakeSession({"mirror.example": ConnectionError("down"),
                        "mikanani.me": (RSS, 200)})
    src = MikanRssSource(["https://mirror.example", "https://mikanani.me"],
                         session=sess)
    cands = src.search("无职转生")
    assert len(cands) == 1
    assert any("mirror.example" in u for u in sess.calls)   # tried first
    assert any("mikanani.me" in u for u in sess.calls)      # then fell back


def test_all_base_urls_fail_raises_unreachable():
    sess = FakeSession({"a.example": ConnectionError("x"),
                        "b.example": ConnectionError("y")})
    src = MikanRssSource(["https://a.example", "https://b.example"], session=sess)
    with pytest.raises(AnimeSourceError) as ei:
        src.search("无职转生")
    assert ei.value.code == "SOURCE_UNREACHABLE"


def test_non_200_falls_through_to_unreachable():
    sess = FakeSession({"mikanani.me": ("", 503)})
    src = MikanRssSource(["https://mikanani.me"], session=sess)
    with pytest.raises(AnimeSourceError) as ei:
        src.search("x")
    assert ei.value.code == "SOURCE_UNREACHABLE"


def test_reachable_but_empty_returns_empty_list():
    src, _ = _src({"RSS/Search": (EMPTY_RSS, 200)})
    assert src.search("无职转生") == []


def test_parse_error_raises():
    src, _ = _src({"RSS/Search": ("<not xml", 200)})
    with pytest.raises(AnimeSourceError) as ei:
        src.search("x")
    assert ei.value.code == "PARSE_ERROR"


def test_bad_xml_falls_back_to_next_base():
    # a mirror returns an HTML error page (bad XML) -> try the next base (review #3)
    sess = FakeSession({"mirror.example": (BAD_XML, 200),
                        "mikanani.me": (RSS, 200)})
    src = MikanRssSource(["https://mirror.example", "https://mikanani.me"],
                         session=sess)
    cands = src.search("无职转生")
    assert len(cands) == 1
    assert any("mirror.example" in u for u in sess.calls)   # tried & failed
    assert any("mikanani.me" in u for u in sess.calls)      # then fell back


def test_all_bases_bad_xml_raises_parse_error():
    sess = FakeSession({"a.example": (BAD_XML, 200),
                        "b.example": ("<still<broken", 200)})
    src = MikanRssSource(["https://a.example", "https://b.example"], session=sess)
    with pytest.raises(AnimeSourceError) as ei:
        src.search("x")
    assert ei.value.code == "PARSE_ERROR"


# -- F6: in-search deadline -- checked before EVERY HTTP request --------------

def test_deadline_exhausted_raises_timeout_before_next_base():
    clock = {"now": 0.0}

    class SlowSession:
        def __init__(self):
            self.timeouts: list[float] = []

        def get(self, url, timeout=None, **kw):
            self.timeouts.append(timeout)
            clock["now"] += 10.0                 # each request burns 10s
            raise ConnectionError("slow network")

    sess = SlowSession()
    src = MikanRssSource(["https://a.example", "https://b.example"],
                         session=sess, timeout=15, clock=lambda: clock["now"])
    with pytest.raises(AnimeSourceError) as ei:
        src.search("无职转生", deadline=8.0)
    assert ei.value.code == "TIMEOUT"
    assert len(sess.timeouts) == 1               # base 2 never attempted
    assert sess.timeouts[0] == 8.0               # min(own 15, remaining 8)


def test_no_deadline_keeps_own_timeout():
    class Recorder:
        def __init__(self):
            self.timeouts: list[float] = []

        def get(self, url, timeout=None, **kw):
            self.timeouts.append(timeout)
            return FakeResp(RSS, 200)

    sess = Recorder()
    src = MikanRssSource(["https://mikanani.me"], session=sess, timeout=15)
    src.search("无职转生")
    assert sess.timeouts == [15]                 # unchanged without a deadline


def test_materialize_no_side_effect():
    src, sess = _src({"RSS/Search": (RSS, 200)})
    cand = src.search("无职转生")[0]
    n_calls = len(sess.calls)
    res = src.materialize(cand)
    assert res.locator == cand.locator          # pure repackage
    assert res.source == "mikan"
    assert len(sess.calls) == n_calls           # no extra HTTP


# -- search-quality §2.3: 0-candidate fallback to the longest CJK run ----------

class SequenceSession:
    """Returns each supplied XML body on successive GETs (last one sticks)."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls: list[str] = []

    def get(self, url, timeout=None, **kw):
        self.calls.append(url)
        text = self.texts[min(len(self.calls) - 1, len(self.texts) - 1)]
        return FakeResp(text, 200)


def test_zero_candidates_retries_longest_cjk_run():
    # user口吻「Re从零开始的异世界生活」→ mikan server returns 0; retry once with the
    # longest contiguous CJK run「从零开始的异世界生活」(§2.3).
    sess = SequenceSession([EMPTY_RSS, RSS])
    src = MikanRssSource(["https://mikanani.me"], session=sess)
    cands = src.search("Re从零开始的异世界生活")
    assert len(cands) == 1                                    # fallback hit
    assert len(sess.calls) == 2                               # exactly one retry
    assert urllib.parse.quote("从零开始的异世界生活") in sess.calls[1]


def test_all_cjk_query_no_second_request_when_empty():
    # an all-CJK query has no shorter CJK run -> fallback == query -> no retry
    sess = SequenceSession([EMPTY_RSS])
    src = MikanRssSource(["https://mikanani.me"], session=sess)
    assert src.search("关于我转生变成史莱姆这档事") == []
    assert len(sess.calls) == 1


def test_network_error_does_not_trigger_fallback():
    # only "reachable but 0 candidates" retries; a raise propagates unretried
    sess = FakeSession({"mikanani.me": ConnectionError("down")})
    src = MikanRssSource(["https://mikanani.me"], session=sess)
    with pytest.raises(AnimeSourceError) as ei:
        src.search("Re从零开始的异世界生活")
    assert ei.value.code == "SOURCE_UNREACHABLE"
    assert len(sess.calls) == 1                               # no fallback GET
