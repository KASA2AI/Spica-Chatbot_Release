from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AudioOwner(str, Enum):
    CHAT = "chat"
    SONG = "song"


@dataclass(frozen=True)
class AudioToken:
    id: int
    owner: AudioOwner
