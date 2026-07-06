"""Bilibili space anime source adapter (Phase 2).

Implements ``AnimeSourcePort`` over the carrier account's space video list.
Per Phase 0 (docs/anime_watch/probes/): the arc/search API needs WBI signing +
buvid3/buvid4 fingerprint cookies + dm_* params, and risk control (-352 / empty
body) is probabilistic -> retry with re-seed. A carrier upload is "one bvid =
whole-season 分P collection", so ``search`` expands each matched video into
per-part SINGLE-episode candidates (locator ``BV..:<part>``) via
``part_source_title``.

Prefilter reuses the Phase-1 resolver (``parse_source_title`` + ``name_matches``)
so alias / short-vs-long-name / fullwidth-bracket handling is NOT re-invented
(review #3). ``materialize`` is a pure repackage -- keeps ``bvid:part``, no
download, no side effect.

Qt-free (CLAUDE.md #1). No os.getenv -- cookie / space uids / timeout are all
constructor args (review #5); default session sets ``trust_env = False``.
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
import time
import urllib.parse
from functools import reduce
from typing import Any

from spica.anime.models import AnimeCandidate, AnimeResource
from spica.anime.resolver import name_matches, parse_source_title, part_source_title
from spica.ports.anime_source import AnimeSourceError

_LOG = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/126.0 Safari/537.36")
_ARC_SEARCH = "https://api.bilibili.com/x/space/wbi/arc/search"
_NAV = "https://api.bilibili.com/x/web-interface/nav"
_FINGER = "https://api.bilibili.com/x/frontend/finger/spi"
_VIEW = "https://api.bilibili.com/x/web-interface/view"

# WBI mixin-key reorder table (public constant of the signing scheme).
_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]
_DM_PARAMS = {
    "dm_img_list": "[]",
    "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ",
    "dm_cover_img_str": "QU5HTEUgKEludGVsLCBNZXNhIEludGVsKEwpIFVIRCBHcmFwaGljcw",
    "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
}

# episode number inside a 分P title: 「第3话」/「EP05」, else a leading「01」.
_EP_MARKER_RE = re.compile(r"第\s*(\d{1,3})\s*[话話集]|E(?:P)?\s*(\d{1,3})", re.I)
_LEADING_NUM_RE = re.compile(r"^\s*0*(\d{1,3})(?!\d)")
# an episode range「01-02」in a collection title (ascending). Keep in sync with
# resolver._RANGE_RE (F12): year spans「2024-25」and「24-25赛季/年度」excluded.
_RANGE_RE = re.compile(
    r"(?<![A-Za-z0-9])(\d{1,3})\s*-\s*(\d{1,3})(?![0-9A-Za-z])(?!\s*(?:赛季|年度))")


def _mixin_key(img: str, sub: str) -> str:
    raw = img + sub
    return reduce(lambda s, i: s + raw[i], _MIXIN_TAB, "")[:32]


def _sign(params: dict, mkey: str, *, now: float) -> dict:
    params = dict(params)
    params["wts"] = int(now)
    items = sorted(params.items())
    query = urllib.parse.urlencode(
        {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in items}
    )
    params["w_rid"] = hashlib.md5((query + mkey).encode()).hexdigest()
    return params


def _part_episode(part_title: str) -> int | None:
    m = _EP_MARKER_RE.search(part_title or "")
    if m:
        return int(m.group(1) or m.group(2))
    m = _LEADING_NUM_RE.match(part_title or "")
    return int(m.group(1)) if m else None


def _range_start(collection_title: str) -> int | None:
    for m in _RANGE_RE.finditer(collection_title):
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo < hi:
            return lo
    return None


def _episode_for_part(collection_title: str, part_title: str, page_index: int,
                      single_ep: int | None, range_start: int | None,
                      *, multi_part: bool) -> int | None:
    """Fallback order (review #2): part-title episode -> collection single
    episode -> collection range-start + page offset -> page index. The page-index
    fallback only applies to a real multi-part collection (F1): a single-part
    video with no episode evidence anywhere returns None so the caller drops it
    instead of fabricating ep1."""
    ep = _part_episode(part_title)
    if ep is not None:
        return ep
    if single_ep is not None:
        return single_ep
    if range_start is not None:
        return range_start + page_index - 1
    return page_index if multi_part else None


def _default_session() -> Any:
    import requests  # lazy: tests inject a fake session
    s = requests.Session()
    s.trust_env = False                      # no env proxy (review #5)
    s.headers.update({"User-Agent": _UA, "Referer": "https://space.bilibili.com/"})
    return s


class BilibiliSpaceSource:
    name = "bilibili"

    def __init__(self, space_uids: list[str], *, cookie: str | None = None,
                 session: Any = None, timeout: float = 12, max_retries: int = 5,
                 max_pages: int = 10,
                 sleep: Any = None, clock: Any = None) -> None:
        self._uids = [str(u) for u in space_uids]
        self._http = session if session is not None else _default_session()
        if cookie:                            # injected, NEVER os.getenv (review #5)
            # Into the JAR, never a manual Cookie header: with a Cookie header
            # present, http.cookiejar skips injection entirely, so the seeded
            # buvid3/4 fingerprint would never be sent (F5).
            for kv in cookie.split(";"):
                k, _, v = kv.strip().partition("=")
                if k:
                    self._http.cookies.set(k, v, domain=".bilibili.com")
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_pages = max_pages
        self._sleep = sleep if sleep is not None else time.sleep
        self._clock = clock if clock is not None else time.monotonic
        self._deadline_at: float | None = None

    # -- public port ---------------------------------------------------------

    def search(self, title_query: str, *,
               deadline: float | None = None) -> list[AnimeCandidate]:
        self._deadline_at = (None if deadline is None
                             else self._clock() + deadline)
        self._seed_buvid()
        img, sub = self._wbi_keys()
        mkey = _mixin_key(img, sub)

        errors: list[AnimeSourceError] = []
        any_ok = False
        cands: list[AnimeCandidate] = []
        for uid in self._uids:
            try:
                videos = self._space_videos(uid, mkey)
            except AnimeSourceError as e:
                errors.append(e)             # this space failed -> try the next
                continue
            any_ok = True
            for v in videos:
                st = parse_source_title(v["title"])
                if not name_matches(title_query, st):   # resolver-based prefilter
                    continue
                cands.extend(self._expand_parts(v, st))
        if not any_ok and errors:
            raise errors[0]
        return cands

    def materialize(self, candidate: AnimeCandidate) -> AnimeResource:
        if ":" not in candidate.locator:
            raise AnimeSourceError("BAD_CANDIDATE", "expected bvid:part locator")
        # episode_key is a placeholder: the coordinator is the single generation
        # point and overwrites it with the query-derived canonical key (F2).
        return AnimeResource(
            episode_key="", source=self.name, locator=candidate.locator,
            display_title=candidate.display_title, size_bytes=candidate.size_bytes,
        )

    # -- HTTP steps ----------------------------------------------------------

    def _budget_timeout(self) -> float:
        """Remaining-budget gate before EVERY HTTP request (F6): raises TIMEOUT
        once the per-search deadline is exhausted, else caps the request timeout.
        Must be called OUTSIDE the broad try/excepts below or they'd swallow it."""
        if self._deadline_at is None:
            return self._timeout
        remaining = self._deadline_at - self._clock()
        if remaining <= 0:
            raise AnimeSourceError("TIMEOUT", "search deadline exceeded")
        return min(self._timeout, remaining)

    def _seed_buvid(self) -> None:
        t = self._budget_timeout()               # outside the try (F6)
        try:
            resp = self._http.get(_FINGER, timeout=t)
            d = resp.json().get("data", {})
            if d.get("b_3"):
                self._http.cookies.set("buvid3", d["b_3"], domain=".bilibili.com")
            if d.get("b_4"):
                self._http.cookies.set("buvid4", d["b_4"], domain=".bilibili.com")
        except Exception:  # noqa: BLE001 -- best-effort seed, signing still tried
            pass

    def _wbi_keys(self) -> tuple[str, str]:
        t = self._budget_timeout()               # outside the try (F6)
        try:
            resp = self._http.get(_NAV, timeout=t)
            wbi = resp.json()["data"]["wbi_img"]
            img = wbi["img_url"].rsplit("/", 1)[-1].split(".")[0]
            sub = wbi["sub_url"].rsplit("/", 1)[-1].split(".")[0]
            return img, sub
        except Exception as e:  # noqa: BLE001
            raise AnimeSourceError("SOURCE_UNREACHABLE", f"nav failed: {e}")

    def _arc_search(self, uid: str, pn: int, mkey: str) -> dict:
        last: Any = None
        for attempt in range(self._max_retries):
            if attempt:            # random backoff between retries (F9, §5.2)
                self._sleep(random.uniform(0.5, 1.5))
            params = _sign(
                {"mid": uid, "ps": 30, "pn": pn, "order": "pubdate",
                 "platform": "web", "web_location": 1550101, **_DM_PARAMS},
                mkey, now=time.time(),
            )
            t = self._budget_timeout()           # outside the try (F6)
            try:
                resp = self._http.get(_ARC_SEARCH, params=params, timeout=t)
                d = resp.json()
            except Exception:  # noqa: BLE001 -- risk-control HTML/empty body
                last = "non-json"
                self._seed_buvid()
                continue
            if d.get("code") == 0:
                return d
            last = d.get("code")
            self._seed_buvid()
        raise AnimeSourceError("RISK_CONTROL", f"arc/search code={last}")

    def _space_videos(self, uid: str, mkey: str) -> list[dict]:
        videos: list[dict] = []
        for pn in range(1, self._max_pages + 1):
            if pn > 1:             # inter-page throttle (F9, §5.2)
                self._sleep(0.5)
            try:
                d = self._arc_search(uid, pn, mkey)
            except AnimeSourceError as e:
                if videos:         # keep already-fetched pages (F11): a mid-
                    _LOG.warning(  # pagination failure must not void the source
                        "bilibili space %s page %d failed (%s); "
                        "keeping %d videos from earlier pages",
                        uid, pn, e.code, len(videos))
                    break
                raise              # first page failed -> the space really failed
            vlist = d.get("data", {}).get("list", {}).get("vlist", []) or []
            if not vlist:
                break
            videos.extend({"bvid": v.get("bvid"), "title": v.get("title", "")}
                          for v in vlist)
        return videos

    def _pagelist(self, bvid: str) -> list[dict]:
        t = self._budget_timeout()               # outside the try (F6)
        try:
            resp = self._http.get(_VIEW, params={"bvid": bvid}, timeout=t)
            d = resp.json()
            if d.get("code") != 0:
                return []
            return [{"page": p.get("page"), "part": p.get("part", "")}
                    for p in d.get("data", {}).get("pages", [])]
        except Exception:  # noqa: BLE001 -- skip a video whose parts we can't read
            return []

    def _expand_parts(self, video: dict, st: Any) -> list[AnimeCandidate]:
        if st.is_special:   # 总集篇/OVA/剧场版: never expanded to episodes (F1)
            return []
        bvid, ctitle = video["bvid"], video["title"]
        pages = self._pagelist(bvid)
        # collection-level anchors for the episode-mapping fallback (review #2)
        parsed = parse_source_title(ctitle)
        single_ep = parsed.episode if (not parsed.is_batch
                                       and parsed.episode is not None) else None
        range_start = _range_start(ctitle)
        multi_part = len(pages) > 1
        out: list[AnimeCandidate] = []
        for pg in pages:
            page_index = pg.get("page") or (len(out) + 1)
            ep = _episode_for_part(ctitle, pg.get("part", ""), page_index,
                                   single_ep, range_start, multi_part=multi_part)
            if ep is None:   # single-part video with no episode evidence (F1)
                continue
            out.append(AnimeCandidate(
                source=self.name, locator=f"{bvid}:{page_index}",
                parsed=part_source_title(ctitle, episode=ep, season=st.season),
                display_title=f"{ctitle} [P{page_index}]",
            ))
        return out
