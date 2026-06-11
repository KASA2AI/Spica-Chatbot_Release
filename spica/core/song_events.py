"""Song runtime events crossing the Host -> UI boundary (B2 / P2).

Same dataclass channel as the companion events (CLAUDE.md #2: cross-boundary =
RuntimeEvent; the UI bridge hops threads via a Qt signal). ``SongRequestEvent``
is emitted by the host's ``_request_song`` closure when the sing_song tool
resolves a song -- the UI dispatches it to SongController, which starts the
SongWorker. The tool runs on the ChatWorker thread and never touches Qt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from spica.core.events import RuntimeEvent, register_event


@dataclass(frozen=True)
class SongRequestEvent(RuntimeEvent):
    kind: ClassVar[str] = "song_request"
    query: str
    title: str = ""
    artist: str = ""

    def _data(self) -> dict[str, Any]:
        return {"query": self.query, "title": self.title, "artist": self.artist}


register_event(
    "song_request",
    lambda d: SongRequestEvent(
        query=str(d.get("query") or ""),
        title=str(d.get("title") or ""),
        artist=str(d.get("artist") or ""),
    ),
)
