from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, QUrl, Signal

from ui.models.playback import AudioOwner, AudioToken
from ui.audio_devices import output_label, resolve_output_device

logger = logging.getLogger(__name__)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer
except Exception:  # pragma: no cover - depends on the local Qt install
    QAudioOutput = None
    QMediaDevices = None
    QMediaPlayer = None


@dataclass
class _PreloadedAudio:
    media_player: Any
    audio_output: Any
    path: Path


class AudioController(QObject):
    playback_requested = Signal()
    def set_output_device(self, device_id: str) -> None:
        """Restart-effective binding, installed before presentation starts."""
        self._output_device_id = device_id

    def set_home_output_device(self, device_id: str | None, *, fallback_device_id: str = '') -> None:
        self._home_output_device_id = device_id
        self._home_fallback_device_id = fallback_device_id

    def __init__(self, parent: QObject, *, output_device_id: str = '') -> None:
        super().__init__(parent)
        self._output_device_id = output_device_id
        self._home_output_device_id = None
        self._home_fallback_device_id = ''
        self._chat_bound_device = self._song_bound_device = ''
        # Playback volume for HER VOICE (the chat/TTS audio path: normal chat +
        # galgame reaction + song-finished report). Linear 0.0-1.0; 0.86 is the
        # historical hardcoded value, kept as the default so behaviour is unchanged
        # until the user moves the "Spica 语音音量" slider. Song playback uses a
        # SEPARATE output (_song_audio_output) and is intentionally not governed here.
        self._chat_volume = 0.86
        self._chat_volume_override: float | None = None
        self._chat_media_player = None
        self._chat_audio_output = None
        self._chat_token: AudioToken | None = None
        self._chat_on_finished: Callable[[], None] | None = None
        self._chat_on_playback: Callable[[str, float], None] | None = None
        self._chat_started = False
        self._chat_started_at: float | None = None
        self._preloaded_chat: dict[int, _PreloadedAudio] = {}
        self._cubism_audio = None

        self._song_media_player = None
        self._song_audio_output = None
        self._song_token: AudioToken | None = None
        self._song_on_finished: Callable[[], None] | None = None
        self._song_on_error: Callable[[str], None] | None = None
        self._media_devices = None
        if QMediaDevices is not None and hasattr(QMediaDevices, 'audioOutputsChanged'):
            self._media_devices = QMediaDevices(self)
            self._media_devices.audioOutputsChanged.connect(self._outputs_changed)

    def _output_binding(self, audio_route):
        if audio_route not in {'daily', 'home'}:
            raise ValueError('未知的音频输出用途')
        primary, fallback = self._output_device_id, ''
        if audio_route == 'home':
            primary = self._home_output_device_id if self._home_output_device_id is not None else primary
            fallback = self._home_fallback_device_id
        return primary, fallback

    def output_status(self, audio_route='home'):
        """Resolve the running selection without constructing a player."""
        primary, fallback = self._output_binding(audio_route)
        if QMediaDevices is None:
            return {'available': False, 'detail': '当前音频后端不可用'}
        try:
            device, binding = resolve_output_device(QMediaDevices, primary, fallback)
            if device.isNull():
                return {'available': False, 'detail': '系统没有可用的输出设备'}
            return {'available': True, 'name': output_label(device),
                    'system_default': not binding, 'fallback': bool(binding and binding != primary)}
        except RuntimeError as exc:
            return {'available': False, 'detail': str(exc)}

    def _configure_output(self, output, audio_route='daily') -> str:
        primary, fallback = self._output_binding(audio_route)
        device, binding = resolve_output_device(QMediaDevices, primary, fallback)
        output.setDevice(device)
        if binding and binding != primary:
            logger.warning('event=audio_output_fallback route=%s device_id=%s', audio_route, binding)
        return binding

    def _outputs_changed(self):
        # Device removal may otherwise make an OS backend reroute silently.
        QTimer.singleShot(0, self._check_output_devices)

    def _check_output_devices(self):
        available = {bytes(device.id()).hex() for device in QMediaDevices.audioOutputs()}
        if self._chat_token is not None and self._chat_bound_device and self._chat_bound_device not in available:
            on_finished, on_playback = self._chat_on_finished, self._chat_on_playback
            self._chat_on_playback = None
            self.release_chat_audio()
            logger.warning('event=chat_audio_output_disconnected')
            if on_playback is not None:
                on_playback('failed', time.monotonic())
            if on_finished is not None:
                on_finished()
        if self._song_token is not None and self._song_bound_device and self._song_bound_device not in available:
            on_error = self._song_on_error
            self.stop_song()
            if on_error is not None:
                on_error('指定音箱已断开，歌曲播放已停止。')

    def enable_cubism_lipsync(self):
        if self._cubism_audio is None:
            from ui.controllers.cubism_audio import CubismAudio

            self._cubism_audio = CubismAudio(self)
        return self._cubism_audio

    def voice_playback_position(self):
        player = self._chat_media_player
        if player is not None and self._chat_token is not None and QMediaPlayer is not None:
            if player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                return Path(player.source().toLocalFile()), player.position()
        player = self._song_media_player
        if player is not None and self._song_token is not None and self._cubism_audio is not None:
            if player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                vocal = self._cubism_audio.song_voice(player.source().toLocalFile())
                if vocal is not None:
                    return vocal, player.position()
        return None, 0

    def register_song_voice(self, mixed_path, vocal_path):
        if self._cubism_audio is not None:
            self._cubism_audio.register_song(mixed_path, vocal_path)

    def chat_audio_started_at(self) -> float | None:
        return self._chat_started_at

    def is_voice_playing(self) -> bool:
        return bool(self._chat_started or (QMediaPlayer is not None and self._song_media_player is not None
                    and self._song_media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState))

    def set_chat_volume(self, volume: float) -> None:
        """Set HER VOICE playback volume (linear 0.0-1.0). Stored so every future chat
        output is created at this level (play_chat_audio / preload_chat_audio), AND
        applied immediately to the currently playing output plus EVERY preloaded one,
        so a change in the settings panel takes effect mid-playback without a restart.
        Song playback (_song_audio_output) is a separate output and is left untouched.
        GUI-thread only -- called from startup and the settings panel, never a worker."""
        try:
            v = float(volume)
        except (TypeError, ValueError):
            return
        v = max(0.0, min(1.0, v))
        self._chat_volume = v
        if self._chat_audio_output is not None and self._chat_volume_override is None:
            try:
                self._chat_audio_output.setVolume(v)
            except Exception:
                pass
        for preloaded in self._preloaded_chat.values():
            if preloaded.audio_output is not None:
                try:
                    preloaded.audio_output.setVolume(v)
                except Exception:
                    pass

    def play_chat_audio(
        self, audio_path: Any, token: AudioToken, on_finished: Callable[[], None],
        *, on_playback: Callable[[str, float], None] | None = None,
        volume: float | None = None,
        audio_route: str = 'daily',
    ) -> bool:
        if volume is not None and (not math.isfinite(volume) or not 0 <= volume <= 1):
            raise ValueError('playback volume must be finite and between 0 and 1')
        self.release_chat_audio()
        if not audio_path or QMediaPlayer is None or QAudioOutput is None:
            logger.debug(
                "event=chat_audio_end token_id=%s reason=unavailable audio_path=%s",
                token.id,
                audio_path,
            )
            if on_playback is not None:
                on_playback('not_started', time.monotonic())
            on_finished()
            return False

        path = Path(str(audio_path))
        if not path.exists():
            logger.debug("event=chat_audio_end token_id=%s reason=missing_file path=%s", token.id, path)
            if on_playback is not None:
                on_playback('not_started', time.monotonic())
            on_finished()
            return False

        self.playback_requested.emit()
        self._chat_token = token
        self._chat_on_finished = on_finished
        self._chat_on_playback = on_playback
        self._chat_volume_override = volume
        play_volume = self._chat_volume if volume is None else volume
        self._chat_started = False
        self._chat_started_at = None
        try:
            if self._cubism_audio is not None:
                self._cubism_audio.prepare(path)
            preloaded_key = self._preloaded_key_for_path(path)
            if preloaded_key is not None:
                preloaded = self._preloaded_chat.pop(preloaded_key)
                self._chat_media_player = preloaded.media_player
                self._chat_audio_output = preloaded.audio_output
            else:
                self._chat_audio_output = QAudioOutput(self)
                self._chat_media_player = QMediaPlayer(self)
                self._chat_media_player.setAudioOutput(self._chat_audio_output)
            self._chat_audio_output.setVolume(play_volume)
            player = self._chat_media_player
            self._set_player_token(player, token)
            player.mediaStatusChanged.connect(self._handle_chat_media_status)
            player.playbackStateChanged.connect(self._handle_chat_playback_state)
            player.errorOccurred.connect(self._handle_chat_error)
            # Re-resolve at actual playback too: a prepared greeting may span a
            # device reconnect or a system-default change.
            self._chat_bound_device = self._configure_output(self._chat_audio_output, audio_route)
            if preloaded_key is None:
                player.setSource(QUrl.fromLocalFile(str(path)))
            # A synchronous InvalidMedia/error callback already owns cleanup.
            if self._chat_media_player is not player:
                return False
            logger.debug("event=chat_audio_play_requested token_id=%s path=%s preloaded=%s",
                         token.id, path, preloaded_key is not None)
            logger.info('event=chat_audio_output_selected token_id=%s route=%s device_id=%s',
                        token.id, audio_route, self._chat_bound_device or 'system_default')
            player.play()
            return self._chat_media_player is player
        except Exception:
            logger.warning('chat audio player failed to start', exc_info=True)
            if self._chat_token is token:
                self._chat_on_playback = None
                self.release_chat_audio()
                if on_playback is not None:
                    on_playback('failed', time.monotonic())
                on_finished()
            return False

    def preload_chat_audio(self, index: int, audio_path: Any, *, audio_route: str = 'daily') -> bool:
        if QMediaPlayer is None or QAudioOutput is None:
            logger.debug("event=preload_miss owner=chat index=%s reason=qt_unavailable audio_path=%s", index, audio_path)
            return False
        if index in self._preloaded_chat:
            logger.debug("event=preload_miss owner=chat index=%s reason=already_preloaded audio_path=%s", index, audio_path)
            return False
        if not audio_path:
            logger.debug("event=preload_miss owner=chat index=%s reason=missing_path", index)
            return False

        path = Path(str(audio_path))
        if not path.exists():
            logger.debug("event=preload_miss owner=chat index=%s reason=missing_file path=%s", index, path)
            return False

        audio_output = None
        media_player = None
        try:
            audio_output = QAudioOutput(self)
            self._configure_output(audio_output, audio_route)
            audio_output.setVolume(self._chat_volume)
            media_player = QMediaPlayer(self)
            media_player.setAudioOutput(audio_output)
            media_player.setSource(QUrl.fromLocalFile(str(path)))
        except Exception:
            self._delete_later(media_player)
            self._delete_later(audio_output)
            logger.debug("event=preload_miss owner=chat index=%s reason=create_failed path=%s", index, path)
            return False

        self._preloaded_chat[index] = _PreloadedAudio(media_player, audio_output, path)
        if self._cubism_audio is not None:
            self._cubism_audio.prepare(path)
        logger.debug("event=preload_hit owner=chat index=%s path=%s action=store", index, path)
        return True

    def release_chat_audio(self) -> None:
        media_player = self._chat_media_player
        audio_output = self._chat_audio_output
        on_playback = self._chat_on_playback
        self._chat_media_player = None
        self._chat_audio_output = None
        self._chat_token = None
        self._chat_on_finished = None
        self._chat_on_playback = None
        self._chat_volume_override = None
        self._chat_started = False
        self._chat_started_at = None
        self._release_player(media_player, audio_output, self._handle_chat_media_status)
        if on_playback is not None:
            on_playback('stopped', time.monotonic())

    def release_preloaded(self, index: int | None = None) -> None:
        if index is None:
            items = list(self._preloaded_chat.items())
            self._preloaded_chat.clear()
        else:
            preloaded = self._preloaded_chat.pop(index, None)
            items = [(index, preloaded)] if preloaded is not None else []

        for _key, preloaded in items:
            if preloaded is not None:
                logger.debug("event=preload_release owner=chat index=%s path=%s", _key, preloaded.path)
                self._release_player(preloaded.media_player, preloaded.audio_output, self._handle_chat_media_status)

    def play_song(
        self,
        audio_path: Any,
        token: AudioToken,
        on_finished: Callable[[], None],
        on_error: Callable[[str], None],
    ) -> bool:
        self.stop_song()
        if QMediaPlayer is None or QAudioOutput is None:
            on_error("当前 Qt 环境没有可用的音频播放组件。")
            return False

        path = Path(str(audio_path))
        if not path.exists():
            on_error(f"音频文件不存在：{path}")
            return False

        logger.debug("event=song_audio_start token_id=%s path=%s", token.id, path)
        self.playback_requested.emit()
        self._song_token = token
        self._song_on_finished = on_finished
        self._song_on_error = on_error
        self._song_audio_output = QAudioOutput(self)
        try:
            self._song_bound_device = self._configure_output(self._song_audio_output)
        except RuntimeError as exc:
            self._song_audio_output.deleteLater()
            self._song_audio_output = None
            self._song_token = None
            self._song_on_finished = self._song_on_error = None
            on_error(str(exc))
            return False
        self._song_audio_output.setVolume(0.92)
        self._song_media_player = QMediaPlayer(self)
        self._set_player_token(self._song_media_player, token)
        self._song_media_player.setAudioOutput(self._song_audio_output)
        self._song_media_player.mediaStatusChanged.connect(self._handle_song_media_status)
        self._song_media_player.setSource(QUrl.fromLocalFile(str(path)))
        self._song_media_player.play()
        return True

    def pause_song(self) -> bool:
        if self._song_media_player is None:
            return False
        self._song_media_player.pause()
        return True

    def resume_song(self) -> bool:
        if self._song_media_player is None:
            return False
        self._song_media_player.play()
        return True

    def stop_song(self) -> None:
        media_player = self._song_media_player
        audio_output = self._song_audio_output
        self._song_media_player = None
        self._song_audio_output = None
        self._song_token = None
        self._song_on_finished = None
        self._song_on_error = None
        self._release_player(media_player, audio_output, self._handle_song_media_status)

    def stop_owner(self, owner: AudioOwner) -> None:
        if owner == AudioOwner.CHAT:
            self.release_chat_audio()
            self.release_preloaded()
            return
        if owner == AudioOwner.SONG:
            self.stop_song()

    def stop_all(self) -> None:
        self.stop_owner(AudioOwner.CHAT)
        self.stop_owner(AudioOwner.SONG)

    def _handle_chat_error(self, error, message='') -> None:
        if QMediaPlayer is not None and error != QMediaPlayer.Error.NoError:
            self._handle_chat_media_status(QMediaPlayer.MediaStatus.InvalidMedia)

    def _handle_chat_playback_state(self, state) -> None:
        if QMediaPlayer is None or state != QMediaPlayer.PlaybackState.PlayingState:
            return
        if self._chat_started or not self._sender_matches_token(
            self.sender(), self._chat_media_player, self._chat_token, AudioOwner.CHAT,
        ):
            return
        self._chat_started = True
        callback, occurred_at = self._chat_on_playback, time.monotonic()
        self._chat_started_at = occurred_at
        # A receipt may cause cancellation. Keep that work off Qt's signal stack.
        if callback is not None:
            QTimer.singleShot(0, lambda: callback('started', occurred_at))

    def _handle_chat_media_status(self, status) -> None:
        if QMediaPlayer is None:
            return
        sender = self.sender()
        token = self._chat_token
        if not self._sender_matches_token(sender, self._chat_media_player, token, AudioOwner.CHAT):
            logger.debug(
                "event=stale_audio_event_ignored owner=chat token_id=%s status=%s",
                token.id if token else None,
                status,
            )
            return

        if status in (QMediaPlayer.MediaStatus.EndOfMedia, QMediaPlayer.MediaStatus.InvalidMedia):
            # Capture teardown targets + cb and NULL self refs NOW (plain Python, no
            # Qt re-entry), then defer ALL Qt teardown + playback advance out of THIS
            # signal's dispatch. Disconnecting/stopping the player that is CURRENTLY
            # emitting mediaStatusChanged deadlocks Qt's cross-thread signal dispatch
            # (2026-06-27: two py-spy frames froze byte-identical at _release_player
            # disconnect, audio_controller.py:298). The slot must run ONLY plain
            # Python; every QMediaPlayer op (disconnect/stop/deleteLater) and the
            # advance run on the next loop tick, on a clean stack.
            media_player = self._chat_media_player
            audio_output = self._chat_audio_output
            on_finished = self._chat_on_finished
            on_playback = self._chat_on_playback
            outcome = ('failed' if status == QMediaPlayer.MediaStatus.InvalidMedia
                       else 'completed' if self._chat_started else 'not_started')
            occurred_at = time.monotonic()
            self._chat_media_player = None
            self._chat_audio_output = None
            self._chat_token = None
            self._chat_on_finished = None
            self._chat_on_playback = None
            self._chat_started = False
            logger.debug("event=chat_audio_end token_id=%s status=%s", token.id, status)

            def _finish_chat_eom() -> None:
                # release BEFORE on_finished (same order as the original sync path).
                # The CAPTURED player (not self.*) is torn down, so a stop()/new turn
                # during the defer gap -- which sees self refs already None -- can
                # neither double-free it nor mix old/new players.
                self._release_player(media_player, audio_output, self._handle_chat_media_status)
                if on_playback is not None:
                    on_playback(outcome, occurred_at)
                if on_finished is not None:
                    on_finished()

            QTimer.singleShot(0, _finish_chat_eom)

    def _handle_song_media_status(self, status) -> None:
        if QMediaPlayer is None:
            return
        sender = self.sender()
        token = self._song_token
        if not self._sender_matches_token(sender, self._song_media_player, token, AudioOwner.SONG):
            logger.debug(
                "event=stale_audio_event_ignored owner=song token_id=%s status=%s",
                token.id if token else None,
                status,
            )
            return

        if status in (QMediaPlayer.MediaStatus.InvalidMedia, QMediaPlayer.MediaStatus.EndOfMedia):
            # Same re-entrancy fix as the chat handler: stop_song() -> _release_player
            # -> disconnect(:298) the signal being emitted deadlocks Qt dispatch
            # (stop_song mirrors release_chat_audio exactly -- the same latent bug,
            # rarer only because a song ends far less often than a chat segment).
            # Capture + null self refs synchronously; defer all Qt teardown + the
            # callback off the dispatch stack. InvalidMedia -> on_error, EndOfMedia
            # -> on_finished (the original split preserved).
            invalid = status == QMediaPlayer.MediaStatus.InvalidMedia
            media_player = self._song_media_player
            audio_output = self._song_audio_output
            on_finished = self._song_on_finished
            on_error = self._song_on_error
            self._song_media_player = None
            self._song_audio_output = None
            self._song_token = None
            self._song_on_finished = None
            self._song_on_error = None
            logger.debug(
                "event=song_audio_end token_id=%s status=%s%s",
                token.id, status, " reason=invalid_media" if invalid else "",
            )

            def _finish_song_eom() -> None:
                self._release_player(media_player, audio_output, self._handle_song_media_status)
                if invalid:
                    if on_error is not None:
                        on_error("歌曲音频无法播放。")
                elif on_finished is not None:
                    on_finished()

            QTimer.singleShot(0, _finish_song_eom)
            return

    def _preloaded_key_for_path(self, path: Path) -> int | None:
        for key, preloaded in self._preloaded_chat.items():
            if preloaded.path == path:
                return key
        return None

    def _set_player_token(self, media_player: Any, token: AudioToken) -> None:
        if media_player is None:
            return
        try:
            media_player.setProperty("audio_token_id", token.id)
            media_player.setProperty("audio_owner", token.owner.value)
        except Exception:
            pass

    def _sender_matches_token(
        self,
        sender: Any,
        current_player: Any,
        token: AudioToken | None,
        owner: AudioOwner,
    ) -> bool:
        if sender is None or current_player is None or sender is not current_player:
            return False
        if token is None or token.owner != owner:
            return False
        try:
            sender_token_id = int(sender.property("audio_token_id"))
            sender_owner = str(sender.property("audio_owner"))
        except Exception:
            return False
        return sender_token_id == token.id and sender_owner == owner.value

    def _release_player(self, media_player: Any, audio_output: Any, handler: Callable[..., None]) -> None:
        if media_player is not None:
            if handler == self._handle_chat_media_status:
                try:
                    media_player.playbackStateChanged.disconnect(self._handle_chat_playback_state)
                except Exception:
                    pass
                try:
                    media_player.errorOccurred.disconnect(self._handle_chat_error)
                except Exception:
                    pass
            try:
                media_player.mediaStatusChanged.disconnect(handler)
            except Exception:
                pass
            try:
                media_player.stop()
            except Exception:
                logger.warning('event=audio_player_stop_failed', exc_info=True)
            # Record the native player's state separately from the core's
            # cancellation result. This is not a claim of physical audibility.
            if handler == self._handle_chat_media_status:
                try:
                    state = media_player.playbackState()
                    logger.info('event=chat_audio_player_stop_state state=%s', state.name)
                except Exception:
                    logger.debug('event=chat_audio_player_stop_state state=unknown')
            self._delete_later(media_player)
        self._delete_later(audio_output)

    def _delete_later(self, obj: Any) -> None:
        if obj is None:
            return
        try:
            obj.deleteLater()
        except Exception:
            pass

    def voice_playback_position(self):
        player = self._chat_media_player
        if player is not None and self._chat_token is not None and QMediaPlayer is not None:
            if player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                return Path(player.source().toLocalFile()), player.position()
        player = self._song_media_player
        if player is not None and self._song_token is not None and self._cubism_audio is not None:
            if player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                vocal = self._cubism_audio.song_voice(player.source().toLocalFile())
                if vocal is not None:
                    return vocal, player.position()
        return None, 0

    def enable_cubism_lipsync(self):
        if self._cubism_audio is None:
            from ui.controllers.cubism_audio import CubismAudio

            self._cubism_audio = CubismAudio(self)
        return self._cubism_audio


    def register_song_voice(self, mixed_path, vocal_path):
        if self._cubism_audio is not None:
            self._cubism_audio.register_song(mixed_path, vocal_path)
