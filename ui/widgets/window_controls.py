from __future__ import annotations

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton, QWidget

from ui.widgets.common import scaled_px


class WindowControls(QFrame):
    settings_requested = Signal()
    minimize_requested = Signal()
    close_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("windowControls")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setStyleSheet(
            """
            QFrame#windowControls {
                background-color: rgba(38, 45, 52, 108);
                border: 1px solid rgba(255, 255, 255, 34);
                border-radius: 18px;
            }
            QPushButton {
                min-width: 28px;
                max-width: 28px;
                min-height: 28px;
                max-height: 28px;
                border: 0;
                border-radius: 14px;
                background-color: rgba(255, 255, 255, 34);
                color: #F3F8FB;
                font-size: 16px;
                font-weight: 700;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 70);
            }
            QPushButton:pressed {
                background-color: rgba(12, 18, 24, 92);
            }
            QPushButton#closeButton:hover {
                background-color: rgba(210, 72, 86, 168);
            }
            """
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 4, 5, 4)
        layout.setSpacing(4)

        self.settings_button = QPushButton("⚙", self)
        self.settings_button.setToolTip("设置")
        self.settings_button.clicked.connect(lambda _checked=False: self.settings_requested.emit())

        self.minimize_button = QPushButton("−", self)
        self.minimize_button.setToolTip("隐藏")
        self.minimize_button.clicked.connect(lambda _checked=False: self.minimize_requested.emit())

        self.close_button = QPushButton("×", self)
        self.close_button.setObjectName("closeButton")
        self.close_button.setToolTip("关闭")
        self.close_button.clicked.connect(lambda _checked=False: self.close_requested.emit())

        layout.addWidget(self.settings_button)
        layout.addWidget(self.minimize_button)
        layout.addWidget(self.close_button)
        self.apply_scale(1.0)

    def apply_scale(self, scale: float) -> None:
        button_size = scaled_px(28, scale)
        button_radius = button_size // 2
        font_size = scaled_px(16, scale)
        container_radius = scaled_px(18, scale)
        self.setStyleSheet(
            f"""
            QFrame#windowControls {{
                background-color: rgba(38, 45, 52, 108);
                border: 1px solid rgba(255, 255, 255, 34);
                border-radius: {container_radius}px;
            }}
            QPushButton {{
                min-width: {button_size}px;
                max-width: {button_size}px;
                min-height: {button_size}px;
                max-height: {button_size}px;
                border: 0;
                border-radius: {button_radius}px;
                background-color: rgba(255, 255, 255, 34);
                color: #F3F8FB;
                font-size: {font_size}px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                background-color: rgba(255, 255, 255, 70);
            }}
            QPushButton:pressed {{
                background-color: rgba(12, 18, 24, 92);
            }}
            QPushButton#closeButton:hover {{
                background-color: rgba(210, 72, 86, 168);
            }}
            """
        )
        self.layout().setContentsMargins(scaled_px(5, scale), scaled_px(4, scale), scaled_px(5, scale), scaled_px(4, scale))
        self.layout().setSpacing(scaled_px(4, scale))

