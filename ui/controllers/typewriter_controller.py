from __future__ import annotations

from collections.abc import Callable
import re

from PySide6.QtCore import QObject, QTimer, Signal

from ui.widgets.common import scaled_px

_READING_PAGE_CHARS = 48
_READING_PAUSE_MS = 1000
_CLOSING_MARKS = '」』”’\"\')）】》'


class TypewriterController(QObject):
    active_changed = Signal(bool)
    revealed = Signal()
    completed = Signal()

    def __init__(self, parent: QObject, set_text: Callable[[str], None], default_speed: float = 1.0) -> None:
        super().__init__(parent)
        self._set_text = set_text
        self.typing_timer: QTimer | None = None
        self.typing_text = ""
        self.typing_index = 0
        self.typing_finished_callback = None
        self.typewriter_speed = max(0.5, min(3.0, float(default_speed)))
        self.ui_scale = 1.0
        self._interval_cap_ms = None
        self._reading = False
        self._page_start = 0
        self._page_end = 0

    def start(self, text: str, interval_ms: int | None = None, on_finished=None, *, max_duration_ms=None, reading: bool = False) -> None:
        self.stop()
        self.typing_text = text or "……"
        self.typing_index = 0
        self._reading = reading
        self._page_start = 0
        self._page_end = self._next_reading_page_end(0) if reading else len(self.typing_text)
        self._interval_cap_ms = (
            max(1, int((max_duration_ms - 120) / max(1, len(self.typing_text))))
            if max_duration_ms is not None else None
        )
        self.typing_finished_callback = on_finished
        self._set_text("")
        self.typing_timer = QTimer(self)
        self.typing_timer.timeout.connect(self._on_typing_timeout)
        self.typing_timer.start(interval_ms or self._typewriter_delay(""))
        self.active_changed.emit(True)
        self._type_next_character()

    def stop(self) -> None:
        self._reading = False
        if self.typing_timer is None:
            self.typing_finished_callback = None
            self.active_changed.emit(False)
            return
        self.typing_timer.stop()
        self.typing_timer.deleteLater()
        self.typing_timer = None
        self.typing_finished_callback = None
        self.active_changed.emit(False)

    def set_speed(self, speed: float) -> None:
        self.typewriter_speed = max(0.5, min(3.0, float(speed)))
        if self.typing_timer is not None:
            self.typing_timer.setInterval(self._current_interval())

    def set_scale(self, scale: float) -> None:
        self.ui_scale = float(scale)
        if self.typing_timer is not None:
            self.typing_timer.setInterval(self._current_interval())

    def use_reading_pace(self) -> None:
        """A live audio revocation preserves the already-visible prefix."""
        if self._reading or self.typing_timer is None:
            return
        self._reading = True
        self._interval_cap_ms = None  # An abandoned audio deadline no longer rushes the text.
        self._page_end = max(self.typing_index, self._next_reading_page_end(0))
        self.typing_timer.setInterval(self._current_interval())

    def _next_reading_page_end(self, start: int) -> int:
        end = min(len(self.typing_text), start + _READING_PAGE_CHARS)
        # Keep multiline lists readable in the compact bubble as well.
        newlines = [match.end() for match in re.finditer('\n', self.typing_text[start:end])]
        if len(newlines) >= 2:
            return start + newlines[1]
        if end == len(self.typing_text):
            return end
        window = self.typing_text[start:end]
        closers = re.escape(_CLOSING_MARKS)
        for boundary in (rf'[。！？!?；;\n][{closers}]*', rf'[，,、：:\s][{closers}]*'):
            candidates = [match.end() for match in re.finditer(boundary, window)
                          if match.end() >= _READING_PAGE_CHARS // 2]
            if candidates:
                return start + candidates[-1]
        return end

    def is_active(self) -> bool:
        return self.typing_timer is not None

    def _on_typing_timeout(self) -> None:
        if self.typing_timer is self.sender():
            self._type_next_character()

    def _type_next_character(self) -> None:
        if self.typing_timer is None:
            return
        if self.typing_index >= len(self.typing_text):
            self._complete()
            return

        if self._reading and self.typing_index == self._page_end:
            self._page_start = self.typing_index
            self._page_end = self._next_reading_page_end(self._page_start)
        self.typing_index += 1
        self._set_text(self.typing_text[self._page_start: self.typing_index])
        if self.typing_timer is not None:
            if self.typing_index == len(self.typing_text):
                # Show the marker during the sentence pause, before releasing
                # the playback slot. Even fast typing leaves two Sana ticks.
                self.revealed.emit()
            self.typing_timer.setInterval(self._current_interval())

    def _current_interval(self) -> int:
        prefix, remaining = self.typing_text[:self.typing_index], self.typing_text[self.typing_index:]
        char = prefix[-1:]  # Empty during initial setup.
        if self._reading:
            if self.typing_index == self._page_end:
                return _READING_PAUSE_MS
            sentence_end = bool(char) and (char in '。！？!?' or char in _CLOSING_MARKS
                and prefix.rstrip(_CLOSING_MARKS).endswith(tuple('。！？!?')))
            if sentence_end and not (remaining and remaining[0] in _CLOSING_MARKS + '。！？!?'):
                return _READING_PAUSE_MS
            return self._typewriter_delay(char)
        if self.typing_index == len(self.typing_text):
            return max(120, self._typewriter_delay('。'))
        if char in '。！？!?' and not remaining.strip(' \n\t' + _CLOSING_MARKS):
            return self._typewriter_delay('')
        return self._typewriter_delay(char)

    def _typewriter_delay(self, char: str) -> int:
        if char and char in "。！？!?":
            delay = scaled_px(220, self.ui_scale)
        elif char and char in "、，,；;：:":
            delay = scaled_px(92, self.ui_scale)
        else:
            delay = max(22, min(46, scaled_px(34, self.ui_scale)))
        delay = max(8, round(delay / self.typewriter_speed))
        return min(delay, self._interval_cap_ms) if self._interval_cap_ms is not None else delay

    def _complete(self) -> None:
        if self.typing_timer is not None:
            self.typing_timer.stop()
            self.typing_timer.deleteLater()
            self.typing_timer = None
        callback = self.typing_finished_callback
        self.typing_finished_callback = None
        self.active_changed.emit(False)
        self.completed.emit()
        if callback:
            callback()
