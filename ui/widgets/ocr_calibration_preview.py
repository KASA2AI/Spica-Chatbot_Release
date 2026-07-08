"""OCR calibration preview / confirm widget (Phase 6, ui/).

Shows the captured region (PNG bytes -> QPixmap), the recognized text (editable
for hand-correction), a suspect-blank warning, and confirm / reframe / save-edit
actions. Pure presentation: it receives Qt-free data (bytes + str) and emits
plain signals; the controller wires them to the Qt-free calibrator.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class OcrCalibrationPreview(QWidget):
    confirmed = Signal()
    reframe_requested = Signal()
    corrected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("OCR 区域校准预览")
        self._image = QLabel("（截图预览）")
        self._image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image.setMinimumSize(320, 120)
        self._warning = QLabel("")
        self._warning.setObjectName("ocrBlankWarning")
        self._warning.setWordWrap(True)
        self._text = QPlainTextEdit()
        self._text.setPlaceholderText("识别文本（可手改后保存）")

        confirm_btn = QPushButton("确认")
        reframe_btn = QPushButton("重新框选")
        correct_btn = QPushButton("保存手改文本")
        confirm_btn.clicked.connect(self.confirmed)
        reframe_btn.clicked.connect(self.reframe_requested)
        correct_btn.clicked.connect(lambda: self.corrected.emit(self._text.toPlainText()))

        buttons = QHBoxLayout()
        buttons.addWidget(confirm_btn)
        buttons.addWidget(reframe_btn)
        buttons.addWidget(correct_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self._image)
        layout.addWidget(self._warning)
        layout.addWidget(QLabel("识别文本（可手改）："))
        layout.addWidget(self._text)
        layout.addLayout(buttons)

    def show_preview(self, image_png: bytes, suspect_blank: bool) -> None:
        if image_png:
            pixmap = QPixmap()
            pixmap.loadFromData(image_png, "PNG")
            if not pixmap.isNull():
                self._image.setPixmap(pixmap)
        self._warning.setText(
            "⚠️ 截图疑似空白 / 黑屏（可能是 Wayland 会话或窗口被遮挡）——请确认确实截到了游戏画面。"
            if suspect_blank
            else ""
        )

    def show_text(self, text: str) -> None:
        self._text.setPlainText(text or "")
