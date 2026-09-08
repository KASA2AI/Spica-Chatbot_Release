"""Qt rendering of an already validated dialogue style snapshot."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QLabel

from pathlib import Path
from spica.core.dialogue_style import DialogueStyle


class DialogueSpeakerLabel(QLabel):
    style_art = None

    def paintEvent(self, event) -> None:  # noqa: N802
        if self.style_art is None:
            super().paintEvent(event)
            return
        style = self.style_art.style
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metrics = self.fontMetrics()
        inset = max(0, self.indent())
        text = metrics.elidedText(self.text(), Qt.TextElideMode.ElideRight, max(1, self.width() - inset - 4))
        path = QPainterPath()
        path.addText(inset, (self.height() - metrics.height()) / 2 + metrics.ascent(), self.font(), text)
        scale = self.font().pixelSize() / style.text.speaker_size
        painter.setPen(QPen(QColor(style.colors.outline), style.text.outline_width * scale))
        painter.setBrush(QColor(style.colors.speaker))
        painter.drawPath(path)
        painter.fillPath(path, QColor(style.colors.speaker))


class DialogueStyleArt:
    def __init__(self, root: Path, style: DialogueStyle):
        self.style = style
        self.images = {key: QPixmap(str(root / path)) for key, path in self.style.images.items()}
        if any(image.isNull() for image in self.images.values()):
            raise ValueError("对话框图片加载失败。")

    def surface(self, size: QSize, dpr: float, opacity: float, *, scale=1.0, footer=False,
                full_height: float | None = None) -> QPixmap:
        result = QPixmap(max(1, round(size.width() * dpr)), max(1, round(size.height() * dpr)))
        result.setDevicePixelRatio(dpr)
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if footer:
            painter.translate(0, -153 * scale)
        layout = self.style.layout
        top = layout.surface_top * scale
        height = full_height if full_height is not None else 220 * scale
        painter.setOpacity(opacity * layout.surface_opacity)
        surface = self.images["surface"]
        painter.drawPixmap(QRectF(0, top, size.width(), max(1, height - top)), surface, QRectF(surface.rect()))
        if "nameplate" in self.images and not footer:
            plate = self.images["nameplate"]
            painter.setOpacity(opacity * layout.nameplate_opacity)
            painter.drawPixmap(QRectF(size.width() * layout.left, layout.top * scale,
                                      layout.name_width * scale, layout.name_height * scale),
                               plate, QRectF(plate.rect()))
        painter.end()
        return result
