from __future__ import annotations

import math

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QBrush, QIcon, QPainter, QPainterPath, QPen, QPixmap


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


def _microphone_icon() -> QIcon:
    icon = QIcon()
    icon.addPixmap(
        _microphone_pixmap(QColor("#168FC5"), QColor("#EAF8FF")),
        QIcon.Mode.Normal,
        QIcon.State.Off,
    )
    icon.addPixmap(
        _microphone_pixmap(QColor("#0B7FA8"), QColor("#FFFFFF")),
        QIcon.Mode.Active,
        QIcon.State.Off,
    )
    icon.addPixmap(
        _microphone_pixmap(QColor("#087EA4"), QColor("#FFFFFF")),
        QIcon.Mode.Normal,
        QIcon.State.On,
    )
    icon.addPixmap(
        _microphone_pixmap(QColor(142, 158, 168), QColor(238, 245, 248)),
        QIcon.Mode.Disabled,
        QIcon.State.Off,
    )
    icon.addPixmap(
        _microphone_pixmap(QColor(142, 158, 168), QColor(238, 245, 248)),
        QIcon.Mode.Disabled,
        QIcon.State.On,
    )
    return icon


def _screenshot_icon() -> QIcon:
    icon = QIcon()
    icon.addPixmap(
        _screenshot_pixmap(QColor("#168FC5"), QColor("#EAF8FF")),
        QIcon.Mode.Normal,
        QIcon.State.Off,
    )
    icon.addPixmap(
        _screenshot_pixmap(QColor("#0B7FA8"), QColor("#FFFFFF")),
        QIcon.Mode.Active,
        QIcon.State.Off,
    )
    icon.addPixmap(
        _screenshot_pixmap(QColor("#087EA4"), QColor("#FFFFFF")),
        QIcon.Mode.Normal,
        QIcon.State.On,
    )
    icon.addPixmap(
        _screenshot_pixmap(QColor(142, 158, 168), QColor(238, 245, 248)),
        QIcon.Mode.Disabled,
        QIcon.State.Off,
    )
    icon.addPixmap(
        _screenshot_pixmap(QColor(142, 158, 168), QColor(238, 245, 248)),
        QIcon.Mode.Disabled,
        QIcon.State.On,
    )
    return icon


def _microphone_pixmap(color: QColor, accent: QColor, size: int = 64) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    body = QRectF(size * 0.34, size * 0.10, size * 0.32, size * 0.48)
    body_path = QPainterPath()
    body_path.addRoundedRect(body, body.width() / 2, body.width() / 2)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(color))
    painter.drawPath(body_path)

    slot_pen = QPen(accent, max(2, round(size * 0.045)))
    slot_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(slot_pen)
    center_x = round(size * 0.50)
    painter.drawLine(center_x, round(size * 0.21), center_x, round(size * 0.45))

    outline_pen = QPen(color, max(3, round(size * 0.07)))
    outline_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    outline_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(outline_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    cradle = QPainterPath()
    cradle.moveTo(size * 0.24, size * 0.40)
    cradle.lineTo(size * 0.24, size * 0.50)
    cradle.cubicTo(size * 0.24, size * 0.70, size * 0.76, size * 0.70, size * 0.76, size * 0.50)
    cradle.lineTo(size * 0.76, size * 0.40)
    painter.drawPath(cradle)
    painter.drawLine(center_x, round(size * 0.70), center_x, round(size * 0.82))
    painter.drawLine(round(size * 0.36), round(size * 0.82), round(size * 0.64), round(size * 0.82))

    painter.end()
    return pixmap


def _screenshot_pixmap(color: QColor, accent: QColor, size: int = 64) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    body = QRectF(size * 0.16, size * 0.23, size * 0.68, size * 0.50)
    body_path = QPainterPath()
    body_path.addRoundedRect(body, size * 0.10, size * 0.10)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(color))
    painter.drawPath(body_path)

    top = QRectF(size * 0.29, size * 0.15, size * 0.23, size * 0.12)
    top_path = QPainterPath()
    top_path.addRoundedRect(top, size * 0.04, size * 0.04)
    painter.drawPath(top_path)

    lens = QRectF(size * 0.36, size * 0.34, size * 0.28, size * 0.28)
    painter.setBrush(QBrush(accent))
    painter.drawEllipse(lens)
    painter.setBrush(QBrush(color))
    painter.drawEllipse(QRectF(size * 0.43, size * 0.41, size * 0.14, size * 0.14))

    flash = QRectF(size * 0.68, size * 0.32, size * 0.08, size * 0.08)
    painter.setBrush(QBrush(accent))
    painter.drawEllipse(flash)

    corner_pen = QPen(accent, max(2, round(size * 0.045)))
    corner_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    corner_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(corner_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    margin = size * 0.23
    length = size * 0.11
    painter.drawLine(round(margin), round(size * 0.82), round(margin + length), round(size * 0.82))
    painter.drawLine(round(margin), round(size * 0.82), round(margin), round(size * 0.82 - length))
    painter.drawLine(round(size - margin), round(size * 0.82), round(size - margin - length), round(size * 0.82))
    painter.drawLine(round(size - margin), round(size * 0.82), round(size - margin), round(size * 0.82 - length))

    painter.end()
    return pixmap
