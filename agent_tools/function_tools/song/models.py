from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from typing import Any


class SongJobCancelled(RuntimeError):
    pass


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def cancelled(self) -> bool:
        return self._event.is_set()

    def throw_if_cancelled(self) -> None:
        if self.cancelled():
            raise SongJobCancelled("song job was cancelled")


@dataclass
class SongRequest:
    query: str
    title: str | None
    artist: str | None
    user_text: str
    voice_model: str = "spica"
    prefer_cache: bool = True
    max_duration_sec: int = 360

    def search_keyword(self) -> str:
        parts = [self.title or "", self.artist or ""]
        keyword = " ".join(part.strip() for part in parts if part and part.strip())
        return keyword or self.query.strip() or self.user_text.strip()


@dataclass
class NeteaseSong:
    song_id: str
    title: str
    artists: list[str]
    album: str = ""
    score: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def artist_text(self) -> str:
        return ", ".join(artist for artist in self.artists if artist)

    def display_name(self) -> str:
        artist = self.artist_text
        return f"{self.title} - {artist}" if artist else self.title


@dataclass
class SongJobResult:
    ok: bool
    final_audio_path: str | None = None
    song_id: str | None = None
    title: str | None = None
    artist: str | None = None
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)
