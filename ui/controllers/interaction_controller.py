from __future__ import annotations

from collections.abc import Callable
from typing import Any

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
        screen_attachment_provider: Callable[[], dict[str, Any] | None] | None = None,
        consume_screen_attachment: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.chat_stream_controller = chat_stream_controller
        self.song_controller = song_controller
        self.audio_controller = audio_controller
        self.voice_input_controller = voice_input_controller
        self.focus_input = focus_input
        self.set_busy = set_busy
        self.screen_attachment_provider = screen_attachment_provider or (lambda: None)
        self.consume_screen_attachment = consume_screen_attachment or (lambda: None)

    def set_chat_stream_controller(self, chat_stream_controller: ChatStreamController | None) -> None:
        self.chat_stream_controller = chat_stream_controller

    def handle_user_text(self, text: str) -> None:
        # A (double-turn窄缝 second line): a send while a mic segment is still
        # recording would otherwise produce two turns (this send + the segment's own
        # recognition). In voice mode, retire the in-flight segment first. Pairs with
        # the input lock (input_enabled=not recording): the lock prevents the keypress,
        # this catches a send already in flight. No-op when nothing is recording.
        if self.voice_input_controller.voice_mode_active:
            self.voice_input_controller.interrupt_current_recording()
        message = (text or "").strip()
        has_screen_attachment = self.screen_attachment_provider() is not None
        if not message and not has_screen_attachment:
            self.focus_input()
            return

        if has_screen_attachment:
            if self.song_controller.is_busy():
                self.song_controller.cancel()
            screen_attachment = self.consume_screen_attachment()
            self._start_chat(message, screen_attachment=screen_attachment)
            return

        # B2: the pre-chat hijack is gone -- singing is the main LLM's sing_song
        # tool. The ONLY rule left is the control fast path (pause/resume/cancel/
        # restart while a song flow is live); any other message during a song
        # cancels it and goes to normal chat (the pre-B2 modal behaviour, kept).
        if self.song_controller.try_handle_control_text(message):
            return

        if self.song_controller.is_busy():
            self.song_controller.cancel()

        self._start_chat(message)

    def stop_conversation_for_song(self) -> None:
        if self.chat_stream_controller is not None:
            self.chat_stream_controller.stop_current()
        self.audio_controller.stop_owner(AudioOwner.CHAT)
        self.voice_input_controller.interrupt_current_recording()

    def _start_chat(self, message: str, screen_attachment: dict[str, Any] | None = None) -> None:
        if self.chat_stream_controller is None:
            self.set_busy(False)
            self.focus_input()
            return
        self.chat_stream_controller.start_chat(message, screen_attachment=screen_attachment)
