"""Anime-watch domain data models (Phase 1).

Plain stdlib dataclasses -- pure data, NO network, NO Qt (CLAUDE.md #1).

Two kinds of "parsed" object, deliberately separate:
- ``EpisodeRef``    -- what the USER asked for (from the tool query), e.g.
                       「无职转生第三季第一集」 -> title_query="无职转生", season=3, episode=1.
- ``SourceTitle``   -- what a SOURCE offers (a mikan RSS item title / a bilibili
                       video title), parsed for matching + ranking.

``episode`` uses the sentinel ``LATEST`` for「最新一集」(D10). ``None`` means the
user gave no episode -> the tool layer asks (P1-11); the resolver never guesses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# 「最新一集」sentinel (D10). Kept a plain str so it survives JSON / the tool
# schema's ``episode: "latest"`` value verbatim.
LATEST = "latest"

# episode as the user expressed it: a concrete number, the LATEST sentinel, or
# unspecified -- constrained so an arbitrary string can't leak in (review tail #5).
EpisodeSpec = int | Literal["latest"] | None

# resolve outcomes (finding #9: pin the string set instead of free-form str).
MatchStatus = Literal["matched", "ambiguous", "need_episode", "none"]


def episode_key(title: str, season: int | None, episode: int) -> str:
    """Stable dedup key shared by the library and coordinator. Season ``None``
    normalizes to 1 (matches the resolver's eff_season)."""
    return f"{title.strip().lower()}|s{season or 1}|e{episode}"


@dataclass(frozen=True)
class EpisodeRef:
    """Normalized user request. ``title_query`` is the anime name only (season /
    episode markers stripped)."""

    title_query: str
    season: int | None = None
    episode: EpisodeSpec = None

    @property
    def is_latest(self) -> bool:
        return self.episode == LATEST


@dataclass(frozen=True)
class SourceTitle:
    """A source-offered item parsed for matching. ``name_zh``/``name_ja`` are
    best-effort (many titles are「中文名 / Romaji」split by '/')."""

    raw: str
    name_zh: str = ""
    name_ja: str = ""
    season: int | None = None
    episode: int | None = None
    quality: str | None = None      # "2160p" | "1080p" | "720p" | None
    subtitle: str | None = None     # "简繁" | "简体" | "繁体" | None
    subgroup: str | None = None
    # is_batch: a multi-episode range item (e.g. mikan「01-12」torrent) -> filtered
    # in v1 (D11). A bilibili collection is NOT a batch: the adapter expands it
    # into per-part single-episode candidates (see resolver.part_source_title),
    # so it carries is_batch=False and a concrete episode (finding #1).
    is_batch: bool = False
    # is_special: OVA / SP / 剧场版 / 总集篇 / 特别篇 -- non-episodic, not matched
    # by a「第N集」request in v1 (finding #5).
    is_special: bool = False


@dataclass(frozen=True)
class AnimeCandidate:
    """A concrete downloadable an adapter surfaced, carrying enough to (a) match,
    (b) rank, (c) hand to the download worker. ``locator`` is source-specific:
    a magnet URI (mikan) or a "bvid:part" string (bilibili)."""

    source: str                     # "bilibili" | "mikan"
    locator: str                    # magnet:?... | BV...:1
    parsed: SourceTitle
    size_bytes: int | None = None
    display_title: str = ""

    @property
    def is_magnet(self) -> bool:
        return self.locator.startswith("magnet:?")


@dataclass(frozen=True)
class AnimeResource:
    """A resolved, downloadable episode handed to the download worker. ``locator``
    is a magnet URI (mikan) or "bvid:part" (bilibili). ``episode_key`` uniquely
    names the episode for library dedup/lookup."""

    episode_key: str                # e.g. "无职转生|s3|e1"
    source: str
    locator: str
    display_title: str = ""
    size_bytes: int | None = None


@dataclass(frozen=True)
class DownloadStatus:
    """A torrent/download task's live state, as the port reports it."""

    task_id: str
    state: str                      # "downloading" | "completed" | "stalled" | "error"
    progress: float = 0.0           # 0.0..1.0
    save_path: str | None = None    # final file once completed
    error: str | None = None

    @property
    def is_done(self) -> bool:
        return self.state == "completed"


@dataclass(frozen=True)
class MatchResult:
    """Outcome of resolving an EpisodeRef against source candidates.

    - status="matched":   ``chosen`` is the single best candidate.
    - status="ambiguous": ``candidates`` are distinct interpretations (e.g.
                          different seasons) to surface for user confirmation.
    - status="need_episode": user gave no episode -> ask which.
    - status="none":      nothing matched.
    """

    status: MatchStatus
    chosen: AnimeCandidate | None = None
    # tuple so the frozen result is fully immutable (finding #9).
    candidates: tuple[AnimeCandidate, ...] = ()
    reason: str = ""
