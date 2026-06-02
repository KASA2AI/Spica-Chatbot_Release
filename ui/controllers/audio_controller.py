from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QUrl

from ui.models.playback import AudioOwner, AudioToken

logger = logging.getLogger(__name__)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
except Exception:  # pragma: no cover - depends on the local Qt install
    QAudioOutput = None
    QMediaPlayer = None


@dataclass
class _PreloadedAudio:
    media_player: Any
    audio_output: Any
    path: Path


class AudioController(QObject):
    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._chat_media_player = None
        self._chat_audio_output = None
        self._chat_token: AudioToken | None = None
        self._chat_on_finished: Callable[[], None] | None = None
        self._preloaded_chat: dict[int, _PreloadedAudio] = {}

        self._song_media_player = None
        self._song_audio_output = None
        self._song_token: AudioToken | None = None
        self._song_on_finished: Callable[[], None] | None = None
        self._song_on_error: Callable[[str], None] | None = None

    def play_chat_audio(self, audio_path: Any, token: AudioToken, on_finished: Callable[[], None]) -> bool:
        self.release_chat_audio()
        if not audio_path or QMediaPlayer is None or QAudioOutput is None:
            logger.debug(
                "event=chat_audio_end token_id=%s reason=unavailable audio_path=%s",
                token.id,
                audio_path,
            )
            on_finished()
            return False

        path = Path(str(audio_path))
        if not path.exists():
            logger.debug("event=chat_audio_end token_id=%s reason=missing_file path=%s", token.id, path)
            on_finished()
            return False

        self._chat_token = token
        self._chat_on_finished = on_finished

        preloaded_key = self._preloaded_key_for_path(path)
        if preloaded_key is not None:
            logger.debug("event=preload_hit owner=chat token_id=%s index=%s path=%s", token.id, preloaded_key, path)
            preloaded = self._preloaded_chat.pop(preloaded_key)
            self._chat_media_player = preloaded.media_player
            self._chat_audio_output = preloaded.audio_output
            self._set_player_token(self._chat_media_player, token)
            try:
                self._chat_media_player.mediaStatusChanged.connect(self._handle_chat_media_status)
            except Exception:
                self.release_chat_audio()
                logger.debug("event=chat_audio_end token_id=%s reason=connect_failed path=%s", token.id, path)
                on_finished()
                return False
            logger.debug("event=chat_audio_start token_id=%s path=%s preloaded=true", token.id, path)
            self._chat_media_player.play()
            return True

        logger.debug("event=preload_miss owner=chat token_id=%s path=%s", token.id, path)
        self._chat_audio_output = QAudioOutput(self)
        self._chat_audio_output.setVolume(0.86)
        self._chat_media_player = QMediaPlayer(self)
        self._set_player_token(self._chat_media_player, token)
        self._chat_media_player.setAudioOutput(self._chat_audio_output)
        self._chat_media_player.mediaStatusChanged.connect(self._handle_chat_media_status)
        self._chat_media_player.setSource(QUrl.fromLocalFile(str(path)))
        logger.debug("event=chat_audio_start token_id=%s path=%s preloaded=false", token.id, path)
        self._chat_media_player.play()
        return True

    def preload_chat_audio(self, index: int, audio_path: Any) -> bool:
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
            audio_output.setVolume(0.86)
            media_player = QMediaPlayer(self)
            media_player.setAudioOutput(audio_output)
            media_player.setSource(QUrl.fromLocalFile(str(path)))
        except Exception:
            self._delete_later(media_player)
            self._delete_later(audio_output)
            logger.debug("event=preload_miss owner=chat index=%s reason=create_failed path=%s", index, path)
            return False

        self._preloaded_chat[index] = _PreloadedAudio(media_player, audio_output, path)
        logger.debug("event=preload_hit owner=chat index=%s path=%s action=store", index, path)
        return True

    def release_chat_audio(self) -> None:
        media_player = self._chat_media_player
        audio_output = self._chat_audio_output
        self._chat_media_player = None
        self._chat_audio_output = None
        self._chat_token = None
        self._chat_on_finished = None
        self._release_player(media_player, audio_output, self._handle_chat_media_status)

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
        self._song_token = token
        self._song_on_finished = on_finished
        self._song_on_error = on_error
        self._song_audio_output = QAudioOutput(self)
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
            on_finished = self._chat_on_finished
            logger.debug("event=chat_audio_end token_id=%s status=%s", token.id, status)
            self.release_chat_audio()
            if on_finished is not None:
                on_finished()

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

        if status == QMediaPlayer.MediaStatus.InvalidMedia:
            on_error = self._song_on_error
            logger.debug("event=song_audio_end token_id=%s status=%s reason=invalid_media", token.id, status)
            self.stop_song()
            if on_error is not None:
                on_error("歌曲音频无法播放。")
            return
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            on_finished = self._song_on_finished
            logger.debug("event=song_audio_end token_id=%s status=%s", token.id, status)
            self.stop_song()
            if on_finished is not None:
                on_finished()

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
            try:
                media_player.mediaStatusChanged.disconnect(handler)
            except Exception:
                pass
            try:
                media_player.stop()
            except Exception:
                pass
            self._delete_later(media_player)
        self._delete_later(audio_output)

    def _delete_later(self, obj: Any) -> None:
        if obj is None:
            return
        try:
            obj.deleteLater()
        except Exception:
            pass
