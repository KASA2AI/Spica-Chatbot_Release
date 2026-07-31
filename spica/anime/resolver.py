"""Title resolution + source-candidate matching (Phase 1) -- the hard, pure part.

Two jobs:
1. ``parse_query``       user 「无职转生第三季第一集」/「…最新一集」-> EpisodeRef.
2. ``parse_source_title`` a mikan/bilibili title -> SourceTitle (for matching).
3. ``resolve``           EpisodeRef + [AnimeCandidate] -> MatchResult.

Design pinned by real Phase 0 data (docs/anime_watch/probes/PHASE0_FINDINGS.md):
- **Consume season markers BEFORE extracting the episode number**, else 「第3季」
  steals the episode slot (observed: Skymoon title parsed ep=3, should be 02).
- Season aliases across subgroups: ``N期`` / ``第N季`` / ``第N期`` / ``SN`` /
  ``Nrd Season`` / roman Ⅱ-Ⅳ / bare「第三季」(Chinese numeral).
- Same episode is offered by many subgroups; pick one by (quality, subtitle)
  preference. ``CHT``-only (繁体) is deprioritized under a 简体/简繁 preference.
- Batch/collection items are filtered (D11).
- Confidence-poor / multi-season -> ambiguous, never silently pick (D10, P1-10).
"""

from __future__ import annotations

import re

from spica.anime.models import (
    LATEST,
    AnimeCandidate,
    EpisodeRef,
    MatchResult,
    SourceTitle,
    episode_key,
)
from spica.anime.title_aliases import (
    DEFAULT_TITLE_ALIASES,
    TitleAliases,
    normalize_title_identity,
    normalized_title_alias_groups,
)

# -- Chinese numerals -> int (covers 0-99, enough for episode/season) ---------

_CN_DIGIT = {"〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_ROMAN = {"Ⅱ": 2, "Ⅲ": 3, "Ⅳ": 4, "Ⅴ": 5, "II": 2, "III": 3, "IV": 4}


def cn_to_int(s: str) -> int | None:
    """「三」->3, 「十」->10, 「十二」->12, 「二十一」->21. Arabic passes through."""
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s in _ROMAN:
        return _ROMAN[s]
    if "十" not in s:
        if len(s) == 1 and s in _CN_DIGIT:
            return _CN_DIGIT[s]
        # multi-digit spelled out (rare): 一二 -> not a real number, bail
        return _CN_DIGIT.get(s)
    # has 十
    left, _, right = s.partition("十")
    tens = _CN_DIGIT.get(left, 1) if left else 1
    ones = _CN_DIGIT.get(right, 0) if right else 0
    return tens * 10 + ones


# season markers, longest/most-specific first so consumption is greedy-correct.
_SEASON_PATTERNS = [
    (re.compile(r"第\s*([0-9〇零一二三四五六七八九十]+)\s*[季期部]"), "cn"),
    (re.compile(r"([0-9]+)\s*(?:期|nd Season|rd Season|th Season|st Season)", re.I), "arabic"),
    (re.compile(r"(?<![A-Za-z])S(\d+)"), "arabic"),  # S3, S3E1 (not "Season")
    (re.compile(r"(?<![A-Za-z])(Ⅱ|Ⅲ|Ⅳ|Ⅴ)"), "roman"),
    (re.compile(r"\b(\d)(?:nd|rd|th|st)\s+Season\b", re.I), "arabic"),
    # ASCII roman (F10): longest first so III isn't eaten by II; letter-isolated
    # on both sides so ASCII / XIV / ViuTV never match. Lowest priority.
    (re.compile(r"(?<![A-Za-z])(III|II|IV)(?![A-Za-zⅠ-Ⅴ])"), "roman"),
]

# episode markers (applied AFTER season markers are stripped).
_EP_PATTERNS = [
    re.compile(r"第\s*([0-9〇零一二三四五六七八九十]+)\s*[话話集]"),
    re.compile(r"(?:^|\s)-\s*(\d{1,3})(?:v\d)?(?=\D|$)"),
    # dual numbering 「[13(85)]」= season-internal 13 + absolute 85 -> take the
    # season-internal number 13 (search-quality §2.2). MUST precede the generic
    # 「[NN]」pattern below (which anyway stops at the「(」and never matches this).
    re.compile(r"\[\s*(\d{1,3})\s*\(\s*\d{1,4}\s*\)\s*\]"),
    re.compile(r"\[\s*(\d{1,3})(?:v\d)?\s*\]"),
    re.compile(r"(?:EP?|Episode)\s*(\d{1,3})", re.I),
]

_LATEST_RE = re.compile(r"最新(?:的)?(?:一)?[话話集集]?|latest", re.I)
# The model normally forwards only the anime query, but typed/voice tool
# arguments can retain the assistant wake word.  Strip it only at the beginning
# and only when separated or followed by a watch lead-in; an anime title merely
# containing "spica" elsewhere remains untouched.
_WAKE_PREFIX_RE = re.compile(
    r"^\s*spica(?:\s*[,，:：、]\s*|\s+|(?=(?:我想看|想看|看|播放|放)))",
    re.I,
)

# Explicit batch/collection keyword markers (mikan multi-episode torrents, D11).
_BATCH_KEYWORD_RE = re.compile(
    r"全\d+[话話集]|Complete|Fin\b|BD-?BOX|合集", re.I,
)
# An episode RANGE like「01-12」/「1-12」. The leading (?<![A-Za-z0-9]) drops
# season-episode「S3 - 01」(the「3」is preceded by「S」) AND year spans like
# 「2024-25」whose「024-25」submatch would otherwise fire (F12); the trailing
# negative lookahead drops sports-style「24-25赛季/年度」spans; a code-level
# ascending check (left < right) rejects incidental pairs like「H-264」/「2-0」
# (finding #7). Keep in sync with bilibili_search._RANGE_RE (F12).
_RANGE_RE = re.compile(
    r"(?<![A-Za-z0-9])(\d{1,3})\s*-\s*(\d{1,3})(?![0-9A-Za-z])(?!\s*(?:赛季|年度))")

# Non-episodic specials: OVA/OAD/SP/剧场版/movie/总集篇/特别篇 (finding #5).
_SPECIAL_RE = re.compile(
    r"\bO[VA]?[AD]\b|\bSP\b|\bSpecial\b|剧场版|劇場版|\bMovie\b|总集篇|總集篇|特别篇|特別篇|前传|前傳",
    re.I,
)

_LEADING_BRACKET_RE = re.compile(r"^\s*([\[【])([^\]】]*)[\]】]")
_FULLWIDTH_BRACKET_RE = re.compile(r"【([^】]*)】")
_BRACKET_GROUP_RE = re.compile(r"[\[【（(][^\]】）)]*[\]】）)]")
_QUALITY_RE = re.compile(r"(2160p|1080p|720p|480p)", re.I)
# Whole-season carrier titles often append mutable release metadata after the
# actual anime name.  It must not participate in title identity: otherwise
# 「全13话…周更」and「全12集…更新中」from two trusted uploaders become two
# different "anime" and the resolver raises a false ambiguity.
_COLLECTION_RELEASE_METADATA_TOKEN = (
    r"(?:全|共)\s*\d{1,3}\s*[话話集]|"
    r"(?:4K|超清|高清)?中字|"
    r"未(?:删|刪)减版|"
    r"每(?:周|週)更新中?|(?:周|週)更中?|"
    r"(?:持续|持續)更新中?|更新中|(?:连载|連載)中|完结|完結"
)
_TRAILING_COLLECTION_RELEASE_METADATA_RE = re.compile(
    rf"[\s·|/，,:：-]+(?:{_COLLECTION_RELEASE_METADATA_TOKEN})"
    rf"(?:[\s·|/，,:：-]*(?:{_COLLECTION_RELEASE_METADATA_TOKEN}))*"
    r"[\s·|/，,:：-]*$",
    re.I,
)

# A fullwidth 【..】 that is a TAG (quality/quarter/合集/…) vs a subgroup NAME vs
# the anime TITLE itself (review tail #2). Halfwidth [..] is always a subgroup
# (mikan convention). Tokens joined by / or 、 are all-tag -> a tag bracket.
_TAG_TOKEN_RE = re.compile(
    r"^(4k超清|4k|超清|1080p|720p|2160p|480p|蓝光|藍光|bd|hevc|10bit|"
    r"\d{1,2}月(?:新番)?|合集|每周更新|完结|完結|生肉|熟肉|新番|更新|先行|附特典)$"
)
_SUBGROUP_SUFFIX_RE = re.compile(
    r"(字幕组|字幕社|工作室|制作组|製作組|练习组|練習組|汉化组|漢化組|发布组|發佈組|压制组|壓制組)$"
)
# A subtitle / quality / release-descriptor token: a bracket ``/``-segment that
# matches is a TAG, never an anime title. Split by script so ASCII tags match only
# as WHOLE tokens: a bare search() for short codes ("SC"/"TC"/"GB") would flag
# English alias names whose letters merely CONTAIN them -- "School Days" (sc),
# "Witch Watch" (tc) -- and drop the real title (review follow-up). CJK descriptors
# stay substring-matched (they rarely collide with a real title).
_SEG_TAG_CJK_RE = re.compile(
    r"蓝光|藍光|简繁|简体|繁体|简中|繁中|简日|繁日|双语|字幕|"
    r"内封|內封|内嵌|內嵌|外挂|外掛|生肉|熟肉|招募")
# Matched with fullmatch() against each [A-Za-z0-9]+ token of the segment -- all
# unambiguous quality/codec descriptors (no 2-letter subtitle codes here, so no
# collision with real English titles).
_SEG_TAG_ASCII_RE = re.compile(
    r"\d{3,4}p|4k|webrip|bdrip|hevc|avc|aac|flac|nvenc|x26[45]|\d+bit",
    re.I)
_RELEASE_PREFIX_TOKEN_RE = re.compile(
    r"(?:真\s*)?4k|超清|高清|蓝光|藍光|中字|正式版|先行版|完整版|"
    r"无删减版|無刪減版|未删减版|未刪減版",
    re.I,
)


def _leading_bracket_kind(open_br: str, content: str) -> str:
    """Classify a leading bracket: 'subgroup' (drop + record), 'tag' (drop), or
    'title' (keep -- the 【..】 holds the anime name, review tail #2)."""
    c = content.strip()
    if open_br == "[":
        return "subgroup"                    # halfwidth = mikan subgroup
    if not c:
        return "tag"
    if _SUBGROUP_SUFFIX_RE.search(c):
        return "subgroup"                    # 【某某字幕组】
    tokens = [t for t in re.split(r"[/、,，\s]+", c) if t]
    if tokens and all(_TAG_TOKEN_RE.match(t.lower()) for t in tokens):
        return "tag"                         # 【4K超清】/【7月/合集】
    return "title"                           # 【黑猫与魔女的教室】/【摩绪】


def _detect_batch(title: str) -> bool:
    """A multi-episode range or explicit collection marker (finding #7)."""
    if _BATCH_KEYWORD_RE.search(title):
        return True
    for m in _RANGE_RE.finditer(title):
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo < hi:  # a real ascending episode range, not「H-264」
            return True
    return False


def _is_tag_segment(seg: str) -> bool:
    """A bracket ``/``-segment that is a subtitle / quality / subgroup TAG, not an
    anime title (§2.1 guard). CJK descriptors match as substrings; ASCII tags match
    only as WHOLE tokens, so English alias names (School Days / Witch Watch) whose
    letters merely CONTAIN a short code are not misflagged (review follow-up)."""
    s = seg.strip()
    if not s:
        return True
    if _SEG_TAG_CJK_RE.search(s) or _SUBGROUP_SUFFIX_RE.search(s):
        return True
    return any(_SEG_TAG_ASCII_RE.fullmatch(tok)
               for tok in re.findall(r"[A-Za-z0-9]+", s))


def _is_release_prefix(text: str) -> bool:
    """True when text before a Bilibili ``【title】`` is release metadata only."""
    if not text.strip():
        return False
    remainder = _RELEASE_PREFIX_TOKEN_RE.sub("", text)
    return not re.sub(r"[\W_]+", "", remainder, flags=re.UNICODE)


def _select_name_source(after_season: str) -> str:
    """Search-quality §2.1: the 「【组】★促销★[中文 / 別名 / English]」form hides the
    real title inside a LATER [..] whose content is a「/」-joined alias list, while
    the leading text is the subgroup + promo (so name extraction picks up the组名
    instead and the two release forms of one anime split into false ambiguity).

    Prefer that bracket as the name source, but ONLY when it is unambiguously a
    title-alias list -- otherwise return ``after_season`` untouched (the ordinary
    extraction is the SAFE default; a wrong pick silently loses name_matches):
    - the bracket content has a「/」and a CJK char (an alias list, not「[1080p]」), and
    - the text BEFORE it has no「/」(a normal「中文 / Romaji」title already flows
      through the free-text split unchanged), and
    - NO「/」-segment is a subtitle/quality/subgroup tag, so 「[繁日双语 / 1080p]」/
      「[简繁日内封字幕 / 1080p]」/「[字幕组 / 1080p]」(≥3 CJK yet a tag) are excluded, and
    - the first「/」-segment has ≥3 CJK chars, so short tags like「[简/繁]」stay out."""
    for m in _BRACKET_GROUP_RE.finditer(after_season):
        inner = m.group(0)[1:-1]
        if "/" not in inner or not re.search(r"[一-鿿]", inner):
            continue
        if "/" in after_season[:m.start()]:
            continue                      # leading text is already a title
        segs = [s.strip() for s in inner.split("/")]
        if any(_is_tag_segment(s) for s in segs):
            continue                      # a subtitle/quality/tag bracket, not a title
        if len(re.findall(r"[一-鿿]", segs[0])) >= 3:
            return inner
    # Bilibili carrier uploads also put release metadata BEFORE a later title
    # bracket: 「真4K【作品名】…」/「正式版【作品名】…」.  The ordinary
    # name-body pass removes every bracket and would retain only「真4K」or
    # 「正式版」, splitting one anime into false title clusters.  Prefer the
    # bracket only when everything before it is a closed set of release tokens;
    # real leading text such as「ED纯享版：」therefore remains untouched.
    for m in _FULLWIDTH_BRACKET_RE.finditer(after_season):
        inner = m.group(1).strip()
        if (_is_release_prefix(after_season[:m.start()])
                and _leading_bracket_kind("【", inner) == "title"):
            return inner
    return after_season


def _extract_season(text: str) -> tuple[int | None, str]:
    """Return (season, text-with-season-marker-removed)."""
    for pat, kind in _SEASON_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        tok = m.group(1)
        val = cn_to_int(tok) if kind in ("cn", "arabic") else _ROMAN.get(tok)
        if val is not None:
            text = text[:m.start()] + " " + text[m.end():]
            return val, text
    return None, text


def _extract_episode(text: str) -> int | None:
    for pat in _EP_PATTERNS:
        m = pat.search(text)
        if m:
            v = cn_to_int(m.group(1))
            if v is not None:
                return v
    return None


def _extract_subtitle(text: str) -> str | None:
    if "简繁" in text:
        return "简繁"
    if re.search(r"简体|简中|CHS|GB(?![A-Za-z])|SC\b", text):
        return "简体"
    if re.search(r"繁体|繁中|CHT|BIG5|TC\b", text):
        return "繁体"
    return None


# -- user query --------------------------------------------------------------

def parse_query(text: str) -> EpisodeRef:
    """「无职转生第三季第一集」-> EpisodeRef(title_query='无职转生', season=3, episode=1)."""
    raw = _WAKE_PREFIX_RE.sub("", text.strip(), count=1)
    season, work = _extract_season(raw)
    episode: int | str | None
    if _LATEST_RE.search(work):
        episode = LATEST
        work = _LATEST_RE.sub(" ", work)
    else:
        episode = _extract_episode(work)
        # strip the episode marker from the name
        for pat in _EP_PATTERNS:
            work = pat.sub(" ", work)
    # strip common lead-ins / trailing quality words from the name
    work = re.sub(r"^(我想看|想看|看|播放|放)\s*", "", work)
    work = _QUALITY_RE.sub(" ", work)
    # Repeated spoken episode markers commonly leave an empty pair behind:
    # 「第四集（第四话）」->「（）」 after both markers are consumed.  It is not
    # part of the title and degrades external search quality.
    work = re.sub(r"[（(]\s*[）)]", " ", work)
    # Preserve an intentional internal boundary between alternate titles or
    # translated names. Name identity still goes through _norm(), which removes
    # whitespace, while external search gets the useful token boundary.
    title = re.sub(r"\s+", " ", work).strip("　 ·/-")
    return EpisodeRef(title_query=title, season=season, episode=episode)


# -- source title ------------------------------------------------------------

def parse_source_title(title: str) -> SourceTitle:
    """Parse a mikan/bilibili title. Season is consumed before episode (so「第3季」
    doesn't steal the episode); season+episode are extracted from the raw text
    (minus the leading subgroup) so a bracketed「[02]」episode still parses -- the
    earlier version pre-stripped ALL brackets and could never see it (finding #6).
    """
    raw = title
    subgroup = None
    work = title
    # Classify the leading bracket: a mikan [subgroup] / a bilibili 【tag】 is
    # dropped; a 【title】 is unwrapped so its name+season flow into parsing
    # (review tail #2). Other brackets (e.g.「[02]」) are kept for episode parse.
    m = _LEADING_BRACKET_RE.match(title)
    if m:
        open_br, content = m.group(1), m.group(2)
        kind = _leading_bracket_kind(open_br, content)
        if kind == "subgroup":
            subgroup = content.strip() or None
            work = title[m.end():]
        elif kind == "tag":
            work = title[m.end():]
        else:  # title -> unwrap the 【..】, keep its content in the name
            work = content + " " + title[m.end():]

    is_batch = _detect_batch(title)
    is_special = bool(_SPECIAL_RE.search(title))
    quality = None
    mq = _QUALITY_RE.search(title)
    if mq:
        quality = mq.group(1).lower()
    subtitle = _extract_subtitle(title)

    season, after_season = _extract_season(work)
    episode = None if is_special else _extract_episode(after_season)

    # name source: normally ``after_season``, but the 「【组】★促销★[中文 / 別名]」
    # form (search-quality §2.1) buries the real title in a later bracket -- prefer
    # it so both release forms of one anime cluster (not falsely ambiguous).
    name_src = _select_name_source(after_season)
    # name-only working copy: NOW drop remaining bracket groups + episode markers.
    name_body = _BRACKET_GROUP_RE.sub(" ", name_src)
    for pat in _EP_PATTERNS:
        name_body = pat.sub(" ", name_body)
    name_body = _TRAILING_COLLECTION_RELEASE_METADATA_RE.sub(" ", name_body)
    name_zh, name_ja = name_body, ""
    if "/" in name_body:
        left, _, right = name_body.partition("/")
        name_zh, name_ja = left.strip(), right.strip()
    return SourceTitle(
        raw=raw, name_zh=_clean_name(name_zh), name_ja=_clean_name(name_ja),
        season=season, episode=episode, quality=quality, subtitle=subtitle,
        subgroup=subgroup, is_batch=is_batch, is_special=is_special,
    )


def part_source_title(
    collection_raw: str, episode: int, *, season: int | None = None,
    quality: str | None = None, subtitle: str | None = None,
    subgroup: str | None = None,
) -> SourceTitle:
    """Build a SINGLE-episode SourceTitle for one bilibili 分P of a season
    collection (finding #1). The collection's raw title may contain a range like
    「01-02话」, but this part is one concrete episode -> is_batch=False, episode
    pinned, so it is never dropped by the batch filter. Season/quality/subtitle
    fall back to parsing the collection title when not given by the adapter.

    The collection range fragment (「01-02话」) is stripped from the name so the
    per-part name matches the single-episode name mikan produces -- otherwise the
    dedup episode_key would differ across sources (review tail #1).

    Precondition (F1): the caller must NOT call this for a special (总集篇/OVA/
    剧场版) video -- the adapter skips those at expansion entry; is_special=False
    here relies on that filter."""
    parsed = parse_source_title(collection_raw)
    return SourceTitle(
        raw=collection_raw,
        name_zh=_strip_range_fragment(parsed.name_zh),
        name_ja=_strip_range_fragment(parsed.name_ja),
        season=season if season is not None else parsed.season,
        episode=episode,
        quality=quality if quality is not None else parsed.quality,
        subtitle=subtitle if subtitle is not None else parsed.subtitle,
        subgroup=subgroup if subgroup is not None else parsed.subgroup,
        is_batch=False, is_special=False,
    )


# a leftover collection range in a name, e.g.「01-02话」/「01-12」 (review tail #1).
_NAME_RANGE_RE = re.compile(r"\d{1,3}\s*-\s*\d{1,3}\s*[话話集]?")


def _strip_range_fragment(name: str) -> str:
    return _clean_name(_NAME_RANGE_RE.sub(" ", name))


def _clean_name(s: str) -> str:
    s = re.sub(r"[～~]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip("　 ·-/")


def _norm(s: str) -> str:
    """Normalize for fuzzy name compare: drop spaces/punct, lowercase."""
    return normalize_title_identity(s)


def _alias_groups(
    title_aliases: TitleAliases | None,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    aliases = DEFAULT_TITLE_ALIASES if title_aliases is None else title_aliases
    return normalized_title_alias_groups(aliases)


def title_alias_variants(
    title: str,
    *,
    title_aliases: TitleAliases | None = None,
) -> tuple[str, ...]:
    """Return the spoken title followed by configured aliases for source search."""
    aliases = DEFAULT_TITLE_ALIASES if title_aliases is None else title_aliases
    normalized_title_alias_groups(aliases)
    query_norm = _norm(title)
    variants = [title]
    seen = {query_norm}
    for canonical, values in aliases.items():
        group = (canonical, *values)
        if not any(
            member_norm and member_norm in query_norm
            for member_norm in (_norm(member) for member in group)
        ):
            continue
        for member in group:
            member_norm = _norm(member)
            if member_norm and member_norm not in seen:
                variants.append(member)
                seen.add(member_norm)
    return tuple(variants)


def _canon(norm_name: str, title_aliases: TitleAliases | None = None) -> str:
    """Fold alias-group members to their canonical form so zh/ja/romaji names of
    one anime compare equal. Shared identity basis for name_matches AND
    _cluster_by_title (review tail #1)."""
    for canonical, group in _alias_groups(title_aliases):
        for member in group[1:]:
            if member in norm_name:
                norm_name = norm_name.replace(member, canonical)
        # A user may state both the canonical title and one alias together;
        # replacement then yields the canonical twice back-to-back.
        doubled = canonical + canonical
        while doubled in norm_name:
            norm_name = norm_name.replace(doubled, canonical)
    return norm_name


def _canon_title_for_key(
    title: str,
    title_aliases: TitleAliases | None = None,
) -> str:
    """Identity basis for the dedup key (F2 tail). Alias-fold, THEN collapse a
    title that merely *contains* an alias group's canonical -- a full name like
    「无职转生，到了异世界就拿出真本事」 -> 「无职转生」 -- so different wordings
    of one anime across SEPARATE user queries share one library key. This mirrors
    the substring basis ``_same_anime`` already uses for matching, so two titles
    that MATCH as the same anime also KEY the same (still bounded by
    the configured alias map: a group must exist for the fold to reach across
    zh/ja/romaji)."""
    norm = _canon(_norm(title), title_aliases)
    for canonical, _group in _alias_groups(title_aliases):
        if canonical in norm:
            return canonical
    return norm


def canonical_episode_key(title_query: str, season: int | None,
                          episode: int, *,
                          title_aliases: TitleAliases | None = None) -> str:
    """The SINGLE dedup-key generation point (F2). Identity basis is the
    alias-folded USER QUERY title (``_canon_title_for_key``) -- never a source
    title -- so the same episode keys identically across the query fast path
    (watch_flow), the coordinator overwrite after materialize, and every source
    adapter, as well as across reworded queries for one anime."""
    return episode_key(
        _canon_title_for_key(title_query, title_aliases),
        season,
        episode,
    )


def _same_anime(
    a_norm: str,
    b_norm: str,
    title_aliases: TitleAliases | None = None,
) -> bool:
    """True if two normalized names denote the same anime: alias-folded, one a
    substring of the other (short「无职转生」vs full「…到了异世界…」/ zh vs ja)."""
    ca = _canon(a_norm, title_aliases)
    cb = _canon(b_norm, title_aliases)
    return bool(ca) and bool(cb) and (ca in cb or cb in ca)


def name_matches(
    query: str,
    st: SourceTitle,
    *,
    title_aliases: TitleAliases | None = None,
) -> bool:
    q = _norm(query)
    if not q:
        return False
    return any(_same_anime(q, _norm(name), title_aliases)
               for name in (st.name_zh, st.name_ja, st.raw) if _norm(name))


# -- ranking + resolve -------------------------------------------------------

def _quality_rank(q: str | None, pref: str) -> int:
    order = {"1080p": 0, "2160p": 1, "720p": 2, "480p": 3, None: 4}
    if pref == "1080p":
        return order.get(q, 4)
    # simple: exact pref first, then by descending resolution
    return order.get(q, 4)


def _subtitle_rank(sub: str | None, pref: list[str]) -> int:
    for i, p in enumerate(pref):
        if sub == p:
            return i
    # 繁体-only is worst under a 简 preference
    if sub == "繁体":
        return len(pref) + 1
    return len(pref)


def rank_candidates(
    cands: list[AnimeCandidate], quality: str, subtitle_pref: list[str]
) -> list[AnimeCandidate]:
    return sorted(
        cands,
        key=lambda c: (
            _quality_rank(c.parsed.quality, quality),
            _subtitle_rank(c.parsed.subtitle, subtitle_pref),
            len(c.parsed.raw),  # tie-break: shorter/cleaner title
        ),
    )


def resolve(
    ref: EpisodeRef,
    candidates: list[AnimeCandidate],
    *,
    quality: str = "1080p",
    subtitle_pref: list[str] | None = None,
    title_aliases: TitleAliases | None = None,
) -> MatchResult:
    subtitle_pref = subtitle_pref or ["简繁", "简体"]

    # 1) name filter + batch drop (D11) + special drop (finding #5)
    named = [c for c in candidates
             if name_matches(
                 ref.title_query,
                 c.parsed,
                 title_aliases=title_aliases,
             )
             and not c.parsed.is_batch and not c.parsed.is_special]
    if not named:
        return MatchResult(status="none", reason="no title match")

    # confidence gate (finding #4): a very short query that pulls in several
    # DIFFERENT anime is too weak to pick from -> ask instead of guessing.
    if (len(_norm(ref.title_query)) < 2
            and len(_cluster_by_title(named, title_aliases)) > 1):
        return MatchResult(status="ambiguous", candidates=tuple(named[:5]),
                           reason="query too short / low confidence")

    # 2) season filter. A missing season marker means S1 (eff_season), so
    # 「无职转生」(no marker) does NOT silently match a 第三季 request (P1-10).
    def eff_season(c: AnimeCandidate) -> int:
        return c.parsed.season if c.parsed.season is not None else 1

    if ref.season is not None:
        pool = [c for c in named if eff_season(c) == ref.season]
    else:
        pool = named
    if not pool:
        return MatchResult(status="none", reason="season not offered")

    # season ambiguity: user didn't pin a season and candidates span seasons
    seasons = sorted({eff_season(c) for c in pool})
    if ref.season is None and len(seasons) > 1:
        reps = tuple(rank_candidates([c for c in pool if eff_season(c) == s],
                                     quality, subtitle_pref)[0] for s in seasons)
        return MatchResult(status="ambiguous", candidates=reps,
                           reason=f"multiple seasons: {seasons}")

    # 3) episode resolve
    if ref.episode == LATEST:
        # cross-title gate BEFORE max() (P2-1): max() over the whole pool would
        # collapse several DIFFERENT anime onto whichever offers the highest
        # episode number -> a silent wrong match. Cluster first; a pool spanning
        # distinct anime is ambiguous, never a guess (D10/P1-10). One anime across
        # subgroups is a single cluster and falls through to the max() below.
        clusters = _cluster_by_title(pool, title_aliases)
        if len(clusters) > 1:
            reps = tuple(rank_candidates(m, quality, subtitle_pref)[0]
                         for m in clusters)
            return MatchResult(status="ambiguous", candidates=reps,
                               reason="multiple distinct titles matched")
        eps = [c.parsed.episode for c in pool if c.parsed.episode is not None]
        if not eps:
            return MatchResult(status="none", reason="no dated episode to call latest")
        target = max(eps)
    elif isinstance(ref.episode, int):
        target = ref.episode
    else:
        # user gave no episode -> ask (never guess, P1-11)
        return MatchResult(status="need_episode", reason="episode unspecified")

    ep_pool = [c for c in pool if c.parsed.episode == target]
    if not ep_pool:
        return MatchResult(status="none", reason=f"episode {target} not found")

    # confidence gate (finding #4): if the surviving candidates are for several
    # DIFFERENT anime (name collision), don't silently rank -- surface for
    # confirmation. Same anime across subgroups (one name a substring of the
    # other, e.g. short vs full title) clusters together and is ranked below.
    clusters = _cluster_by_title(ep_pool, title_aliases)
    if len(clusters) > 1:
        reps = tuple(rank_candidates(m, quality, subtitle_pref)[0]
                     for m in clusters)
        return MatchResult(status="ambiguous", candidates=reps,
                           reason="multiple distinct titles matched")

    # 4) rank surviving subgroups, pick best
    best = rank_candidates(ep_pool, quality, subtitle_pref)[0]
    return MatchResult(status="matched", chosen=best,
                       reason=f"season={ref.season} ep={target}")


def _cluster_by_title(
    cands: list[AnimeCandidate],
    title_aliases: TitleAliases | None = None,
) -> list[list[AnimeCandidate]]:
    """Group candidates by anime identity via the SHARED ``_same_anime`` basis
    (review tail #1): alias-folded, one name a substring of the other. So
    「无职转生 3期」and「無職転生 S3」cluster together (not falsely ambiguous)."""
    clusters: list[tuple[str, list[AnimeCandidate]]] = []
    for c in cands:
        n = _norm(c.parsed.name_zh) or _norm(c.parsed.raw)
        placed = False
        for i, (rep, members) in enumerate(clusters):
            if _same_anime(n, rep, title_aliases):
                members.append(c)
                if (len(_canon(n, title_aliases))
                        < len(_canon(rep, title_aliases))):  # keep shortest as rep
                    clusters[i] = (n, members)
                placed = True
                break
        if not placed:
            clusters.append((n, [c]))
    return [members for _, members in clusters]
