from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject

from ui.controllers.audio_controller import AudioController
from ui.controllers.chat_stream_controller import ChatStreamController
from ui.controllers.song_controller import SongController
from ui.controllers.voice_input_controller import VoiceInputController
from ui.models.playback import AudioOwner


class InteractionController(QObject):
    def __init__(
        self,
        parent: QObject,
        chat_stream_controller: ChatStreamController | None,
        song_controller: SongController,
        audio_controller: AudioController,
        voice_input_controller: VoiceInputController,
        focus_input: Callable[[], None],
        set_busy: Callable[[bool], None],
    ) -> None:
        super().__init__(parent)
        self.chat_stream_controller = chat_stream_controller
        self.song_controller = song_controller
        self.audio_controller = audio_controller
        self.voice_input_controller = voice_input_controller
        self.focus_input = focus_input
        self.set_busy = set_busy

    def set_chat_stream_controller(self, chat_stream_controller: ChatStreamController | None) -> None:
        self.chat_stream_controller = chat_stream_controller

    def handle_user_text(self, text: str) -> None:
        message = (text or "").strip()
        if not message:
            self.focus_input()
            return

        intent = self.song_controller.route_text(message)
        if self.song_controller.is_actionable_intent(intent):
            self.song_controller.handle_intent(intent)
            return

        if self.song_controller.is_intent_confirming():
            self.song_controller.exit_intent_confirmation()

        if self.song_controller.is_busy():
            self.song_controller.cancel(show_message=False)

        self._start_chat(message)

    def stop_conversation_for_song(self) -> None:
        if self.chat_stream_controller is not None:
            self.chat_stream_controller.stop_current()
        self.audio_controller.stop_owner(AudioOwner.CHAT)
        self.voice_input_controller.interrupt_current_recording()

    def _start_chat(self, message: str) -> None:
        if self.chat_stream_controller is None:
            self.set_busy(False)
            self.focus_input()
            return
        self.chat_stream_controller.start_chat(message)
