from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QKeyEvent, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget


MIN_SELECTION_SIZE = 24


class ScreenshotSelectionOverlay(QWidget):
    selection_finished = Signal(dict)
    selection_cancelled = Signal(str)

    def __init__(self, screen: Any | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.target_screen = screen or QGuiApplication.primaryScreen()
        self._origin: QPoint | None = None
        self._current: QPoint | None = None
        self._selecting = False
        self.setObjectName("screenshotSelectionOverlay")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._fit_to_screen()

    def begin(self) -> None:
        self._fit_to_screen()
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def selected_rect(self) -> QRect:
        if self._origin is None or self._current is None:
            return QRect()
        return QRect(self._origin, self._current).normalized().intersected(self.rect())

    def cancel(self, reason: str = "cancelled") -> None:
        self.hide()
        self.selection_cancelled.emit(reason)
        self.close()

    def _fit_to_screen(self) -> None:
        if self.target_screen is None:
            self.target_screen = QGuiApplication.primaryScreen()
        geometry = self.target_screen.geometry() if self.target_screen is not None else QRect(0, 0, 1, 1)
        self.setGeometry(geometry)

    def _finish_selection(self) -> None:
        local_rect = self.selected_rect()
        if local_rect.width() < MIN_SELECTION_SIZE or local_rect.height() < MIN_SELECTION_SIZE:
            self.cancel("截图区域太小")
            return

        screen_geometry = self.geometry()
        logical_rect = QRect(local_rect)
        logical_rect.translate(screen_geometry.topLeft())
        payload = {
            "screen": self.target_screen,
            "screen_name": self.target_screen.name() if self.target_screen is not None else "",
            "screen_index": _screen_index(self.target_screen),
            "device_pixel_ratio": float(self.target_screen.devicePixelRatio()) if self.target_screen is not None else 1.0,
            "logical_rect": logical_rect,
        }
        self.hide()
        self.selection_finished.emit(payload)
        self.close()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(8, 14, 18, 142))

        rect = self.selected_rect()
        if not rect.isEmpty():
            painter.fillRect(rect, QColor(255, 255, 255, 28))
            pen = QPen(QColor(70, 220, 255, 238), 2)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawRect(rect.adjusted(1, 1, -1, -1))
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._origin = event.position().toPoint()
        self._current = self._origin
        self._selecting = True
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if not self._selecting:
            return
        self._current = event.position().toPoint()
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() != Qt.MouseButton.LeftButton or not self._selecting:
            return
        self._current = event.position().toPoint()
        self._selecting = False
        self.update()
        self._finish_selection()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Escape:
            self.cancel("cancelled")
            return
        super().keyPressEvent(event)


def _screen_index(screen: Any | None) -> int:
    screens = list(QGuiApplication.screens() or [])
    if screen in screens:
        return screens.index(screen)
    return -1
