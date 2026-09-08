"""Single evaluator for desktop dialogue visibility."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer

from ui.widgets.dialogue_box import TintedDialogueBox


SYSTEM_MESSAGE_MS = 4000


class DialogueVisibilityController(QObject):
    """Own the dialogue's visible/hidden state instead of scattering show/hide."""

    def __init__(
        self,
        dialogue: TintedDialogueBox,
        *,
        update_click_through_mask: Callable[[], None] | None = None,
        user_hidden: bool = False,
        system_message_ms: int = SYSTEM_MESSAGE_MS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent or dialogue)
        self._dialogue = dialogue
        self._update_mask = update_click_through_mask or (lambda: None)
        self._user_hidden = bool(user_hidden)
        self._system_reveal_active = False
        self._system_message: str | None = None
        self._dialogue_line = str(dialogue.text_label.text() or "……")
        self._system_message_timer = QTimer(self)
        self._system_message_timer.setSingleShot(True)
        self._system_message_timer.setInterval(max(1, int(system_message_ms)))
        self._system_message_timer.timeout.connect(self._expire_system_message)
        self._apply_visibility(update_mask=False)


    @property
    def user_hidden(self) -> bool:
        return self._user_hidden

    @property
    def system_reveal_active(self) -> bool:
        return self._system_reveal_active


    def show_dialogue_line(self, text: str) -> None:
        """Buffer a spoken line while temporary system feedback is visible."""

        self._dialogue_line = str(text or "……")
        if not self._system_reveal_active:
            self._dialogue.set_dialogue_text(self._dialogue_line)

    def show_system_message(self, text: str) -> None:
        """Keep control/error feedback visible without creating another surface."""

        message = str(text or "……")
        temporary_reveal = self._user_hidden
        if temporary_reveal:
            self._system_message = message
            self._system_reveal_active = True
            self._dialogue.set_dialogue_text(message)
            self._system_message_timer.start()
        else:
            self._system_message_timer.stop()
            self._system_message = None
            self._system_reveal_active = False
            self._dialogue.set_dialogue_text(message)
        self._apply_visibility()

    def set_user_hidden(self, hidden: bool) -> None:
        self._user_hidden = bool(hidden)
        self._apply_visibility()


    def _expire_system_message(self) -> None:
        self._system_reveal_active = False
        self._system_message = None
        self._dialogue.set_dialogue_text(self._dialogue_line)
        self._apply_visibility()

    def _apply_visibility(self, *, update_mask: bool = True) -> None:
        visible = (
            not self._user_hidden or self._system_reveal_active
        )
        if visible:
            self._dialogue.show()
            # Visibility changes must preserve the overlay's stacking order:
            # a late system event cannot cover an open settings panel.
        else:
            self._dialogue.hide()
        if update_mask:
            self._update_mask()


__all__ = ["DialogueVisibilityController"]
