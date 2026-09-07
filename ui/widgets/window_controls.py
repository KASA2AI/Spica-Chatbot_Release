from __future__ import annotations

from PySide6.QtCore import QSize, Signal, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton, QWidget

from ui.widgets.common import scaled_px
from ui.widgets.icons import line_icon


class WindowControls(QFrame):
    settings_requested = Signal()
    minimize_requested = Signal()
    close_requested = Signal()  # The explicit exit action now lives in SettingsPanel.
    companion_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("windowControls")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self._companion_active = False
        layout = QHBoxLayout(self)
        self.settings_button = QPushButton(self)
        self.settings_button.setIcon(line_icon("settings"))
        self.settings_button.setToolTip("设置与退出")
        self.settings_button.clicked.connect(lambda _checked=False: self.settings_requested.emit())

        self.companion_button = QPushButton(self)
        self.companion_button.setIcon(line_icon("gamepad"))
        self.companion_button.setCheckable(True)
        self.companion_button.setToolTip("陪玩 galgame")
        self.companion_button.clicked.connect(self._on_companion_clicked)

        self.minimize_button = QPushButton(self)
        self.minimize_button.setIcon(line_icon("minimize"))
        self.minimize_button.setToolTip("最小化（保留会话和草稿）")
        self.minimize_button.clicked.connect(lambda _checked=False: self.minimize_requested.emit())
        layout.addWidget(self.companion_button)
        layout.addWidget(self.settings_button)
        layout.addWidget(self.minimize_button)
        for button in (self.settings_button, self.companion_button, self.minimize_button):
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.apply_scale(1.0)

    def _on_companion_clicked(self) -> None:
        # Checked state belongs exclusively to the backend event, never a click.
        self.companion_button.blockSignals(True)
        self.companion_button.setChecked(self._companion_active)
        self.companion_button.blockSignals(False)
        self.companion_requested.emit()

    def set_companion_active(self, active: bool) -> None:
        self._companion_active = bool(active)
        self.companion_button.blockSignals(True)
        self.companion_button.setChecked(self._companion_active)
        self.companion_button.blockSignals(False)
        self.companion_button.setToolTip("陪玩中（点击管理）" if active else "陪玩 galgame")

    def apply_scale(self, scale: float) -> None:
        self.setStyleSheet(
            f"""
            QFrame#windowControls {{ background: transparent; border: none; }}
            QPushButton {{
                border: 1px solid transparent;
                border-radius: {scaled_px(6, scale)}px;
                background: transparent;
                color: #ECF2F8;
                padding: 0;
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
            """
        )
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.layout().setSpacing(scaled_px(6, scale))
        for button, icon in (
            (self.settings_button, "settings"),
            (self.companion_button, "gamepad"),
            (self.minimize_button, "minimize"),
        ):
            button.setFixedSize(scaled_px(36, scale), scaled_px(36, scale))
            button.setIconSize(QSize(scaled_px(18, scale), scaled_px(18, scale)))
            button.setIcon(line_icon(icon, color="#DCE8F5"))
