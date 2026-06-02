from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QBrush, QIcon, QPainter, QPainterPath, QPen, QPixmap


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

