#!/usr/bin/env python3
"""Search-quality live probe: real-season titles through the real search chain.

One-off recon (NOT production code, does not enter spica/ -- same convention as
probe_mikan.py). Findings + optimization list: ../SEARCH_QUALITY_FINDINGS.md.

Runs the REAL chain against mikanani.me:
    parse_query -> MikanRssSource.search (network) -> resolver.resolve (pure)
Resolve-only: never calls materialize's downstream, never touches qbt/yt-dlp.
~16 RSS requests total, 1.2s throttle between cases (be polite to mikan).

Run from repo root:  python docs/anime_watch/probes/probe_search_quality.py
Re-run after Phase 5 search-hardening fixes land to confirm the failing cases
(2/3/5/10 as of 2026-07-07) flip green.
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from spica.adapters.anime_source.mikan import MikanRssSource  # noqa: E402
from spica.anime.resolver import (  # noqa: E402
    _cluster_by_title,
    name_matches,
    parse_query,
    parse_source_title,
    resolve,
)

# (query, expectation note) -- titles from mikan's 2026 spring season list
CASES = [
    ("租借女友第五季第2集", "中文数字第五季"),
    ("关于我转生变成史莱姆这档事第四季第1集", "长标题全称+第四季 (RSS 窗口滚动)"),
    ("转生史莱姆第四季第1集", "常用简称(非子串)"),
    ("实力至上主义教室第四季最新一集", "部分标题(子串)+LATEST"),
    ("Re从零开始的异世界生活第四季第3集", "官方标题含全角冒号 Re："),
    ("转生最新一集", "短歧义词+LATEST (review P2-1 现场)"),
    ("石纪元第2集", "官方季标=第x部分(非标准)"),
    ("异兽魔都第二季第1集", "短标题续作"),
    ("尖帽子的魔法工房第一集", "新作S1+中文数字集"),
    ("欺诈游戏最新一集", "普通标题+LATEST (假歧义样本)"),
]

# server-side punctuation sensitivity (findings §2.3)
SEARCH_VARIANTS = [
    "Re从零开始的异世界生活",     # user phrasing, no colon -> 0 as of 2026-07
    "Re：从零开始的异世界生活",   # official full-width colon
    "从零开始的异世界生活",       # longest CJK run (the fallback candidate)
    "Re:从零开始的异世界生活",    # ASCII colon
]

QUALITY = "1080p"
SUBPREF = ["简繁", "简体"]


def run_case(case_no: int, query: str, note: str, src: MikanRssSource) -> None:
    print(f"\n=== case {case_no}: 「{query}」  ({note})")
    ref = parse_query(query)
    print(f"  parse_query -> title='{ref.title_query}' season={ref.season} "
          f"episode={ref.episode!r}")
    try:
        cands = src.search(ref.title_query, deadline=20.0)
    except Exception as e:  # noqa: BLE001 -- recon harness
        print(f"  search RAISED: {type(e).__name__}: {e}")
        return
    print(f"  search -> {len(cands)} candidates")
    for c in cands[:3]:
        p = c.parsed
        print(f"    · s={p.season} e={p.episode} q={p.quality} sub={p.subtitle} "
              f"batch={p.is_batch} | {c.display_title[:72]}")
    res = resolve(ref, cands, quality=QUALITY, subtitle_pref=SUBPREF)
    print(f"  resolve -> status={res.status}  reason={res.reason!r}")
    chosen = getattr(res, "chosen", None)
    if chosen is not None:
        p = chosen.parsed
        print(f"  CHOSEN: s={p.season} e={p.episode} q={p.quality} sub={p.subtitle}"
              f" | {chosen.display_title[:80]}")
    if res.status == "ambiguous":
        for c in list(res.candidates)[:5]:
            print(f"    ? {c.display_title[:80]}")


def offline_parses() -> None:
    """Pinned root-cause repros (findings §2.1/§2.2/§2.4) -- no network."""
    print("\n== offline parses (root-cause repros) ==")
    t1 = ("[喵萌奶茶屋&LoliHouse] 欺诈游戏 / 诈欺游戏 / LIAR GAME - 13 "
          "[WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]")
    t2 = "【喵萌奶茶屋】★04月新番★[欺诈游戏 / 诈欺游戏 / LIAR GAME][13][1080p][繁日双语]"
    p1, p2 = parse_source_title(t1), parse_source_title(t2)
    print(f"  §2.1 t1 name_zh={p1.name_zh!r} e={p1.episode}")
    print(f"  §2.1 t2 name_zh={p2.name_zh!r} e={p2.episode} (bug: 组名+促销语)")

    class _C:  # minimal cluster-input shape
        def __init__(self, parsed):
            self.parsed = parsed
            self.display_title = parsed.raw

    n = len(_cluster_by_title([_C(p1), _C(p2)]))
    print(f"  §2.1 _cluster_by_title -> {n} clusters (expect 1; 2 = false ambiguity)")

    t3 = ("【豌豆字幕组】[关于我转生变成史莱姆这档事 第四季 / "
          "Tensei Shitara Slime Datta Ken S4][13(85)][繁体][1080P][MP4]")
    p3 = parse_source_title(t3)
    print(f"  §2.2 [13(85)] -> season={p3.season} episode={p3.episode} (expect e=13)")

    t4 = "[绿茶字幕组] 租借女友  / Kanojo Okarishimasu  [52][WebRip][1080p][简繁日内封]"
    p4 = parse_source_title(t4)
    print(f"  §0.3 absolute [52] -> s={p4.season} e={p4.episode} (by design: 安全非匹配)")

    hit = name_matches("转生史莱姆", parse_source_title(
        "[豌豆字幕组&LoliHouse] 关于我转生变成史莱姆这档事 第四季 / "
        "Tensei Shitara Slime Datta Ken 4th Season - 13 [WebRip 1080p]"))
    print(f"  §2.4 name_matches('转生史莱姆', 全称S4) -> {hit} (False = 简称硬边界)")


def main() -> None:
    src = MikanRssSource(["https://mikanani.me"], timeout=15)
    for i, (query, note) in enumerate(CASES, 1):
        try:
            run_case(i, query, note, src)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        time.sleep(1.2)

    print("\n== server-side search variants (findings §2.3) ==")
    for q in SEARCH_VARIANTS:
        try:
            n: object = len(src.search(q, deadline=20.0))
        except Exception as e:  # noqa: BLE001
            n = f"RAISED {e}"
        print(f"  searchstr={q!r:45} -> {n} candidates")
        time.sleep(1.2)

    offline_parses()


if __name__ == "__main__":
    main()
