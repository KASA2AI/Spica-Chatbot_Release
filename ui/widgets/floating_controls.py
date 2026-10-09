"""Small, non-activating controls alongside the existing floating character."""

import html
import math

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QToolButton, QWidget

from ui.widgets.icons import line_icon


class _VoiceIndicator(QWidget):
    """A listening/activity indicator, not an audio volume meter."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(20, 20)
        self.mode = 'idle'
        self.reduced_motion = False
        self.phase = 0.
        self.timer = QTimer(self)
        self.timer.setInterval(90)
        self.timer.timeout.connect(self._tick)

    def set_mode(self, mode, reduced_motion):
        self.mode, self.reduced_motion = mode, reduced_motion
        self._sync()
        self.update()

    def _sync(self):
        if self.isVisible() and not self.reduced_motion and self.mode in {'listening', 'speaking', 'thinking'}:
            if not self.timer.isActive():
                self.timer.start()
        else:
            self.timer.stop()

    def _tick(self):
        self.phase += .35
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = '#8dabc4' if self.mode in {'listening', 'speaking'} else '#a58bb6'
        if self.mode in {'muted', 'unavailable'}:
            color = '#a69ba9'
        elif self.mode == 'error':
            color = '#c17f91'
        painter.setBrush(QColor(color))
        painter.setPen(Qt.PenStyle.NoPen)
        if self.mode in {'listening', 'speaking'}:
            for i in range(4):
                height = 5 + (i % 2) * 4 if self.reduced_motion else 4 + 9 * abs(math.sin(self.phase + i * .7))
                painter.drawRoundedRect(QRectF(2 + i * 4, (20 - height) / 2, 2.5, height), 1.2, 1.2)
        elif self.mode == 'thinking':
            for i in range(3):
                painter.setOpacity(1. if self.reduced_motion else .4 + .6 * abs(math.sin(self.phase - i)))
                painter.drawEllipse(QRectF(2 + i * 6, 8, 3, 3))
        else:
            painter.drawEllipse(QRectF(6, 6, 8, 8))


class FloatingControls(QWidget):
    wake_requested = Signal()
    pet_requested = Signal()
    expand_requested = Signal()
    notice_requested = Signal()

    def __init__(self, parent):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setWindowTitle('Spica · 桌宠小工具')
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._closed = False
        self.setStyleSheet('''
            QFrame#petStatus { background: rgba(250,247,253,230);
                border: 1px solid #e5dce9; border-radius: 14px; }
            QFrame#petTools { background: rgba(252,249,253,242);
                border: 1px solid #e5dce9; border-radius: 16px; }
            QToolButton { color: #927b99; background: rgba(251,248,253,230);
                border: 1px solid #e5dce9; border-radius: 13px;
                padding: 0; font-size: 20px; }
            QToolButton:hover { background: #eee4f3; border-color: #baa1c7; }
            QToolButton:pressed { background: #e5d5ed; }
            QToolButton:disabled { color: #b1a8b7; }
            QToolButton[petAction="true"] { color: #87758e; background: transparent;
                border: none; border-radius: 10px; font-size: 10px; }
            QToolButton[petAction="true"]:hover { background: #eee5f2; }
            QToolButton[petAction="true"]:pressed { background: #e3d4ec; }
            QToolButton[petAction="true"]:disabled { color: #b1a8b7; }
        ''')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(4)
        self.status = QFrame(self)
        self.status.setObjectName('petStatus')
        self.status.setFixedSize(28, 28)
        row = QHBoxLayout(self.status)
        row.setContentsMargins(4, 4, 4, 4)
        self.indicator = _VoiceIndicator(self.status)
        row.addWidget(self.indicator)
        layout.addWidget(self.status)
        self.notice_button = self._button('✓', '看完了，收起这张纸条', self.notice_requested.emit)
        self.notice_button.setAccessibleName('看完了，收起这张纸条')
        layout.addWidget(self.notice_button)
        self.toolbar = QFrame(self)
        self.toolbar.setObjectName('petTools')
        tools = QHBoxLayout(self.toolbar)
        tools.setContentsMargins(6, 5, 6, 5)
        tools.setSpacing(2)
        self.talk_button = self._action_button('说话', 'microphone', '#7799b6', '唤醒当前角色', self.wake_requested)
        self.pet_button = self._action_button('摸头', 'petting', '#b687a6', '轻轻摸一下头', self.pet_requested)
        self.expand_button = self._action_button('展开', 'expand', '#9485ac', '展开完整界面', self.expand_requested)
        for button in (self.talk_button, self.pet_button, self.expand_button):
            tools.addWidget(button)
        layout.addWidget(self.toolbar)
        self.hide()

    def _button(self, text, tip, callback):
        button = QToolButton(self)
        button.setText(text)
        button.setFixedSize(26, 26)
        button.setToolTip(tip)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda _checked=False: callback())
        return button

    def _action_button(self, text, icon, color, tip, signal):
        button = self._button(text, tip, signal.emit)
        button.setProperty('petAction', True)
        button.setAccessibleName(tip)
        button.setFixedSize(40, 44)
        button.setIcon(line_icon(icon, color=color))
        button.setIconSize(QSize(20, 20))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        return button

    def present(self, *, text, mode, hovered, notice_title='', interactive=True,
                reduced_motion=False, show_status=True):
        if self._closed:
            return
        self.status.setToolTip(html.escape(text))
        self.status.setAccessibleName(text)
        self.indicator.set_mode(mode, reduced_motion)
        status_visible = bool(show_status and mode in {'listening', 'speaking', 'thinking', 'muted', 'error'})
        self.status.setVisible(status_visible)
        self.toolbar.setVisible(hovered)
        self.talk_button.setText('说话')
        self.talk_button.setToolTip('唤醒当前角色')
        self.pet_button.setEnabled(interactive)
        self.notice_button.setVisible(bool(notice_title))
        self.notice_button.setToolTip('看完了，收起这张纸条：' + html.escape(notice_title))
        self.adjustSize()
        self.setVisible(bool(hovered or notice_title or status_visible))

    def place_near(self, anchor):
        screen = QGuiApplication.screenAt(anchor.center()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        bounds = screen.availableGeometry().intersected(screen.geometry()).adjusted(6, 6, -6, -6)
        x = anchor.center().x() - self.width() // 2
        x = max(bounds.left(), min(x, bounds.right() + 1 - self.width()))
        y = anchor.bottom() + 4
        if y + self.height() > bounds.bottom() + 1:
            y = anchor.top() - self.height() - 4
        y = max(bounds.top(), min(y, bounds.bottom() + 1 - self.height()))
        self.move(QPoint(x, y))

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self.indicator.timer.stop()
        self.hide()
        self.deleteLater()
