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

import re
from dataclasses import dataclass
from enum import Enum
from typing import Literal

# Filesystem-reserved characters (Windows superset of POSIX) that must never
# reach a directory name -- includes the path separators, so the result of
# anime_dirname is always ONE safe component (no traversal).
_UNSAFE_DIRCHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
# Windows reserves these device names as a WHOLE component (case-insensitive,
# with or without an extension: "CON", "AUX.txt" are both rejected).
_WIN_RESERVED = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
})
# Byte budget (not char count) -- one path component is usually capped at 255
# bytes, and CJK is 3 bytes/char, so a 120-CHAR title would be 360 bytes.
_DIRNAME_MAX_BYTES = 200


class DownloadTerminalOwner(str, Enum):
    """The single worker-side owner of a terminal decision."""

    RUNNING = "running"
    MANUAL_CANCEL = "manual_cancel"
    STALL_CANCEL = "stall_cancel"
    SHUTDOWN_PRESERVE = "shutdown_preserve"
    COMPLETED = "completed"
    FAILED = "failed"


class DownloadTerminalResult(str, Enum):
    """Observable terminal outcome, deliberately separate from ownership."""

    COMPLETED = "completed"
    CANCELLED = "cancelled"
    UNCONFIRMED = "unconfirmed"
    FAILED = "failed"
    PRESERVED = "preserved"


class DownloadTerminalCause(str, Enum):
    """Why the terminal outcome was reached."""

    NORMAL = "normal"
    MANUAL = "manual"
    STALL = "stall"
    SHUTDOWN = "shutdown"


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate to at most ``max_bytes`` UTF-8 bytes without splitting a codepoint."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", "ignore")

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


def anime_dirname(name: str) -> str:
    """One filesystem-safe directory component for an anime's downloads, so the
    cache is grouped ``<download_dir>/<anime>/<episode>`` instead of flat.

    Order (each step's guarantee matters): strip separators + reserved/control
    chars -> collapse whitespace -> rewrite a Windows reserved device name
    (``CON``/``AUX.txt``/``COM1``...) by prefixing ``_`` (BEFORE truncation, so the
    prefix stays inside the budget) -> truncate to a UTF-8 BYTE budget (not chars,
    so CJK titles stay under the 255-byte component limit) -> THEN strip trailing
    dots/spaces (Windows rejects them, and truncation can re-expose one) -> empty
    / lone ``.``/``..`` -> ``"未命名"``.

    The return value is ALWAYS a single component: no separator, no ``.``/``..``,
    within the byte budget, not a reserved name. Callers still resolve +
    containment-check before use (defence in depth), but this alone cannot
    produce a traversal or an unusable name."""
    cleaned = _UNSAFE_DIRCHARS.sub("", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # reserved-name rewrite BEFORE truncation so the "_" prefix is inside the byte
    # budget (prefixing after a 200-byte truncation would yield 201 bytes).
    if cleaned and cleaned.split(".", 1)[0].lower() in _WIN_RESERVED:
        cleaned = "_" + cleaned
    cleaned = _truncate_utf8(cleaned, _DIRNAME_MAX_BYTES)
    cleaned = cleaned.strip(". ")                     # after truncation (Windows)
    if not cleaned or cleaned in (".", ".."):
        return "未命名"
    return cleaned


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
    # Optional adapter-owned URL used only by ``materialize`` for the one chosen
    # candidate.  It is never handed to the download worker.
    materialize_url: str = ""
    # Origin of the RSS response that supplied ``materialize_url``. Mikan uses
    # it to require the selected torrent to be same-origin, even when multiple
    # configured mirrors are otherwise trusted.
    materialize_origin: str = ""

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
    # Base64 is a primitive so a small, verified .torrent can cross RuntimeEvent
    # boundaries without exposing an arbitrary URL or filesystem path to qbt.
    torrent_payload_b64: str | None = None


@dataclass(frozen=True)
class DownloadStatus:
    """A torrent/download task's live state, as the port reports it."""

    task_id: str
    state: str                      # "metadata" | "downloading" | "completed" | "stalled" | "error"
    progress: float = 0.0           # 0.0..1.0
    save_path: str | None = None    # final file once completed
    error: str | None = None
    # qBittorrent's Unix epoch for the task's last real activity.  Optional so
    # other TorrentClientPort adapters do not have to manufacture wall-clock
    # data they cannot observe.
    last_activity_at: float | None = None

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
