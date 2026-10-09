"""A small, non-interactive transcript surface anchored to the floating role."""

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QAbstractTextDocumentLayout, QBrush, QColor, QFont, QFontMetricsF, QGuiApplication, QLinearGradient, QPainter, QPainterPath, QPen, QPalette, QTextDocument
from PySide6.QtWidgets import QWidget


class FloatingDialogueBubble(QWidget):
    def __init__(self, parent):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.NoDropShadowWindowHint)
        self.setWindowTitle('Spica · 对话气泡')
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._speaker = ''
        self._text = ''
        self._tail_on_top = False
        self._tail_x = 100.
        self._document = QTextDocument(self)
        font = QFont(self.font())
        font.setPixelSize(14)
        self._document.setDefaultFont(font)
        self._document.setDocumentMargin(0)
        self._text_height = 0.
        self.resize(220, 86)

    def set_dialogue(self, speaker, text):
        speaker, text = str(speaker), str(text or '…')
        if (speaker, text) == (self._speaker, self._text):
            return
        self._speaker, self._text = speaker, text
        metrics = QFontMetricsF(self._document.defaultFont())
        line_width = max((metrics.horizontalAdvance(line) for line in text.splitlines()), default=0)
        width = max(190, min(290, int(line_width + 44)))
        # Plain text only; output cannot create links, images or markup.
        self._document.setPlainText(text)
        self._document.setTextWidth(width - 40)
        self._text_height = min(112., self._document.size().height())
        self.resize(width, int(self._text_height + 58))
        self.update()

    def clear(self):
        self.hide()
        self._speaker = self._text = ''
        self._document.clear()

    def place_near(self, anchor):
        screen = QGuiApplication.screenAt(anchor.center()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry().intersected(screen.geometry()).adjusted(8, 8, -8, -8)
        previous = (self.pos(), self._tail_on_top, self._tail_x)
        above = anchor.top() - self.height() - 6
        self._tail_on_top = above < available.top()
        y = anchor.bottom() + 6 if self._tail_on_top else above
        x = anchor.center().x() - self.width() // 2
        x = max(available.left(), min(x, available.right() + 1 - self.width()))
        y = max(available.top(), min(y, available.bottom() + 1 - self.height()))
        self._tail_x = max(28., min(anchor.center().x() - x, self.width() - 28.))
        self.move(QPoint(x, y))
        if previous != (self.pos(), self._tail_on_top, self._tail_x):
            self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        top = 14 if self._tail_on_top else 5
        body = QRectF(5, top, self.width() - 10, self.height() - 19)
        shape = QPainterPath()
        shape.addRoundedRect(body, 18, 18)
        tail = QPainterPath()
        edge = body.top() if self._tail_on_top else body.bottom()
        tip = edge - 10 if self._tail_on_top else edge + 10
        tail.moveTo(self._tail_x - 9, edge)
        tail.quadTo(self._tail_x - 5, tip, self._tail_x, tip)
        tail.quadTo(self._tail_x + 5, tip, self._tail_x + 9, edge)
        tail.closeSubpath()
        shape = shape.united(tail)
        painter.setPen(Qt.PenStyle.NoPen)
        for offset, alpha in ((3, 7), (2, 10), (1, 15)):
            painter.fillPath(shape.translated(0, offset), QColor(48, 65, 103, alpha))
        fill = QLinearGradient(body.topLeft(), body.bottomRight())
        fill.setColorAt(0, QColor(247, 254, 255, 248))
        fill.setColorAt(1, QColor(247, 240, 255, 248))
        painter.fillPath(shape, fill)
        border = QLinearGradient(body.topLeft(), body.bottomRight())
        border.setColorAt(0, QColor('#9cdce5'))
        border.setColorAt(1, QColor('#d7bce6'))
        painter.setPen(QPen(QBrush(border), 1.2))
        painter.drawPath(shape)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#8ecbdc'))
        painter.drawEllipse(QPointF(22, top + 17), 2.5, 2.5)
        heading = QFont(self.font())
        heading.setPixelSize(11)
        heading.setWeight(QFont.Weight.DemiBold)
        painter.setFont(heading)
        painter.setPen(QColor('#678098'))
        label = QFontMetricsF(heading).elidedText(self._speaker, Qt.TextElideMode.ElideRight, self.width() - 54)
        painter.drawText(QRectF(32, top + 7, self.width() - 52, 20), Qt.AlignmentFlag.AlignVCenter, label)
        painter.save()
        text_rect = QRectF(20, top + 32, self.width() - 40, self._text_height)
        painter.setClipRect(text_rect)
        # Long current lines follow their newest text within the compact bubble.
        offset = max(0., self._document.size().height() - self._text_height)
        painter.translate(text_rect.left(), text_rect.top() - offset)
        context = QAbstractTextDocumentLayout.PaintContext()
        context.palette.setColor(QPalette.ColorRole.Text, QColor('#35495f'))
        self._document.documentLayout().draw(painter, context)
        painter.restore()
