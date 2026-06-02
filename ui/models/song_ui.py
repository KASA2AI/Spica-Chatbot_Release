from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_tools.function_tools.song import SongContext, SongState


@dataclass
class SongUiState:
    state: SongState = SongState.IDLE
    context: SongContext = field(default_factory=SongContext)
    session_id: int = 0
    auto_play: bool = True
    prelude_active: bool = False
    clear_throat_active: bool = False
    user_paused_preparing: bool = False
    pending_audio_path: str | None = None
    pending_song_hint: Any | None = None

    @property
    def is_preparing(self) -> bool:
        return self.state == SongState.PREPARING

    @property
    def is_playback_active(self) -> bool:
        return self.state == SongState.PLAYING

    @property
    def is_busy(self) -> bool:
        return self.state in {
            SongState.PREPARING,
            SongState.READY,
            SongState.PLAYING,
            SongState.PAUSED,
            SongState.CANCELLING,
        }
