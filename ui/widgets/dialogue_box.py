from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ui.widgets.common import DIALOG_FILTER_PATH, scaled_px


class TintedDialogueBox(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dialogueBox")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.background = QColor(234, 248, 255, 184)
        self.border = QColor(25, 151, 181, 74)
        self.filter_tint = QColor(167, 232, 255, 46)
        self.filter_image = QImage(str(DIALOG_FILTER_PATH))
        self.corner_radius = 18

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 24)
        layout.setSpacing(10)

        self.speaker_label = QLabel("spica", self)
        self.speaker_label.setObjectName("speakerLabel")
        self.speaker_label.setStyleSheet(
            "QLabel#speakerLabel { color: #1997B5; font-size: 16px; font-weight: 700; background: transparent; }"
        )

        self.text_label = QLabel("こんにちは。何を話しましょうか。", self)
        self.text_label.setObjectName("dialogueText")
        self.text_label.setWordWrap(True)
        self.text_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.text_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.text_label.setStyleSheet(
            "QLabel#dialogueText { color: #253744; font-size: 24px; line-height: 150%; background: transparent; }"
        )

        layout.addWidget(self.speaker_label)
        layout.addWidget(self.text_label, 1)
        self.apply_scale(1.0)

    def set_dialogue_text(self, text: str) -> None:
        self.text_label.setText(text or "……")

    def apply_scale(self, scale: float) -> None:
        self.layout().setContentsMargins(
            scaled_px(28, scale),
            scaled_px(22, scale),
            scaled_px(28, scale),
            scaled_px(24, scale),
        )
        self.layout().setSpacing(scaled_px(10, scale))
        self.speaker_label.setStyleSheet(
            f"QLabel#speakerLabel {{ color: #1997B5; font-size: {scaled_px(16, scale)}px; font-weight: 700; background: transparent; }}"
        )
        self.text_label.setStyleSheet(
            f"QLabel#dialogueText {{ color: #253744; font-size: {scaled_px(24, scale)}px; line-height: 150%; background: transparent; }}"
        )
        self.corner_radius = scaled_px(18, scale)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = self.corner_radius
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)

        painter.fillPath(path, self.background)
        self._paint_filter(painter, rect, path)
        painter.setPen(self.border)
        painter.drawPath(path)

    def _paint_filter(self, painter: QPainter, rect: QRectF, clip_path: QPainterPath) -> None:
        if self.filter_image.isNull():
            return

        target = rect.toRect().size()
        if target.isEmpty():
            return

        mask = self.filter_image.scaled(
            target,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)

        tinted = QImage(mask.size(), QImage.Format.Format_ARGB32_Premultiplied)
        tinted.fill(self.filter_tint)
        mask_painter = QPainter(tinted)
        mask_painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        mask_painter.drawImage(0, 0, mask)
        mask_painter.end()

        painter.save()
        painter.setClipPath(clip_path)
        painter.drawImage(rect.topLeft().toPoint(), tinted)
        painter.restore()

