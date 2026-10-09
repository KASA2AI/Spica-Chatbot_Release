"""Single evaluator for desktop dialogue visibility."""

from __future__ import annotations

from collections.abc import Callable
from collections import deque
import time

from PySide6.QtCore import QObject, QTimer, Signal

from ui.widgets.dialogue_box import TintedDialogueBox


SYSTEM_MESSAGE_MS = 4000


class DialogueVisibilityController(QObject):
    """Own the dialogue's visible/hidden state instead of scattering show/hide."""

    feedback_changed = Signal(str)
    line_changed = Signal(str)

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
        self._feedback_visible = False
        self._system_message: str | None = None
        self._dialogue_line = str(dialogue.text_label.text() or "……")
        self._floating_context = None
        self._floating_text = ''
        self._reading_until = 0.
        self._notices = deque(maxlen=16)
        self._seen_notice_ids = deque(maxlen=64)
        self._notice_until = 0.
        self._notice_input_at = None
        self._notice_message = None
        self._system_message_timer = QTimer(self)
        self._system_message_timer.setSingleShot(True)
        self._system_message_timer.setInterval(max(1, int(system_message_ms)))
        self._system_message_timer.timeout.connect(self._expire_system_message)
        self._apply_visibility(update_mask=False)


    @property
    def reply_text_visible(self) -> bool:
        return (not self._system_reveal_active and not self._feedback_visible and not self._notice_message
                and self._dialogue.isVisible() and not self._dialogue.window().isMinimized())


    @property
    def user_hidden(self) -> bool:
        return self._user_hidden

    @property
    def system_reveal_active(self) -> bool:
        return self._system_reveal_active


    def show_dialogue_line(self, text: str) -> None:
        """Buffer the currently presented line for either desktop surface."""

        self._dialogue_line = str(text or "……")
        self.line_changed.emit(self._dialogue_line)
        if not self._system_reveal_active and not self._notice_message:
            self._feedback_visible = False
            self._dialogue.set_dialogue_text(self._dialogue_line)

    def retain_reply(self) -> None:
        """Hold the last caption without rewriting it or extending microphone authorization."""
        if not self._dialogue_line.strip():
            return
        self._reading_until = time.monotonic() + min(60., max(15., 8. + len(self._dialogue_line) / 8.))

    def remember_floating_line(self, text, context):
        if context is not None:
            self._floating_text, self._floating_context = str(text), context
            self._reading_until = 0.

    @property
    def has_pending_notices(self):
        return bool(self._notices)

    def queue_notice(self, notice):
        if notice.notification_id in self._seen_notice_ids:
            return False
        self._seen_notice_ids.append(notice.notification_id)
        if len(self._notices) == self._notices.maxlen:
            self._notice_until = 0.
            self._notice_input_at = None
        self._notices.append(notice)
        return True

    def clear_floating(self):
        self._floating_text, self._floating_context = '', None
        self._reading_until = 0.


    def dismiss_notice(self, notification_id):
        if not self._notices or self._notices[0].notification_id != notification_id:
            return False
        self._notices.popleft()
        self._notice_until = 0.
        self._notice_input_at = None
        return True

    def close_floating(self):
        self.clear_floating()
        self._notices.clear()
        self._seen_notice_ids.clear()
        self._notice_until = 0.
        self._notice_input_at = None
        self.show_notice_message(None)

    def floating_content(self, context, *, active, available, visible, speaker, input_idle_seconds=None):
        """Select dialogue/readback before notices; the surface only renders it."""
        now = time.monotonic()
        if context != self._floating_context or not available:
            self.clear_floating()
            self._floating_context = context if available else None
        if available and (active or now < self._reading_until):
            self._notice_until = 0.
            self._notice_input_at = None
            return ('dialogue', speaker, self._floating_text or '…', None) if visible else None
        if not active and now >= self._reading_until:
            self.clear_floating()
        if not visible:
            self._notice_until = 0.
            self._notice_input_at = None
            return None
        if not self._notices:
            self._notice_input_at = None
            return None
        # Never infer reading from the mere passage of time. Require new input
        # while this notice is visible, then grant a full reading interval.
        last_input = now - input_idle_seconds if input_idle_seconds is not None else None
        if self._notice_input_at is None:
            self._notice_input_at = now
        if last_input is None:
            self._notice_until = 0.
        elif not self._notice_until and last_input >= self._notice_input_at:
            self._notice_until = now + 8.
        if self._notice_until and now >= self._notice_until:
            self._notices.popleft()
            self._notice_until = 0.
            self._notice_input_at = now
        if not self._notices:
            return None
        notice = self._notices[0]
        return ('notice', notice.title, notice.message, notice.notification_id)

    def show_notice_message(self, text):
        """Full-mode presentation of the same queue; no independent expiry timer."""
        if text == self._notice_message:
            return
        self._notice_message = text
        if not self._system_reveal_active:
            self._dialogue.set_dialogue_text(text or self._dialogue_line)
        self._apply_visibility()

    def show_system_message(self, text: str) -> None:
        """Keep control/error feedback visible without creating another surface."""

        message = str(text or "……")
        self._feedback_visible = True
        self.feedback_changed.emit(message)
        temporary_reveal = self._user_hidden or self._notice_message is not None
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
        self._feedback_visible = False
        self._system_reveal_active = False
        self._system_message = None
        self._dialogue.set_dialogue_text(self._notice_message or self._dialogue_line)
        self._apply_visibility()

    def _apply_visibility(self, *, update_mask: bool = True) -> None:
        visible = (
            (not self._user_hidden or self._system_reveal_active or self._notice_message is not None)
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
