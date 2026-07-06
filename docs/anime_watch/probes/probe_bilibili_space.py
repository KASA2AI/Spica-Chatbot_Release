#!/usr/bin/env python3
"""Phase 0 probe: bilibili space video-list crawlability + naming conventions.

Answers open questions #1 and #2 (WBI signing needed? anon crawlable? what do
the carrier account's video titles look like -> the bilibili match rules).
NOT production code -- one-off recon. Writes redacted sample_bilibili.json.

The raw arc/search API returns code=-403 without a WBI signature (verified in
recon), so this implements the WBI mixin-key signing dance.

Run: python docs/anime_watch/probes/probe_bilibili_space.py [mid]
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.parse
from functools import reduce
from pathlib import Path

import requests

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
HERE = Path(__file__).parent
DEFAULT_MID = "3493112693394137"

# WBI mixin-key reorder table (public constant of the signing scheme).
MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]


def seed_buvid(sess: requests.Session) -> None:
    """Newer risk control (-352) needs buvid3/buvid4 fingerprint cookies."""
    try:
        r = sess.get("https://api.bilibili.com/x/frontend/finger/spi", timeout=10)
        d = r.json().get("data", {})
        if d.get("b_3"):
            sess.cookies.set("buvid3", d["b_3"], domain=".bilibili.com")
        if d.get("b_4"):
            sess.cookies.set("buvid4", d["b_4"], domain=".bilibili.com")
        print(f"seeded buvid3={str(d.get('b_3'))[:12]}...")
    except Exception as e:  # noqa: BLE001
        print(f"buvid seed failed: {e}")


# Fake WebGL fingerprint params newer risk control demands (dm_*).
DM_PARAMS = {
    "dm_img_list": "[]",
    "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ",
    "dm_cover_img_str": "QU5HTEUgKEludGVsLCBNZXNhIEludGVsKEwpIFVIRCBHcmFwaGljcw",
    "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
}


def get_wbi_keys(sess: requests.Session) -> tuple[str, str]:
    r = sess.get("https://api.bilibili.com/x/web-interface/nav", timeout=10)
    r.raise_for_status()
    wbi = r.json()["data"]["wbi_img"]
    img = wbi["img_url"].rsplit("/", 1)[-1].split(".")[0]
    sub = wbi["sub_url"].rsplit("/", 1)[-1].split(".")[0]
    return img, sub


def mixin_key(img: str, sub: str) -> str:
    raw = img + sub
    return reduce(lambda s, i: s + raw[i], MIXIN_TAB, "")[:32]


def sign(params: dict, mkey: str) -> dict:
    params = dict(params)
    params["wts"] = int(time.time())
    items = sorted(params.items())
    # bilibili strips !'()* from values before signing
    query = urllib.parse.urlencode(
        {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in items}
    )
    params["w_rid"] = hashlib.md5((query + mkey).encode()).hexdigest()
    return params


def main() -> None:
    mid = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MID
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Referer": "https://space.bilibili.com/"})
    # A homepage visit + finger/spi seed buvid cookies (needed for -352).
    try:
        sess.get(f"https://space.bilibili.com/{mid}", timeout=10)
    except Exception:  # noqa: BLE001
        pass
    seed_buvid(sess)

    img, sub = get_wbi_keys(sess)
    mkey = mixin_key(img, sub)
    print(f"wbi img={img[:8]}... sub={sub[:8]}... mixin={mkey[:8]}...")

    # Risk control (-352 / empty body) is probabilistic -> retry with re-seed.
    data = None
    for attempt in range(6):
        params = sign(
            {"mid": mid, "ps": 30, "pn": 1, "order": "pubdate",
             "platform": "web", "web_location": 1550101, **DM_PARAMS},
            mkey,
        )
        r = sess.get("https://api.bilibili.com/x/space/wbi/arc/search",
                     params=params, timeout=12)
        try:
            d = r.json()
        except Exception:  # noqa: BLE001 -- risk-control HTML/empty body
            print(f"  attempt {attempt}: non-JSON (len={len(r.text)}), re-seeding")
            seed_buvid(sess)
            continue
        if d.get("code") == 0:
            data = d
            print(f"  attempt {attempt}: OK")
            break
        print(f"  attempt {attempt}: code={d.get('code')} {d.get('message')!r}, re-seeding")
        seed_buvid(sess)
    if data is None:
        print("FAILED after retries -> anon crawl too flaky; login cookie needed.")
        return
    print(f"code={data.get('code')} message={data.get('message')!r}")
    if data.get("code") != 0:
        print("RISK-CONTROL or auth needed -> anon crawl of this endpoint blocked.")
        print("Open question #2 answer: needs cookies / stronger signing.")
        (HERE / "sample_bilibili.json").write_text(
            json.dumps({"code": data.get("code"), "message": data.get("message")},
                       ensure_ascii=False, indent=2))
        return

    page = data["data"]["list"]["vlist"]
    total = data["data"]["page"]["count"]
    print(f"total videos on space: {total}, fetched: {len(page)}\n")

    samples = []
    for idx, v in enumerate(page):
        rec = {"title": v.get("title"), "bvid": v.get("bvid"),
               "created": v.get("created"), "length": v.get("length"),
               "is_union_video": v.get("is_union_video")}
        samples.append(rec)
        if idx < 15:
            print(f"[{idx}] {v.get('title')[:75]}")
            print(f"     bvid={v.get('bvid')} len={v.get('length')} "
                  f"union={v.get('is_union_video')}")

    (HERE / "sample_bilibili.json").write_text(
        json.dumps(samples, ensure_ascii=False, indent=2))
    print(f"\nwrote {HERE / 'sample_bilibili.json'} ({len(samples)} titles)")


if __name__ == "__main__":
    main()
