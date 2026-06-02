from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class StreamKind(str, Enum):
    CHAT = "chat"
    SONG_PRELUDE = "song_prelude"


@dataclass(frozen=True)
class StreamToken:
    id: int
    kind: StreamKind
