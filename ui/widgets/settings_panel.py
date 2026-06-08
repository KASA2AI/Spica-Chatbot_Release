from __future__ import annotations

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from spica.conversation.character_loader import DEFAULT_INTERLOCUTOR_NAME
from ui.widgets.common import MAX_UI_SCALE, MIN_UI_SCALE, scaled_px


class SettingsPanel(QFrame):
    costume_changed = Signal(str)
    interlocutor_name_changed = Signal(str)
    scale_changed = Signal(float)
    overall_scale_changed = Signal(float)
    typing_speed_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setStyleSheet(
            """
            QFrame#settingsPanel {
                background-color: rgba(238, 250, 255, 206);
                border: 1px solid rgba(25, 151, 181, 82);
                border-radius: 14px;
            }
            QLabel {
                background: transparent;
                color: #253744;
                font-size: 13px;
                font-weight: 600;
            }
            QComboBox,
            QLineEdit,
            QDoubleSpinBox {
                min-height: 28px;
                border: 1px solid rgba(25, 151, 181, 88);
                border-radius: 8px;
                background-color: rgba(255, 255, 255, 178);
                color: #253744;
                padding: 2px 8px;
            }
            QSlider::groove:horizontal {
                height: 5px;
                border-radius: 2px;
                background: rgba(25, 151, 181, 72);
            }
            QSlider::handle:horizontal {
                width: 16px;
                margin: -6px 0;
                border-radius: 8px;
                background: #168FC5;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(10)

        title = QLabel("设置", self)
        title.setObjectName("settingsTitle")
        layout.addWidget(title)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(10)

        self.name_input = QLineEdit(self)
        self.name_input.setPlaceholderText(DEFAULT_INTERLOCUTOR_NAME)
        self.name_input.editingFinished.connect(self._emit_interlocutor_name)

        self.costume_box = QComboBox(self)
        self.costume_box.currentTextChanged.connect(self.costume_changed.emit)

        scale_row = QWidget(self)
        scale_row.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        scale_layout = QHBoxLayout(scale_row)
        scale_layout.setContentsMargins(0, 0, 0, 0)
        scale_layout.setSpacing(8)

        self.scale_slider = QSlider(Qt.Orientation.Horizontal, scale_row)
        self.scale_slider.setRange(50, 180)
        self.scale_slider.setSingleStep(5)
        self.scale_slider.setPageStep(10)

        self.scale_spin = QDoubleSpinBox(scale_row)
        self.scale_spin.setRange(0.5, 1.8)
        self.scale_spin.setSingleStep(0.05)
        self.scale_spin.setDecimals(2)

        self.scale_slider.valueChanged.connect(self._slider_changed)
        self.scale_spin.valueChanged.connect(self._spin_changed)

        scale_layout.addWidget(self.scale_slider, 1)
        scale_layout.addWidget(self.scale_spin)

        overall_row = QWidget(self)
        overall_row.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        overall_layout = QHBoxLayout(overall_row)
        overall_layout.setContentsMargins(0, 0, 0, 0)
        overall_layout.setSpacing(8)

        self.overall_slider = QSlider(Qt.Orientation.Horizontal, overall_row)
        self.overall_slider.setRange(round(MIN_UI_SCALE * 100), round(MAX_UI_SCALE * 100))
        self.overall_slider.setSingleStep(5)
        self.overall_slider.setPageStep(10)

        self.overall_spin = QDoubleSpinBox(overall_row)
        self.overall_spin.setRange(MIN_UI_SCALE, MAX_UI_SCALE)
        self.overall_spin.setSingleStep(0.05)
        self.overall_spin.setDecimals(2)

        self.overall_slider.valueChanged.connect(self._overall_slider_changed)
        self.overall_spin.valueChanged.connect(self._overall_spin_changed)

        overall_layout.addWidget(self.overall_slider, 1)
        overall_layout.addWidget(self.overall_spin)

        typing_row = QWidget(self)
        typing_row.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        typing_layout = QHBoxLayout(typing_row)
        typing_layout.setContentsMargins(0, 0, 0, 0)
        typing_layout.setSpacing(8)

        self.typing_speed_slider = QSlider(Qt.Orientation.Horizontal, typing_row)
        self.typing_speed_slider.setRange(50, 300)
        self.typing_speed_slider.setSingleStep(10)
        self.typing_speed_slider.setPageStep(25)

        self.typing_speed_spin = QDoubleSpinBox(typing_row)
        self.typing_speed_spin.setRange(0.5, 3.0)
        self.typing_speed_spin.setSingleStep(0.1)
        self.typing_speed_spin.setDecimals(2)
        self.typing_speed_spin.setSuffix("x")

        self.typing_speed_slider.valueChanged.connect(self._typing_speed_slider_changed)
        self.typing_speed_spin.valueChanged.connect(self._typing_speed_spin_changed)

        typing_layout.addWidget(self.typing_speed_slider, 1)
        typing_layout.addWidget(self.typing_speed_spin)

        form.addRow("用户名", self.name_input)
        form.addRow("服装", self.costume_box)
        form.addRow("立绘缩放", scale_row)
        form.addRow("整体缩放", overall_row)
        form.addRow("文字速度", typing_row)
        layout.addLayout(form)
        self.apply_scale(1.0)

    def set_costumes(self, costumes: list[str], selected: str | None) -> None:
        self.costume_box.blockSignals(True)
        self.costume_box.clear()
        for costume in costumes:
            self.costume_box.addItem(costume)
        if selected and selected in costumes:
            self.costume_box.setCurrentText(selected)
        self.costume_box.blockSignals(False)

    def set_interlocutor_name(self, name: str) -> None:
        self.name_input.blockSignals(True)
        self.name_input.setText((name or DEFAULT_INTERLOCUTOR_NAME).strip() or DEFAULT_INTERLOCUTOR_NAME)
        self.name_input.blockSignals(False)

    def _emit_interlocutor_name(self) -> None:
        name = self.name_input.text().strip() or DEFAULT_INTERLOCUTOR_NAME
        self.name_input.setText(name)
        self.interlocutor_name_changed.emit(name)

    def set_scale(self, scale: float) -> None:
        value = max(0.5, min(1.8, float(scale)))
        self.scale_slider.blockSignals(True)
        self.scale_spin.blockSignals(True)
        self.scale_slider.setValue(round(value * 100))
        self.scale_spin.setValue(value)
        self.scale_slider.blockSignals(False)
        self.scale_spin.blockSignals(False)

    def _slider_changed(self, value: int) -> None:
        scale = value / 100
        self.scale_spin.blockSignals(True)
        self.scale_spin.setValue(scale)
        self.scale_spin.blockSignals(False)
        self.scale_changed.emit(scale)

    def _spin_changed(self, value: float) -> None:
        self.scale_slider.blockSignals(True)
        self.scale_slider.setValue(round(value * 100))
        self.scale_slider.blockSignals(False)
        self.scale_changed.emit(float(value))

    def set_overall_scale(self, scale: float) -> None:
        value = max(MIN_UI_SCALE, min(MAX_UI_SCALE, float(scale)))
        self.overall_slider.blockSignals(True)
        self.overall_spin.blockSignals(True)
        self.overall_slider.setValue(round(value * 100))
        self.overall_spin.setValue(value)
        self.overall_slider.blockSignals(False)
        self.overall_spin.blockSignals(False)

    def _overall_slider_changed(self, value: int) -> None:
        scale = value / 100
        self.overall_spin.blockSignals(True)
        self.overall_spin.setValue(scale)
        self.overall_spin.blockSignals(False)
        self.overall_scale_changed.emit(scale)

    def _overall_spin_changed(self, value: float) -> None:
        self.overall_slider.blockSignals(True)
        self.overall_slider.setValue(round(value * 100))
        self.overall_slider.blockSignals(False)
        self.overall_scale_changed.emit(float(value))

    def set_typing_speed(self, speed: float) -> None:
        value = max(0.5, min(3.0, float(speed)))
        self.typing_speed_slider.blockSignals(True)
        self.typing_speed_spin.blockSignals(True)
        self.typing_speed_slider.setValue(round(value * 100))
        self.typing_speed_spin.setValue(value)
        self.typing_speed_slider.blockSignals(False)
        self.typing_speed_spin.blockSignals(False)

    def _typing_speed_slider_changed(self, value: int) -> None:
        speed = value / 100
        self.typing_speed_spin.blockSignals(True)
        self.typing_speed_spin.setValue(speed)
        self.typing_speed_spin.blockSignals(False)
        self.typing_speed_changed.emit(speed)

    def _typing_speed_spin_changed(self, value: float) -> None:
        self.typing_speed_slider.blockSignals(True)
        self.typing_speed_slider.setValue(round(value * 100))
        self.typing_speed_slider.blockSignals(False)
        self.typing_speed_changed.emit(float(value))

    def apply_scale(self, scale: float) -> None:
        radius = scaled_px(14, scale)
        label_font = scaled_px(13, scale)
        title_font = scaled_px(15, scale)
        editor_height = scaled_px(28, scale)
        self.setStyleSheet(
            f"""
            QFrame#settingsPanel {{
                background-color: rgba(238, 250, 255, 206);
                border: 1px solid rgba(25, 151, 181, 82);
                border-radius: {radius}px;
            }}
            QLabel {{
                background: transparent;
                color: #253744;
                font-size: {label_font}px;
                font-weight: 600;
            }}
            QLabel#settingsTitle {{
                font-size: {title_font}px;
                font-weight: 800;
                color: #1997B5;
                background: transparent;
            }}
            QComboBox,
            QLineEdit,
            QDoubleSpinBox {{
                min-height: {editor_height}px;
                border: 1px solid rgba(25, 151, 181, 88);
                border-radius: {scaled_px(8, scale)}px;
                background-color: rgba(255, 255, 255, 178);
                color: #253744;
                padding: {scaled_px(2, scale)}px {scaled_px(8, scale)}px;
                font-size: {label_font}px;
            }}
            QSlider::groove:horizontal {{
                height: {scaled_px(5, scale)}px;
                border-radius: {scaled_px(2, scale)}px;
                background: rgba(25, 151, 181, 72);
            }}
            QSlider::handle:horizontal {{
                width: {scaled_px(16, scale)}px;
                margin: -{scaled_px(6, scale)}px 0;
                border-radius: {scaled_px(8, scale)}px;
                background: #168FC5;
            }}
            """
        )
        self.layout().setContentsMargins(scaled_px(14, scale), scaled_px(12, scale), scaled_px(14, scale), scaled_px(14, scale))
        self.layout().setSpacing(scaled_px(10, scale))

