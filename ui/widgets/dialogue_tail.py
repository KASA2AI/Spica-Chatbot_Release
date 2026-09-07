"""The supplied game's 68-frame sentence-end marker, cached in its original color."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QWidget

from ui.widgets.common import scaled_px


class DialogueTail(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        atlas = QPixmap(str(Path(__file__).resolve().parents[1] / "assets" / "dialogue_tail.png"))
        self._frames: list[QPixmap] = []
        if not atlas.isNull():
            self._frames = [atlas.copy((i % 17) * 81, (i // 17) * 41, 81, 41) for i in range(68)]
        self._frame_index = 0
        self._requested = False
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self._advance)
        self.apply_scale(1.0)
        self.hide()

    def apply_scale(self, scale: float) -> None:
        self._scale = scale
        self.setFixedSize(scaled_px(43, scale), scaled_px(24, scale))
        self.update()

    def restart(self) -> None:
        self._frame_index = 0
        self.update()

    def set_running(self, running: bool) -> None:
        self._requested = bool(running)
        self.setVisible(self._requested and bool(self._frames))
        if self._requested and self.isVisible() and self._frames:
            self.timer.start()
        else:
            self.timer.stop()

    def _advance(self) -> None:
        self._frame_index = (self._frame_index + 1) % len(self._frames)
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._requested and self._frames:
            self.timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self.timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        if not self._frames:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(
            QRect(scaled_px(1, self._scale), scaled_px(1, self._scale),
                  scaled_px(41, self._scale), scaled_px(21, self._scale)),
            self._frames[self._frame_index],
        )
