from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLineEdit, QPushButton, QWidget

from ui.widgets.common import scaled_px
from ui.widgets.icons import _microphone_icon, _screenshot_icon


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
        self.setStyleSheet(
            """
            QFrame#inputPanel {
                background-color: rgba(255, 255, 255, 154);
                border: 1px solid rgba(25, 151, 181, 76);
                border-radius: 25px;
            }
            QLineEdit#messageInput {
                background-color: rgba(255, 255, 255, 118);
                border: 1px solid rgba(25, 151, 181, 72);
                border-radius: 18px;
                color: #253744;
                padding: 8px 13px;
                font-size: 15px;
            }
            QLineEdit#messageInput:focus {
                border: 1px solid rgba(25, 151, 181, 165);
            }
            QPushButton#sendButton {
                background-color: #168FC5;
                border: 0;
                border-radius: 18px;
                color: white;
                font-size: 15px;
                font-weight: 700;
                padding: 0 20px;
            }
            QPushButton#sendButton:disabled {
                background-color: rgba(22, 143, 197, 118);
            }
            QPushButton#stopButton {
                background-color: #D9534F;
                border: 0;
                border-radius: 18px;
                color: white;
                font-size: 15px;
                font-weight: 700;
                padding: 0 18px;
            }
            QPushButton#stopButton:hover {
                background-color: #C9302C;
            }
            QPushButton#voiceButton {
                background-color: rgba(255, 255, 255, 150);
                border: 1px solid rgba(25, 151, 181, 98);
                border-radius: 19px;
            }
            QPushButton#voiceButton:hover {
                background-color: rgba(234, 248, 255, 210);
                border: 1px solid rgba(22, 143, 197, 155);
            }
            QPushButton#voiceButton:checked {
                background-color: rgba(167, 232, 255, 180);
                border: 2px solid rgba(22, 143, 197, 205);
            }
            QPushButton#screenshotButton {
                background-color: rgba(255, 255, 255, 150);
                border: 1px solid rgba(25, 151, 181, 98);
                border-radius: 19px;
            }
            QPushButton#screenshotButton:hover {
                background-color: rgba(234, 248, 255, 210);
                border: 1px solid rgba(22, 143, 197, 155);
            }
            QPushButton#screenshotButton:checked {
                background-color: rgba(167, 232, 255, 180);
                border: 2px solid rgba(22, 143, 197, 205);
            }
            """
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        self.input = QLineEdit(self)
        self.input.setObjectName("messageInput")
        self.input.setPlaceholderText("spica......")
        self.input.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self.input.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.input.setInputMethodHints(Qt.InputMethodHint.ImhNone)
        self.input.returnPressed.connect(self.send_requested.emit)

        self.voice_button = QPushButton(self)
        self.voice_button.setObjectName("voiceButton")
        self.voice_button.setCheckable(True)
        self.voice_button.setToolTip("语音模式")
        self.voice_button.setFixedSize(38, 38)
        self.voice_button.setIcon(_microphone_icon())
        self.voice_button.setIconSize(QSize(26, 26))
        self.voice_button.clicked.connect(lambda _checked=False: self.voice_requested.emit(self.voice_button.isChecked()))

        self.screenshot_button = QPushButton(self)
        self.screenshot_button.setObjectName("screenshotButton")
        self.screenshot_button.setCheckable(True)
        self.screenshot_button.setToolTip("截图并随下一条消息发送给 Spica 查看")
        self.screenshot_button.setFixedSize(38, 38)
        self.screenshot_button.setIcon(_screenshot_icon())
        self.screenshot_button.setIconSize(QSize(26, 26))
        self.screenshot_button.clicked.connect(lambda _checked=False: self.screenshot_requested.emit())

        self.send_button = QPushButton("发送", self)
        self.send_button.setObjectName("sendButton")
        self.send_button.setFixedHeight(38)
        self.send_button.clicked.connect(lambda _checked=False: self.send_requested.emit())

        # B: cross-mode stop affordance. Hidden by default; shown ONLY while a chat/
        # reaction turn is in flight (set_turn_active), so the user can stop her
        # speaking in EITHER input mode without voice barge-in. Click -> stop_current
        # (rides #1's worker.cancel, halting the backend producer cleanly).
        self.stop_button = QPushButton("停止", self)
        self.stop_button.setObjectName("stopButton")
        self.stop_button.setFixedHeight(38)
        self.stop_button.setToolTip("停止 Spica 说话")
        self.stop_button.hide()
        self.stop_button.clicked.connect(lambda _checked=False: self.stop_requested.emit())

        layout.addWidget(self.input, 1)
        layout.addWidget(self.screenshot_button)
        layout.addWidget(self.voice_button)
        layout.addWidget(self.send_button)
        layout.addWidget(self.stop_button)
        self.apply_scale(1.0)

    def set_busy(self, busy: bool, voice_enabled: bool = True, *, input_enabled: bool | None = None) -> None:
        # A: input + send track input_enabled, NOT busy -- so the user can type to
        # interrupt while she speaks (turn active -> enabled), yet stay locked while a
        # mic recording segment is in flight (input_enabled=False) to avoid a double
        # turn. Default None falls back to `not busy` (pre-A behaviour), so bare
        # callers -- including test_screenshot_ui -- are byte-identical. screenshot
        # stays `not busy` (test-pinned); voice stays voice_enabled.
        text_enabled = (not busy) if input_enabled is None else input_enabled
        self.input.setEnabled(text_enabled)
        self.send_button.setEnabled(text_enabled)
        self.screenshot_button.setEnabled(not busy)
        self.voice_button.setEnabled(voice_enabled)

    def set_turn_active(self, active: bool) -> None:
        """B: show the stop button exactly while a chat/reaction turn is in flight.
        Visibility is the ONLY gate, and it tracks the chat-stream busy state -- never
        the input mode -- so the button is reachable in voice mode too (where she
        cannot be interrupted by voice). Cross-mode by construction."""
        self.stop_button.setVisible(active)

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

    def apply_scale(self, scale: float) -> None:
        button_size = scaled_px(38, scale)
        button_radius = button_size // 2
        send_height = scaled_px(38, scale)
        font_size = scaled_px(15, scale)
        padding_v = scaled_px(8, scale)
        padding_h = scaled_px(13, scale)
        panel_radius = scaled_px(25, scale)
        input_radius = scaled_px(18, scale)
        icon_size = scaled_px(28, scale)
        self.setStyleSheet(
            f"""
            QFrame#inputPanel {{
                background-color: rgba(255, 255, 255, 154);
                border: 1px solid rgba(25, 151, 181, 76);
                border-radius: {panel_radius}px;
            }}
            QLineEdit#messageInput {{
                background-color: rgba(255, 255, 255, 118);
                border: 1px solid rgba(25, 151, 181, 72);
                border-radius: {input_radius}px;
                color: #253744;
                padding: {padding_v}px {padding_h}px;
                font-size: {font_size}px;
            }}
            QLineEdit#messageInput:focus {{
                border: 1px solid rgba(25, 151, 181, 165);
            }}
            QPushButton#sendButton {{
                background-color: #168FC5;
                border: 0;
                border-radius: {input_radius}px;
                color: white;
                font-size: {font_size}px;
                font-weight: 700;
                padding: 0 {scaled_px(20, scale)}px;
            }}
            QPushButton#sendButton:disabled {{
                background-color: rgba(22, 143, 197, 118);
            }}
            QPushButton#stopButton {{
                background-color: #D9534F;
                border: 0;
                border-radius: {input_radius}px;
                color: white;
                font-size: {font_size}px;
                font-weight: 700;
                padding: 0 {scaled_px(18, scale)}px;
            }}
            QPushButton#stopButton:hover {{
                background-color: #C9302C;
            }}
            QPushButton#voiceButton {{
                background-color: rgba(255, 255, 255, 150);
                border: 1px solid rgba(25, 151, 181, 98);
                border-radius: {button_radius}px;
            }}
            QPushButton#voiceButton:hover {{
                background-color: rgba(234, 248, 255, 210);
                border: 1px solid rgba(22, 143, 197, 155);
            }}
            QPushButton#voiceButton:checked {{
                background-color: rgba(167, 232, 255, 180);
                border: 2px solid rgba(22, 143, 197, 205);
            }}
            QPushButton#screenshotButton {{
                background-color: rgba(255, 255, 255, 150);
                border: 1px solid rgba(25, 151, 181, 98);
                border-radius: {button_radius}px;
            }}
            QPushButton#screenshotButton:hover {{
                background-color: rgba(234, 248, 255, 210);
                border: 1px solid rgba(22, 143, 197, 155);
            }}
            QPushButton#screenshotButton:checked {{
                background-color: rgba(167, 232, 255, 180);
                border: 2px solid rgba(22, 143, 197, 205);
            }}
            """
        )
        self.layout().setContentsMargins(scaled_px(12, scale), scaled_px(8, scale), scaled_px(12, scale), scaled_px(8, scale))
        self.layout().setSpacing(scaled_px(8, scale))
        self.screenshot_button.setFixedSize(button_size, button_size)
        self.screenshot_button.setIconSize(QSize(max(1, icon_size - 2), max(1, icon_size - 2)))
        self.voice_button.setFixedSize(button_size, button_size)
        self.voice_button.setIconSize(QSize(max(1, icon_size - 2), max(1, icon_size - 2)))
        self.send_button.setFixedHeight(send_height)
        self.stop_button.setFixedHeight(send_height)
