from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class StreamKind(str, Enum):
    # B2: SONG_PRELUDE (the synthetic-prompt prelude stream) died with the song
    # hijack stack -- the sing_song turn's own answer is the acknowledgment.
    CHAT = "chat"
    # P3: a system-initiated (proactive) turn. kind.value rides interaction_mode
    # into the runtime, where it is the typed marker (tool supply hard-off etc.).
    SYSTEM = "system"


@dataclass(frozen=True)
class StreamToken:
    id: int
    kind: StreamKind
