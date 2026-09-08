from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPalette, QPixmap, QRegion
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLineEdit, QPushButton, QWidget

from ui.widgets.common import DEFAULT_DIALOGUE_OPACITY, scaled_px
from ui.widgets.dialogue_box import blue_veil
from ui.widgets.icons import line_icon


class MessageInput(QLineEdit):
    """A candidate-confirmation Enter is not a chat submission."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._ime_composing = False

    def inputMethodEvent(self, event) -> None:  # noqa: N802
        self._ime_composing = bool(event.preeditString())
        super().inputMethodEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self._ime_composing and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self._ime_composing = False


class InputPanel(QFrame):
    send_requested = Signal()
    voice_requested = Signal(bool)
    screenshot_requested = Signal()
    stop_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("inputPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self._opacity = DEFAULT_DIALOGUE_OPACITY
        self.style_art = None
        self._surface_key = None
        self._surface = QPixmap()
        self._hit_region = QRegion()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        self.input = MessageInput(self)
        self.input.setObjectName("messageInput")
        self.input.setPlaceholderText("说点什么…")
        self.input.setMinimumWidth(40)
        self.input.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self.input.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.input.setInputMethodHints(Qt.InputMethodHint.ImhNone)
        self.input.returnPressed.connect(self.send_requested.emit)

        self.voice_button = QPushButton(self)
        self.voice_button.setObjectName("voiceButton")
        self.voice_button.setCheckable(True)
        self.voice_button.setToolTip("语音模式")
        self.voice_button.setFixedSize(38, 38)
        self.voice_button.setIcon(line_icon("microphone"))
        self.voice_button.setIconSize(QSize(26, 26))
        self.voice_button.clicked.connect(lambda _checked=False: self.voice_requested.emit(self.voice_button.isChecked()))

        self.screenshot_button = QPushButton(self)
        self.screenshot_button.setObjectName("screenshotButton")
        self.screenshot_button.setCheckable(True)
        self.screenshot_button.setToolTip("截图并随下一条消息发送给 Spica 查看")
        self.screenshot_button.setFixedSize(38, 38)
        self.screenshot_button.setIcon(line_icon("screenshot"))
        self.screenshot_button.setIconSize(QSize(26, 26))
        self.screenshot_button.clicked.connect(lambda _checked=False: self.screenshot_requested.emit())

        self.send_button = QPushButton(self)
        self.send_button.setObjectName("sendButton")
        self.send_button.setAccessibleName("发送消息")
        self.send_button.setFixedHeight(38)
        self.send_button.clicked.connect(lambda _checked=False: self.send_requested.emit())

        # B: cross-mode stop affordance. Hidden by default; shown ONLY while a chat/
        # reaction turn is in flight (set_turn_active), so the user can stop her
        # speaking in EITHER input mode without voice barge-in. Click -> stop_current
        # (rides #1's worker.cancel, halting the backend producer cleanly).
        self.stop_button = QPushButton(self)
        self.stop_button.setObjectName("stopButton")
        self.stop_button.setAccessibleName("停止 Spica 说话")
        self.stop_button.setFixedHeight(38)
        self.stop_button.setToolTip("停止 Spica 说话")
        policy = self.stop_button.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.stop_button.setSizePolicy(policy)
        self.stop_button.hide()
        self.stop_button.clicked.connect(lambda _checked=False: self.stop_requested.emit())

        self._busy = False
        self._voice_enabled = True
        self._input_enabled: bool | None = None
        self._turn_active = False

        layout.addWidget(self.input, 1)
        layout.addWidget(self.screenshot_button)
        layout.addWidget(self.voice_button)
        layout.addWidget(self.send_button)
        layout.addWidget(self.stop_button)
        self.send_button.setIcon(line_icon("send"))
        self.stop_button.setIcon(line_icon("stop", color="#F4CAD2"))
        for button in (self.screenshot_button, self.voice_button, self.send_button, self.stop_button):
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_button.setToolTip("发送消息（Enter）")
        self.apply_scale(1.0)

    def set_busy(self, busy: bool, voice_enabled: bool = True, *, input_enabled: bool | None = None) -> None:
        # A: input + send track input_enabled, NOT busy -- so the user can type to
        # interrupt while she speaks (turn active -> enabled), yet stay locked while a
        # mic recording segment is in flight (input_enabled=False) to avoid a double
        # turn. Default None falls back to `not busy` (pre-A behaviour), so bare
        # callers -- including test_screenshot_ui -- are byte-identical. screenshot
        # stays `not busy` (test-pinned); voice stays voice_enabled.
        self._busy = bool(busy)
        self._voice_enabled = bool(voice_enabled)
        self._input_enabled = input_enabled
        self._apply_control_state()

    def set_turn_active(self, active: bool) -> None:
        """B: show the stop button exactly while a chat/reaction turn is in flight.
        Visibility is the ONLY gate, and it tracks the chat-stream busy state -- never
        the input mode -- so the button is reachable in voice mode too (where she
        cannot be interrupted by voice). Cross-mode by construction."""
        self._turn_active = bool(active)
        self._apply_control_state()


    def _apply_control_state(self) -> None:
        text_enabled = (
            not self._busy
            if self._input_enabled is None
            else bool(self._input_enabled)
        )
        self.input.setEnabled(text_enabled)
        self.send_button.setEnabled(text_enabled)
        self.screenshot_button.setEnabled(not self._busy)
        self.voice_button.setEnabled(self._voice_enabled)
        self.stop_button.setEnabled(True)
        self.stop_button.setVisible(self._turn_active)

    def set_voice_active(self, active: bool) -> None:
        self.voice_button.blockSignals(True)
        self.voice_button.setChecked(active)
        self.voice_button.blockSignals(False)
        self.voice_button.setToolTip("关闭语音模式" if active else "语音模式")

    def set_voice_transcript(self, text: str) -> None:
        """Display-only: mirror the recognized whole sentence into the input box
        while voice mode auto-submits it (driven by
        ``OverlayWindow._on_voice_recognized_text``). This is NOT an editable draft --
        the voice path never reads it back; it is cleared after a brief linger. A thin
        wrapper so the visualisation has a testable seam without poking ``input`` from
        the overlay."""
        self.input.setText(text)

    def clear_voice_transcript(self) -> None:
        """Clear the lingering voice transcript preview, returning the box to its
        placeholder. Pairs with :meth:`set_voice_transcript`."""
        self.input.clear()

    def set_screenshot_pending(self, active: bool) -> None:
        self.screenshot_button.blockSignals(True)
        self.screenshot_button.setChecked(active)
        self.screenshot_button.blockSignals(False)
        self.screenshot_button.setToolTip(
            "取消待发送截图" if active else "截图并随下一条消息发送给 Spica 查看"
        )

    def set_opacity(self, opacity: float) -> None:
        self._opacity = float(opacity)
        self.update()

    def set_style(self, art) -> None:
        self.style_art = art
        self.apply_scale(self._scale)

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        self._ensure_surface()
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._surface)

    def _ensure_surface(self) -> None:
        key = (self.width(), self.height(), self.devicePixelRatioF(), self._opacity, self._scale)
        if key == self._surface_key:
            return
        render = self.style_art.surface if self.style_art else blue_veil
        self._surface = render(self.size(), self.devicePixelRatioF(), self._opacity, footer=True, scale=self._scale)
        logical_surface = self._surface.scaled(
            self.size(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation,
        )
        logical_surface.setDevicePixelRatio(1.0)
        self._hit_region = QRegion(logical_surface.mask())
        self._surface_key = key

    def hit_region(self) -> QRegion:
        """Keep the feathered wings click-through, with full live control targets."""
        self._ensure_surface()
        region = QRegion(self._hit_region)
        for widget in (self.input, self.screenshot_button, self.voice_button, self.send_button, self.stop_button):
            if not widget.isHidden() and widget.isEnabled():
                region = region.united(QRegion(widget.geometry()))
        return region.intersected(QRegion(self.rect()))

    def apply_scale(self, scale: float) -> None:
        self._scale = scale
        button_size = scaled_px(36, scale)
        font_size = scaled_px(13, scale)
        self.setStyleSheet(
            f"""
            QFrame#inputPanel {{ background: transparent; border: none; }}
            QLineEdit#messageInput {{
                background: transparent;
                border: 1px solid transparent;
                border-radius: {scaled_px(5, scale)}px;
                padding: {scaled_px(3, scale)}px 0;
                color: #EDF2F8;
                selection-background-color: #A9C8E8;
                selection-color: #20344F;
                font-family: 'Segoe UI', 'Noto Sans CJK SC', 'Microsoft YaHei UI', sans-serif;
                font-size: {scaled_px(15, scale)}px;
            }}
            QLineEdit#messageInput:focus {{
                background: transparent;
                border-bottom: 1px solid rgba(205, 227, 250, 125);
            }}
            QPushButton {{
                border: 1px solid transparent;
                border-radius: {scaled_px(6, scale)}px;
                background: transparent;
                color: #ECF2F8;
                padding: 0;
                font-size: {font_size}px;
                font-weight: 400;
            }}
            QPushButton:hover, QPushButton:focus {{
                background: rgba(220, 236, 255, 28);
                border-color: rgba(220, 236, 255, 70);
            }}
            QPushButton:pressed {{ background: rgba(220, 236, 255, 50); }}
            QPushButton:checked {{
                background: rgba(180, 216, 246, 60);
                border-color: rgba(210, 235, 255, 110);
            }}
            QPushButton:disabled {{ color: rgba(220, 232, 245, 95); }}
            QPushButton#sendButton {{
                background: rgba(217, 233, 250, 20);
                border: 1px solid transparent;
            }}
            QPushButton#sendButton:hover, QPushButton#sendButton:focus {{
                background: rgba(217, 233, 250, 40);
            }}
            QPushButton#sendButton:pressed {{ background: rgba(217, 233, 250, 60); }}
            QPushButton#sendButton:disabled {{ background: rgba(217, 233, 250, 10); }}
            QPushButton#stopButton {{ color: #F4D7DD; }}
            QPushButton#stopButton:hover {{ background: rgba(181, 127, 146, 36); }}
            """
        )
        palette = self.input.palette()
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#C2D3E5"))
        self.input.setPalette(palette)
        self.input.setTextMargins(0, 0, 0, 0)
        self.layout().setContentsMargins(scaled_px(140, scale) - 3, scaled_px(6, scale), scaled_px(78, scale), scaled_px(25, scale))
        self.layout().setSpacing(scaled_px(8, scale))
        for button, icon in (
            (self.screenshot_button, "screenshot"),
            (self.voice_button, "microphone"),
            (self.send_button, "send"),
        ):
            button.setFixedSize(button_size, button_size)
            button.setIconSize(QSize(scaled_px(19, scale), scaled_px(19, scale)))
            button.setIcon(line_icon(icon, color="#DCE8F5"))
        self.stop_button.setFixedSize(scaled_px(52, scale), button_size)
        self.stop_button.setText("停止")
        self.stop_button.setIcon(line_icon("stop", color="#F4D7DD"))
        self.stop_button.setIconSize(QSize(scaled_px(15, scale), scaled_px(15, scale)))
        if self.style_art is not None:
            style = self.style_art.style
            self.layout().setContentsMargins(scaled_px(900 * style.layout.left, scale) - 3,
                scaled_px(6, scale), scaled_px(900 * style.layout.right, scale), scaled_px(25, scale))
            sheet = self.styleSheet().replace("#EDF2F8", style.colors.input).replace("#ECF2F8", style.colors.accent)
            sheet = sheet.replace("#F4D7DD", style.colors.accent).replace("#A9C8E8", style.colors.accent)
            accent = QColor(style.colors.accent)
            rgb = f"{accent.red()}, {accent.green()}, {accent.blue()}"
            sheet += f"""
                QLineEdit#messageInput:focus {{ border-bottom: 1px solid rgba({rgb}, 125); }}
                QFrame#inputPanel QPushButton:enabled {{ background: rgba({rgb}, 24); border-color: rgba({rgb}, 80); }}
                QFrame#inputPanel QPushButton:enabled:hover, QFrame#inputPanel QPushButton:enabled:focus {{
                    background: rgba({rgb}, 50); border-color: rgba({rgb}, 130);
                }}
                QFrame#inputPanel QPushButton:checked {{ background: rgba({rgb}, 70); border-color: rgba({rgb}, 150); }}
                QFrame#inputPanel QPushButton:enabled:pressed {{ background: rgba({rgb}, 85); border-color: rgba({rgb}, 175); }}
            """
            self.setStyleSheet(sheet)
            palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(style.colors.muted))
            self.input.setPalette(palette)
            for button, icon in ((self.screenshot_button, "screenshot"), (self.voice_button, "microphone"),
                                 (self.send_button, "send"), (self.stop_button, "stop")):
                button.setIcon(line_icon(icon, color=style.colors.accent))
        self._surface_key = None
        self.update()
