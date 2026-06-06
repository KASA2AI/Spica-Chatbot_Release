from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QMouseEvent, QPainter
from PySide6.QtWidgets import QWidget

from ui.widgets.common import scaled_px


class CornerResizeHandle(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("cornerResizeHandle")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.setToolTip("缩放窗口")
        self.apply_scale(1.0)

    def apply_scale(self, scale: float) -> None:
        size = scaled_px(28, scale)
        self.setFixedSize(size, size)
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        parent = self.parentWidget()
        if event.button() == Qt.MouseButton.LeftButton and hasattr(parent, "_start_corner_resize"):
            parent._start_corner_resize(event)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        parent = self.parentWidget()
        if hasattr(parent, "_corner_resize_to"):
            parent._corner_resize_to(event)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        parent = self.parentWidget()
        if hasattr(parent, "_finish_corner_resize"):
            parent._finish_corner_resize(event)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QColor(22, 143, 197, 170))
        width = self.width()
        height = self.height()
        for offset in (7, 13, 19):
            x1 = max(3, width - offset)
            y1 = height - 4
            x2 = width - 4
            y2 = max(3, height - offset)
            painter.drawLine(x1, y1, x2, y2)

