from __future__ import annotations

from dataclasses import dataclass, field

from agent_tools.function_tools.song import SongContext, SongState


@dataclass
class SongPlaybackGate:
    prelude_done: bool = True
    clear_throat_done: bool = True
    song_ready: bool = False
    user_paused: bool = False

    @property
    def can_play(self) -> bool:
        return (
            self.song_ready
            and self.prelude_done
            and self.clear_throat_done
            and not self.user_paused
        )


@dataclass
class SongUiState:
    state: SongState = SongState.IDLE
    context: SongContext = field(default_factory=SongContext)
    session_id: int = 0
    pending_audio_path: str | None = None
    playback_gate: SongPlaybackGate = field(default_factory=SongPlaybackGate)

    # Compatibility mirrors. New playback logic should use playback_gate directly.
    @property
    def prelude_active(self) -> bool:
        return not self.playback_gate.prelude_done

    @property
    def clear_throat_active(self) -> bool:
        return not self.playback_gate.clear_throat_done

    @property
    def user_paused_preparing(self) -> bool:
        return self.playback_gate.user_paused

    @property
    def auto_play(self) -> bool:
        return (
            not self.playback_gate.user_paused
            and self.playback_gate.prelude_done
            and self.playback_gate.clear_throat_done
        )

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
