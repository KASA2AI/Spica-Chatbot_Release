"""Golden cases for the anime title resolver (Phase 1).

Titles marked (real) are verbatim from Phase 0 probe samples
(docs/anime_watch/probes/sample_{mikan,bilibili}.json) -- the cases that the
naive probe regexes got WRONG, now pinned correct.
"""

from __future__ import annotations

import pytest

from spica.anime.models import LATEST, AnimeCandidate, EpisodeRef, SourceTitle
from spica.anime.resolver import (
    cn_to_int,
    name_matches,
    parse_query,
    parse_source_title,
    part_source_title,
    resolve,
)


# -- Chinese numeral conversion ----------------------------------------------

@pytest.mark.parametrize("s,expected", [
    ("三", 3), ("一", 1), ("十", 10), ("十二", 12), ("二十", 20),
    ("二十一", 21), ("两", 2), ("〇", 0), ("5", 5), ("12", 12),
    ("Ⅲ", 3), ("II", 2),
])
def test_cn_to_int(s, expected):
    assert cn_to_int(s) == expected


# -- user query parsing ------------------------------------------------------

@pytest.mark.parametrize("query,title,season,episode", [
    ("无职转生第三季第一集", "无职转生", 3, 1),
    ("无职转生 第三季 第一集", "无职转生", 3, 1),
    ("我想看无职转生第三季第一集", "无职转生", 3, 1),
    ("无职转生S3E1", "无职转生", 3, 1),
    ("无职转生第3季第12话", "无职转生", 3, 12),
    ("葬送的芙莉莲第一集", "葬送的芙莉莲", None, 1),
    ("无职转生第三季最新一集", "无职转生", 3, LATEST),
    ("我想看无职转生最新一集", "无职转生", None, LATEST),
    ("鬼灭之刃", "鬼灭之刃", None, None),
])
def test_parse_query(query, title, season, episode):
    ref = parse_query(query)
    assert ref.title_query == title
    assert ref.season == season
    assert ref.episode == episode


# -- source title parsing (the traps the naive probe hit) --------------------

def test_source_season_not_stolen_as_episode():
    # (real) Skymoon title -- naive regex parsed ep=3 from 「第3季」; correct ep=2.
    st = parse_source_title(
        "[Skymoon-Raws] 无职转生，到了异世界就拿出真本事 第3季 / "
        "Mushoku Tensei 3rd Season - 02 [1080p]")
    assert st.season == 3
    assert st.episode == 2
    assert st.subgroup == "Skymoon-Raws"
    assert st.quality == "1080p"


def test_source_chinese_numeral_season():
    # (real) 沸班亚马 -- naive SEASON_RE missed 「第三季」(Chinese numeral).
    st = parse_source_title(
        "[沸班亚马制作组] 无职转生 第三季 ～到了异世界就拿出真本事～ - 01 "
        "[IQIYI WebRip 2160p NVENC AAC][简繁]")
    assert st.season == 3
    assert st.episode == 1
    assert st.quality == "2160p"
    assert st.subtitle == "简繁"


def test_source_lolihouse_ideal():
    # (real) the ideal match: 1080p + 简繁内封.
    st = parse_source_title(
        "[LoliHouse] 无职转生 3期 / Mushoku Tensei S3 - 02 "
        "[WebRip 1080p HEVC-10bit AAC][简繁内封字幕]")
    assert st.season == 3
    assert st.episode == 2
    assert st.quality == "1080p"
    assert st.subtitle == "简繁"


def test_source_cht_only_detected():
    # (real) ANi Baha -- CHT (繁体) only; must be detectable to deprioritize.
    st = parse_source_title(
        "[ANi] 无职转生～到了异世界就拿出真本事～第三季 - 02 "
        "[1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]")
    assert st.season == 3
    assert st.episode == 2
    assert st.subtitle == "繁体"


def test_ascii_roman_season_parses():
    # F10: ASCII II/III/IV were dead entries in _ROMAN -- no pattern fed them.
    st = parse_source_title("[X] 无职转生II ～異世界に行ったら本気だす～ - 13 [1080p]")
    assert st.season == 2
    assert st.episode == 13


def test_ascii_roman_iv_parses():
    st = parse_source_title("[X] Overlord IV - 03 [1080p]")
    assert st.season == 4
    assert st.episode == 3


def test_ascii_roman_not_matched_inside_words():
    # letter-isolated on both sides: ASCII / XIV / ViuTV must not become seasons
    assert parse_source_title("[X] ASCII艺术部 - 01 [1080p]").season is None
    assert parse_source_title("[X] Louis XIV - 01 [1080p]").season is None
    assert parse_source_title(
        "[Skymoon-Raws] 某番 - 01 [ViuTV][WEB-DL][CHT][1080p]").season is None


def test_ascii_roman_ii_is_safe_nonmatch_for_s1_request():
    # F10 repro: 「无职转生II」ep13 must NOT satisfy an S1E13 request
    cand = _cand("[X] 无职转生II ～異世界に行ったら本気だす～ - 13 [1080p]")
    r = resolve(EpisodeRef("无职转生", 1, 13), [cand])
    assert r.status != "matched"


def test_year_span_not_batch():
    # F12: a year span (2024-25) is not an episode range
    st = parse_source_title("[X] 某番 2024-25 秋季 - 03 [1080p]")
    assert st.is_batch is False
    assert st.episode == 3


def test_season_span_not_batch():
    st = parse_source_title("[X] 某番 24-25赛季 第03话 [1080p]")
    assert st.is_batch is False
    assert st.episode == 3


def test_true_ranges_still_batch():
    assert parse_source_title("[某组] 某番 01-12 [1080p]").is_batch is True
    assert parse_source_title("[某组] 某番 01-02话 [1080p]").is_batch is True
    assert parse_source_title("[某组] 某番 第01-12话 [1080p]").is_batch is True


def test_canonical_key_folds_all_query_wordings():
    # F2 tail: short / ja / romaji / FULL name of one anime, asked as separate
    # user queries, must key IDENTICALLY -- else the library dedup misses across
    # rewordings and re-downloads. The full name contains the canonical as a
    # substring, so the key basis must fold it down like _same_anime does.
    from spica.anime.resolver import canonical_episode_key
    keys = {
        canonical_episode_key(parse_query(q).title_query, 3, 1)
        for q in (
            "无职转生第三季第一集",
            "無職転生第三季第一集",
            "Mushoku Tensei S3 E1",
            "无职转生，到了异世界就拿出真本事 第三季第一集",
        )
    }
    assert keys == {"无职转生|s3|e1"}


def test_source_batch_flagged():
    st = parse_source_title("[某组] 无职转生 第三季 01-12 合集 [1080p]")
    assert st.is_batch is True


def test_source_batch_not_false_positive_on_single():
    st = parse_source_title(
        "[LoliHouse] 无职转生 3期 - 02 [WebRip 1080p][简繁内封]")
    assert st.is_batch is False


# -- matching / ranking ------------------------------------------------------

def _cand(title, locator="magnet:?xt=urn:btih:" + "a" * 40):
    return AnimeCandidate(source="mikan", locator=locator,
                          parsed=parse_source_title(title), display_title=title)


def test_resolve_picks_simplified_1080p_over_cht():
    ref = parse_query("无职转生第三季第一集")
    cands = [
        _cand("[ANi] 无职转生 第三季 - 01 [1080P][Baha][CHT][MP4]"),
        _cand("[LoliHouse] 无职转生 3期 / Mushoku Tensei S3 - 01 "
              "[WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"),
    ]
    res = resolve(ref, cands)
    assert res.status == "matched"
    assert res.chosen.parsed.subgroup == "LoliHouse"  # 简繁 beats CHT


def test_resolve_latest_takes_max_episode():
    ref = parse_query("无职转生第三季最新一集")
    cands = [
        _cand("[LoliHouse] 无职转生 3期 - 01 [1080p][简繁内封]"),
        _cand("[LoliHouse] 无职转生 3期 - 02 [1080p][简繁内封]"),
    ]
    res = resolve(ref, cands)
    assert res.status == "matched"
    assert res.chosen.parsed.episode == 2


def test_resolve_batch_filtered_out():
    ref = parse_query("无职转生第三季第一集")
    cands = [_cand("[某组] 无职转生 第三季 01-12 合集 [1080p][简繁]")]
    res = resolve(ref, cands)
    assert res.status == "none"


def test_resolve_ambiguous_seasons_when_unpinned():
    ref = parse_query("无职转生第一集")  # no season pinned
    cands = [
        _cand("[LoliHouse] 无职转生 / Mushoku Tensei - 01 [1080p][简繁]"),
        _cand("[LoliHouse] 无职转生 3期 - 01 [1080p][简繁]"),
    ]
    res = resolve(ref, cands)
    assert res.status == "ambiguous"
    assert len(res.candidates) == 2


def test_resolve_need_episode_when_unspecified():
    ref = parse_query("无职转生第三季")  # no episode
    cands = [_cand("[LoliHouse] 无职转生 3期 - 01 [1080p][简繁]")]
    res = resolve(ref, cands)
    assert res.status == "need_episode"


def test_resolve_none_when_no_name_match():
    ref = parse_query("间谍过家家第一集")
    cands = [_cand("[LoliHouse] 无职转生 3期 - 01 [1080p][简繁]")]
    res = resolve(ref, cands)
    assert res.status == "none"


def test_resolve_episode_not_found():
    ref = parse_query("无职转生第三季第五集")
    cands = [_cand("[LoliHouse] 无职转生 3期 - 01 [1080p][简繁]")]
    res = resolve(ref, cands)
    assert res.status == "none"


def test_name_matches_via_romaji():
    st = parse_source_title(
        "[LoliHouse] 无职转生 3期 / Mushoku Tensei S3 - 02 [1080p][简繁内封]")
    assert name_matches("无职转生", st)
    assert name_matches("Mushoku Tensei", st)


# -- review tail #4: minimal alias map (zh <-> ja <-> romaji), bidirectional ---

def test_alias_ja_query_matches_zh_source():
    # query「無職転生」(Japanese kanji) matches a Chinese「无职转生」source item
    zh_src = parse_source_title(
        "[LoliHouse] 无职转生 3期 / Mushoku Tensei S3 - 01 [1080p][简繁内封]")
    assert name_matches("無職転生", zh_src)
    res = resolve(parse_query("無職転生第三季第一集"), [_cand(
        "[LoliHouse] 无职转生 3期 / Mushoku Tensei S3 - 01 [1080p][简繁内封]")])
    assert res.status == "matched"


def test_alias_zh_query_matches_ja_source():
    # query「无职转生」(simplified) matches a Japanese「無職転生」source item
    ja_src = parse_source_title(
        "[某组] 無職転生 / Mushoku Tensei S3 - 01 [1080p][简繁]")
    assert name_matches("无职转生", ja_src)
    res = resolve(parse_query("无职转生第三季第一集"), [_cand(
        "[某组] 無職転生 / Mushoku Tensei S3 - 01 [1080p][简繁]")])
    assert res.status == "matched"


def test_alias_romaji_and_zh_same_group():
    romaji_src = parse_source_title("[X] Mushoku Tensei S3 - 01 [1080p][简繁]")
    assert name_matches("无职转生", romaji_src)
    assert name_matches("無職転生", romaji_src)


# -- review tail #1: alias-aware clustering (shared identity basis) -----------

def test_zh_ja_candidates_cluster_not_ambiguous():
    # zh「无职转生」and ja「無職転生」of the SAME anime must cluster -> matched.
    ref = parse_query("无职转生第三季第一集")
    cands = [
        _cand("[LoliHouse] 无职转生 3期 - 01 [1080p][简繁内封]"),
        _cand("[某组] 無職転生 / Mushoku Tensei S3 - 01 [1080p][简繁]"),
    ]
    res = resolve(ref, cands)
    assert res.status == "matched"


def test_long_query_matches_short_source_name():
    # a long user title must still match a source that uses the short name.
    ref = parse_query("无职转生到了异世界就拿出真本事第三季第一集")
    assert ref.title_query == "无职转生到了异世界就拿出真本事"
    res = resolve(ref, [_cand("[LoliHouse] 无职转生 3期 - 01 [1080p][简繁内封]")])
    assert res.status == "matched"


# -- review tail #2: leading fullwidth bracket that is a TITLE, not a tag -----

def test_fullwidth_title_bracket_kept_black_cat():
    st = parse_source_title("【黑猫与魔女的教室】第13话")
    assert "黑猫与魔女的教室" in st.name_zh
    assert st.episode == 13


def test_fullwidth_title_bracket_kept_mao():
    st = parse_source_title("【摩绪】第14话")
    assert "摩绪" in st.name_zh
    assert st.episode == 14


def test_fullwidth_title_bracket_with_season():
    st = parse_source_title("【入间同学入魔了！第四季】第14话")
    assert "入间同学入魔了" in st.name_zh
    assert st.season == 4
    assert st.episode == 14


def test_fullwidth_tag_bracket_still_stripped():
    st = parse_source_title("【4K超清】无职转生 第三季 - 05")
    assert st.season == 3
    assert st.episode == 5
    assert "无职转生" in st.name_zh


def test_fullwidth_quarter_collection_tag_still_batch():
    st = parse_source_title("【7月/合集】穹庐下的魔女 01-02话")
    assert st.is_batch is True
    assert "穹庐下的魔女" in st.name_zh


# -- finding #1: bilibili 分P collection is not batch-filtered -----------------

def test_raw_collection_title_is_batch():
    # (real) the carrier bundles a season as one video 「01-02话」-> a range/batch
    st = parse_source_title("【4K超清】无职转生 第三季 01-02话（每周更新）")
    assert st.is_batch is True


def test_part_source_title_is_single_episode():
    # the bilibili adapter models each 分P as a single-episode candidate
    st = part_source_title("【4K超清】无职转生 第三季 01-02话（每周更新）",
                           episode=1, season=3)
    assert st.is_batch is False
    assert st.episode == 1
    assert st.season == 3


def test_bilibili_part_resolves_despite_collection_title():
    ref = parse_query("无职转生第三季第一集")
    part = AnimeCandidate(
        source="bilibili", locator="BV1fmMP6NEvw:1",
        parsed=part_source_title("【4K超清】无职转生 第三季 01-02话（每周更新）",
                                 episode=1, season=3),
        display_title="【4K超清】无职转生 第三季 01-02话")
    res = resolve(ref, [part])
    assert res.status == "matched"
    assert res.chosen.locator == "BV1fmMP6NEvw:1"


# -- finding #6: bracket handling / [02] episode -----------------------------

def test_bracket_episode_parses():
    st = parse_source_title("[Sakurato] 无职转生 3期 [02] [1080p][简繁内封]")
    assert st.episode == 2
    assert st.season == 3


def test_bilibili_fullwidth_tag_stripped_but_name_kept():
    st = parse_source_title("【4K超清】无职转生 第三季 - 05（每周更新）")
    assert st.season == 3
    assert st.episode == 5
    assert name_matches("无职转生", st)


# -- finding #7: batch regex covers 1-12 but not S3 - 01 ----------------------

def test_batch_single_digit_start_range():
    assert parse_source_title("[某组] 无职转生 1-12 [1080p][简繁]").is_batch is True


def test_batch_not_flagged_on_season_episode():
    st = parse_source_title(
        "[LoliHouse] 无职转生 3期 / Mushoku Tensei S3 - 01 [1080p][简繁内封]")
    assert st.is_batch is False   # 「S3 - 01」must NOT read as a range


# -- finding #5: specials / cour / v2 / episode-0 / absolute numbering --------

def test_special_movie_flagged_no_episode():
    st = parse_source_title("[X] 无职转生 剧场版 [1080p][简繁]")
    assert st.is_special is True
    assert st.episode is None


def test_special_ova_not_matched_as_episode():
    ref = parse_query("无职转生第一集")
    cands = [_cand("[LoliHouse] 无职转生 OVA [1080p][简繁]")]
    assert resolve(ref, cands).status == "none"


def test_recap_x5_not_parsed_as_integer_episode():
    # x.5 总集篇: v1 has no fractional episodes -> non-match (safe), documented.
    st = parse_source_title("[X] 无职转生 3期 第12.5话 总集篇 [1080p][简繁]")
    assert st.is_special is True   # 总集篇 flagged
    assert st.episode is None


def test_v2_suffix_parses_episode():
    st = parse_source_title("[LoliHouse] 无职转生 3期 - 01v2 [1080p][简繁内封]")
    assert st.episode == 1


def test_episode_zero_prologue():
    st = parse_source_title("[X] 无职转生 3期 第0话 [1080p][简繁]")
    assert st.episode == 0


def test_cour_parses_episode_without_remap():
    # クール offset is a documented v1 limitation: we do NOT remap 13.. to 01..;
    # the number is taken as written. Just assert it parses and doesn't crash.
    st = parse_source_title("[X] 无职转生 第2クール - 13 [1080p][简繁]")
    assert st.episode == 13


def test_absolute_numbering_not_remapped_is_safe_nonmatch():
    # 第25话 is NOT remapped to S2E01 in v1 -> a S2E1 request safely doesn't match
    ref = parse_query("无职转生第二季第一集")
    cands = [_cand("[X] 无职转生 第二季 - 25 [1080p][简繁]")]
    assert resolve(ref, cands).status == "none"


# -- finding #4: confidence gating -------------------------------------------

def test_confidence_distinct_titles_ambiguous():
    ref = parse_query("魔法第一集")
    cands = [
        _cand("[A] 魔法少女小圆 - 01 [1080p][简繁]"),
        _cand("[B] 魔法科高中的劣等生 - 01 [1080p][简繁]"),
    ]
    res = resolve(ref, cands)
    assert res.status == "ambiguous"
    assert len(res.candidates) == 2


def test_confidence_short_query_ambiguous():
    ref = EpisodeRef(title_query="刀", season=None, episode=1)
    cands = [
        _cand("[A] 刀剑神域 - 01 [1080p][简繁]"),
        _cand("[B] 刀语 - 01 [1080p][简繁]"),
    ]
    assert resolve(ref, cands).status == "ambiguous"


def test_same_anime_different_verbosity_still_matches():
    # short vs full title of the SAME anime must cluster -> matched, not ambiguous
    ref = parse_query("无职转生第三季第一集")
    cands = [
        _cand("[ANi] 无职转生～到了异世界就拿出真本事～第三季 - 01 [1080P][CHT]"),
        _cand("[LoliHouse] 无职转生 3期 / Mushoku Tensei S3 - 01 [1080p][简繁内封]"),
    ]
    res = resolve(ref, cands)
    assert res.status == "matched"
    assert res.chosen.parsed.subgroup == "LoliHouse"
