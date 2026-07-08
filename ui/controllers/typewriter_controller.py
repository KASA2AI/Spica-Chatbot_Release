from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer

from ui.widgets.common import scaled_px


class TypewriterController(QObject):
    def __init__(self, parent: QObject, set_text: Callable[[str], None], default_speed: float = 1.0) -> None:
        super().__init__(parent)
        self._set_text = set_text
        self.typing_timer: QTimer | None = None
        self.typing_text = ""
        self.typing_index = 0
        self.typing_finished_callback = None
        self.typewriter_speed = max(0.5, min(3.0, float(default_speed)))
        self.ui_scale = 1.0

    def start(self, text: str, interval_ms: int | None = None, on_finished=None) -> None:
        self.stop()
        self.typing_text = text or "……"
        self.typing_index = 0
        self.typing_finished_callback = on_finished
        self._set_text("")
        self.typing_timer = QTimer(self)
        self.typing_timer.timeout.connect(self._type_next_character)
        self.typing_timer.start(interval_ms or self._typewriter_delay(""))
        self._type_next_character()

    def stop(self) -> None:
        if self.typing_timer is None:
            self.typing_finished_callback = None
            return
        self.typing_timer.stop()
        self.typing_timer.deleteLater()
        self.typing_timer = None
        self.typing_finished_callback = None

    def set_speed(self, speed: float) -> None:
        self.typewriter_speed = max(0.5, min(3.0, float(speed)))
        if self.typing_timer is not None:
            self.typing_timer.setInterval(self._typewriter_delay(""))

    def set_scale(self, scale: float) -> None:
        self.ui_scale = float(scale)
        if self.typing_timer is not None:
            self.typing_timer.setInterval(self._typewriter_delay(""))

    def is_active(self) -> bool:
        return self.typing_timer is not None

    def _type_next_character(self) -> None:
        if self.typing_index >= len(self.typing_text):
            self._complete()
            return

        char = self.typing_text[self.typing_index]
        self.typing_index += 1
        self._set_text(self.typing_text[: self.typing_index])
        if self.typing_timer is not None:
            self.typing_timer.setInterval(self._typewriter_delay(char))

    def _typewriter_delay(self, char: str) -> int:
        if char in "。！？!?":
            delay = scaled_px(220, self.ui_scale)
        elif char in "、，,；;：:":
            delay = scaled_px(92, self.ui_scale)
        else:
            delay = max(22, min(46, scaled_px(34, self.ui_scale)))
        return max(8, round(delay / self.typewriter_speed))

    def _complete(self) -> None:
        if self.typing_timer is not None:
            self.typing_timer.stop()
            self.typing_timer.deleteLater()
            self.typing_timer = None
        callback = self.typing_finished_callback
        self.typing_finished_callback = None
        if callback:
            callback()
