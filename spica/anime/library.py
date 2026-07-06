"""Local download library (Phase 1) -- dedup index, disk accounting, reconcile.

Pure data + logic (no Qt, no network). Persistence is plain JSON via
``to_json``/``from_json``; the host owns the single write point (P1-6) and calls
these -- the library object itself does no file I/O so it stays unit-testable.

Responsibilities:
- dedup / lookup: has this episode already been downloaded? (query hit -> play,
  no re-download).
- "most-recent completed, not yet played" pointer (P1-11): lets 「放吧」play the
  right episode even when the LLM can't reconstruct the exact EpisodeRef.
- disk accounting + over-limit check (D6): never auto-delete, just report.
- reconcile (P1-9): given qbt-reported completed tasks not yet in the library,
  return the entries to register. This is REGISTER-ONLY -- it never triggers
  playback (a reconciled task of unknown age must never auto-play).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Iterable

# shared with the coordinator -- single definition (finding #9).
from spica.anime.models import episode_key  # noqa: F401  (re-exported)


def _utc_now_iso() -> str:
    """Timezone-aware UTC ISO-8601 (finding #9): naive utcnow() is deprecated and
    the '+00:00' suffix keeps string-sorted timestamps unambiguous."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class LibraryEntry:
    episode_key: str
    title: str
    season: int
    episode: int
    file_path: str
    size_bytes: int
    source: str
    added_at: str = field(default_factory=_utc_now_iso)
    played: bool = False


class AnimeLibrary:
    def __init__(self, entries: Iterable[LibraryEntry] | None = None) -> None:
        self._by_key: dict[str, LibraryEntry] = {}
        for e in entries or ():
            self._by_key[e.episode_key] = e

    # -- dedup / lookup -------------------------------------------------------

    def find(self, key: str) -> LibraryEntry | None:
        return self._by_key.get(key)

    def __contains__(self, key: str) -> bool:
        return key in self._by_key

    def add(self, entry: LibraryEntry) -> bool:
        """Insert or replace by ``episode_key``. Overwrite is intentional (a
        re-download of the same episode supersedes the old record). Returns True
        if this key was new, False if it replaced an existing entry (finding #9)."""
        was_new = entry.episode_key not in self._by_key
        self._by_key[entry.episode_key] = entry
        return was_new

    def mark_played(self, key: str) -> None:
        e = self._by_key.get(key)
        if e is not None:
            self._by_key[key] = LibraryEntry(**{**asdict(e), "played": True})

    # -- 「放吧」pointer (P1-11) ----------------------------------------------

    def most_recent_unplayed(self) -> LibraryEntry | None:
        pending = [e for e in self._by_key.values() if not e.played]
        if not pending:
            return None
        return max(pending, key=lambda e: e.added_at)

    # -- disk accounting (D6) -------------------------------------------------

    def disk_usage_bytes(self) -> int:
        return sum(e.size_bytes for e in self._by_key.values())

    def over_limit(self, limit_gb: float) -> bool:
        return self.disk_usage_bytes() > limit_gb * (1024 ** 3)

    # -- reconcile (P1-9, register-only) --------------------------------------

    def reconcile(self, completed: Iterable[LibraryEntry]) -> list[LibraryEntry]:
        """Register qbt-completed episodes not yet known. Returns the NEWLY added
        entries (for the caller to announce -- never to auto-play)."""
        added: list[LibraryEntry] = []
        for e in completed:
            if e.episode_key not in self._by_key:
                self._by_key[e.episode_key] = e
                added.append(e)
        return added

    # -- persistence ----------------------------------------------------------

    def to_json(self) -> list[dict]:
        return [asdict(e) for e in self._by_key.values()]

    @classmethod
    def from_json(cls, data: list[dict]) -> "AnimeLibrary":
        return cls(LibraryEntry(**d) for d in data)
