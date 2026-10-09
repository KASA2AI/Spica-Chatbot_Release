from __future__ import annotations

import math

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap


def line_icon(name: str, *, color: str = "#EDF6FF") -> QIcon:
    """Small desktop controls, painted once at high resolution for HiDPI scaling."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(64 / 24, 64 / 24)
    pen = QPen(QColor(color), 1.45)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    path = QPainterPath()
    if name == "microphone":
        painter.drawRoundedRect(QRectF(9, 3, 6, 12), 3, 3)
        path.moveTo(6, 10)
        path.lineTo(6, 12)
        path.cubicTo(6, 20, 18, 20, 18, 12)
        path.lineTo(18, 10)
        path.moveTo(12, 18)
        path.lineTo(12, 22)
        path.moveTo(9, 22)
        path.lineTo(15, 22)
    elif name == "screenshot":
        path.moveTo(3, 7)
        path.lineTo(7, 7)
        path.lineTo(9, 4)
        path.lineTo(15, 4)
        path.lineTo(17, 7)
        path.lineTo(21, 7)
        path.lineTo(21, 20)
        path.lineTo(3, 20)
        path.closeSubpath()
        painter.drawEllipse(QRectF(8, 9, 8, 8))
    elif name == "send":
        path.moveTo(5, 11)
        path.lineTo(12, 4)
        path.lineTo(19, 11)
        path.moveTo(12, 4)
        path.lineTo(12, 21)
    elif name == "stop":
        painter.drawRoundedRect(QRectF(5, 5, 14, 14), 1, 1)
    elif name == "minimize":
        path.moveTo(5, 12)
        path.lineTo(19, 12)
    elif name == "close":
        path.moveTo(6, 6)
        path.lineTo(18, 18)
        path.moveTo(18, 6)
        path.lineTo(6, 18)
    elif name == "petting":
        path.moveTo(12, 20)
        path.cubicTo(8, 17, 3, 13, 3, 8)
        path.cubicTo(3, 3, 9, 2, 12, 7)
        path.cubicTo(15, 2, 21, 3, 21, 8)
        path.cubicTo(21, 13, 16, 17, 12, 20)
    elif name == "expand":
        path.moveTo(4, 10)
        path.lineTo(4, 4)
        path.lineTo(10, 4)
        path.moveTo(4, 4)
        path.lineTo(10, 10)
        path.moveTo(14, 20)
        path.lineTo(20, 20)
        path.lineTo(20, 14)
        path.moveTo(20, 20)
        path.lineTo(14, 14)
    elif name == "collapse":
        path.moveTo(3, 9)
        path.lineTo(9, 9)
        path.lineTo(9, 3)
        path.moveTo(3, 3)
        path.lineTo(9, 9)
        path.moveTo(15, 21)
        path.lineTo(15, 15)
        path.lineTo(21, 15)
        path.moveTo(15, 15)
        path.lineTo(21, 21)
    elif name == "settings":
        painter.drawEllipse(QRectF(8.5, 8.5, 7, 7))
        # Eight low-profile teeth rather than a platform-dependent gear emoji.
        for index in range(32):
            angle = index * math.pi / 16
            radius = 9 if index % 4 in (1, 2) else 7.5
            x, y = 12 + radius * math.cos(angle), 12 + radius * math.sin(angle)
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        path.closeSubpath()
    elif name == "alarm":
        painter.drawEllipse(QRectF(5, 5, 14, 14))
        path.moveTo(12, 8)
        path.lineTo(12, 12)
        path.lineTo(15, 14)
        path.moveTo(3, 5)
        path.lineTo(6, 2)
        path.moveTo(18, 2)
        path.lineTo(21, 5)
        path.moveTo(7, 18)
        path.lineTo(5, 21)
        path.moveTo(17, 18)
        path.lineTo(19, 21)
    elif name == "gamepad":
        path.moveTo(7, 5)
        path.cubicTo(3, 5, 1, 19, 4, 19)
        path.cubicTo(6, 19, 7, 15, 9, 15)
        path.lineTo(15, 15)
        path.cubicTo(17, 15, 18, 19, 20, 19)
        path.cubicTo(23, 19, 21, 5, 17, 5)
        path.closeSubpath()
        path.moveTo(5, 10)
        path.lineTo(10, 10)
        path.moveTo(7.5, 7.5)
        path.lineTo(7.5, 12.5)
        painter.drawEllipse(QRectF(15, 8, 1.5, 1.5))
        painter.drawEllipse(QRectF(18, 11, 1.5, 1.5))
    painter.drawPath(path)
    painter.end()
    return QIcon(pixmap)
