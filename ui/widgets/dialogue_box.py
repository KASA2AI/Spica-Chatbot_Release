from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap, QRegion, QTextCharFormat, QTextLayout, QTextOption
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ui.widgets.common import DEFAULT_DIALOGUE_OPACITY, scaled_px
from ui.widgets.dialogue_tail import DialogueTail


@lru_cache(maxsize=1)
def _dialogue_artwork() -> QPixmap:
    return QPixmap(str(Path(__file__).resolve().parents[1] / "assets" / "dialogue_surface.png"))


def blue_veil(size: QSize, dpr: float, opacity: float, *, footer: bool = False, scale: float = 1.0) -> QPixmap:
    """Slice the approved transparent artwork; callers cache by size/DPR/opacity."""
    pixmap = QPixmap(max(1, round(size.width() * dpr)), max(1, round(size.height() * dpr)))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.translate(0, -153 * scale if footer else 0)
    painter.setOpacity(max(0.0, min(1.0, float(opacity))))
    artwork = _dialogue_artwork()
    painter.drawPixmap(QRectF(0, 0, size.width(), 220 * scale), artwork, QRectF(artwork.rect()))
    painter.end()
    return pixmap


class DialogueText(QLabel):
    """Plain-text QLabel seam with fixed line spacing and wheel overflow reading."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._scale = 1.0
        self._scroll_offset = 0.0
        self._text_height = 0.0
        self._text_layout = QTextLayout()
        self._tail_position = QPointF()
        self._hanging_quote = 0
        self._completed = False
        self.tail = DialogueTail(self)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def setText(self, text: str) -> None:  # noqa: N802
        previous = self.text()
        follow_end = bool(previous) and text.startswith(previous) and self._scroll_offset >= self._maximum_scroll() - 1
        super().setText(text)
        self._completed = False
        self._scroll_offset = self._scroll_offset if text.startswith(previous) else 0.0
        self._layout_text()
        if follow_end:
            self._scroll_offset = self._maximum_scroll()
            self._position_tail()
        self.update()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        # Long dialogue scrolls inside its allocation; QLabel must not request
        # a taller parent and push the input below the window.
        return QSize(0, 0)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(scaled_px(608, self._scale), scaled_px(87, self._scale))

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        del width
        return scaled_px(87, self._scale)

    def apply_scale(self, scale: float) -> None:
        self._scale = scale
        font = QFont()
        font.setFamilies(["Noto Sans CJK SC", "Microsoft YaHei UI", "Microsoft YaHei", "Yu Gothic UI"])
        font.setPixelSize(scaled_px(18, scale))
        font.setWeight(QFont.Weight.Medium)
        self.setFont(font)
        self.setStyleSheet(f"color: #F1F5FA; background: transparent; font-size: {scaled_px(18, scale)}px; font-weight: 500;")
        self.tail.apply_scale(scale)
        self._layout_text()
        self.update()

    def show_tail(self) -> None:
        self._completed = True
        self.tail.restart()
        self._position_tail()

    def hide_tail(self) -> None:
        self._completed = False
        self.tail.set_running(False)

    def _maximum_scroll(self) -> float:
        return max(0.0, self._text_height - self.height())

    def _layout_text(self) -> None:
        text = self.text().replace("\n", "\u2028")
        layout = QTextLayout(text, self.font())
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        layout.beginLayout()
        self._hanging_quote = self.fontMetrics().horizontalAdvance("「") if text.startswith("「") else 0
        line_height = max(29 * self._scale, self.fontMetrics().lineSpacing())
        y = 0.0
        last = None
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            inset = -self._hanging_quote if last is None else 0
            # Stable widths during typing: a growing prefix must not pull
            # already-visible characters back from the next line.
            reserve = 0 if last is None else 50 * self._scale
            line.setLineWidth(max(1, self.width() - inset - reserve))
            line.setPosition(QPointF(inset, y))
            y += line_height
            last = line
        layout.endLayout()
        self._text_layout = layout
        self._text_height = 0.0 if last is None else last.y() + max(last.height(), self.tail.height() + 4 * self._scale)
        self._tail_position = QPointF() if last is None else QPointF(last.x() + last.naturalTextWidth() + 7 * self._scale, last.y() + 4 * self._scale)
        if self._tail_position.x() + self.tail.width() > self.width():
            self._tail_position = QPointF(0, self._tail_position.y() + line_height)
            self._text_height = max(self._text_height, self._tail_position.y() + self.tail.height())
        self._scroll_offset = min(self._scroll_offset, self._maximum_scroll())
        self.setToolTip("内容较长，可用滚轮上下查看" if self._maximum_scroll() else "")
        self._position_tail()

    def _position_tail(self) -> None:
        x, y = self._tail_position.x(), self._tail_position.y() - self._scroll_offset
        self.tail.move(round(x), round(y))
        visible = self._completed and bool(self.text()) and y >= 0 and y + self.tail.height() <= self.height() + 1
        self.tail.set_running(visible)
        # The opening quote hangs outside this label's clipping rectangle;
        # repaint only its small strip on the parent when content/scroll changes.
        if self.parentWidget() is not None:
            width = scaled_px(30, self._scale)
            self.parentWidget().update(QRect(self.x() - width, self.y(), width, self.height()))

    def wheelEvent(self, event) -> None:  # noqa: N802
        maximum = self._maximum_scroll()
        if not maximum:
            event.ignore()
            return
        distance = event.pixelDelta().y() or event.angleDelta().y() / 120 * 29 * self._scale * 2
        self._scroll_offset = max(0.0, min(maximum, self._scroll_offset - distance))
        self._position_tail()
        self.update()
        event.accept()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._layout_text()

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        self.draw_text(painter, QPointF(0, -self._scroll_offset))

    def draw_text(self, painter: QPainter, origin: QPointF) -> None:
        outline = QTextCharFormat()
        outline.setForeground(QColor("#F1F5FA"))
        outline.setTextOutline(QPen(QColor(28, 46, 70, 180), 0.85 * self._scale))
        run = QTextLayout.FormatRange()
        run.start, run.length, run.format = 0, len(self.text()), outline
        self._text_layout.draw(painter, origin, [run])
        # Refill at the same position so the fine outline cannot hollow out
        # the glyph. No offset shadow or blur is applied to the text.
        painter.setPen(QColor("#F1F5FA"))
        self._text_layout.draw(painter, origin)


class TintedDialogueBox(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dialogueBox")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("按住对话框空白处拖动窗口")
        self._opacity = DEFAULT_DIALOGUE_OPACITY
        self._surface_key = None
        self._surface = QPixmap()
        self._hit_region = QRegion()
        layout = QVBoxLayout(self)
        self.speaker_label = QLabel("Spica", self)
        self.speaker_label.setObjectName("speakerLabel")
        self.speaker_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.speaker_label.setCursor(Qt.CursorShape.OpenHandCursor)
        self.speaker_label.setToolTip("按住姓名栏拖动窗口")
        self.text_label = DialogueText(self)
        self.text_label.setObjectName("dialogueText")
        self.tail = self.text_label.tail
        layout.addWidget(self.speaker_label)
        layout.addWidget(self.text_label, 1)
        self.apply_scale(1.0)
        self.set_dialogue_text("こんにちは。何を話しましょうか。")

    def set_dialogue_text(self, text: str) -> None:
        self._home_text = text or "……"
        self.text_label.setText(self._home_text)

    def set_typing_active(self, active: bool) -> None:
        del active
        self.hide_tail()

    def show_tail(self) -> None:
        self.text_label.show_tail()

    def hide_tail(self) -> None:
        self.text_label.hide_tail()

    def set_opacity(self, opacity: float) -> None:
        self._opacity = float(opacity)
        self.update()

    def drag_rect(self) -> QRect:
        return self.speaker_label.geometry().adjusted(0, 0, scaled_px(20, self._scale), scaled_px(5, self._scale))

    def hit_region(self) -> QRegion:
        self._ensure_surface()
        return self._hit_region

    def apply_scale(self, scale: float) -> None:
        self._scale = scale
        # Allocate rounding to the bottom padding so three rows still fit at
        # small scales (0.65 otherwise loses a pixel to independent rounding).
        bottom = max(0, scaled_px(153, scale) - scaled_px(29, scale) - scaled_px(26, scale)
                     - scaled_px(10, scale) - scaled_px(86, scale))
        self.layout().setContentsMargins(scaled_px(140, scale), scaled_px(29, scale), scaled_px(78, scale), bottom)
        self.layout().setSpacing(scaled_px(10, scale))
        self.speaker_label.setFixedSize(scaled_px(110, scale), scaled_px(26, scale))
        self.speaker_label.setStyleSheet(
            "color: #E6EEF8; font-family: 'Noto Sans CJK SC', 'Microsoft YaHei UI', sans-serif; "
            f"font-size: {scaled_px(16, scale)}px; font-weight: 500; background: transparent;"
        )
        self.text_label.apply_scale(scale)
        self._surface_key = None
        self.update()

    def _ensure_surface(self) -> None:
        dpr = self.devicePixelRatioF()
        key = (self.width(), self.height(), dpr, self._opacity, self._scale)
        if key == self._surface_key:
            return
        self._surface = blue_veil(self.size(), dpr, self._opacity, scale=self._scale)
        logical_surface = self._surface.scaled(self.size(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        logical_surface.setDevicePixelRatio(1.0)
        self._hit_region = QRegion(logical_surface.mask())
        self._surface_key = key

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        self._ensure_surface()
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._surface)
        text = self.text_label
        if text._hanging_quote and text._text_layout.lineCount():
            painter.setClipRect(QRect(text.x() - text._hanging_quote, text.y(), text._hanging_quote, text.height()))
            text.draw_text(painter, QPointF(text.x(), text.y() - text._scroll_offset))
