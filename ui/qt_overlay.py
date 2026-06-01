from __future__ import annotations

import logging
import sys
import uuid
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, QSize, QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QBrush, QGuiApplication, QIcon, QImage, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap, QRegion
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from agent import SimpleAgent
from agent.character_loader import DEFAULT_INTERLOCUTOR_NAME
from agent_tools.function_tools.song import (
    CancellationToken,
    SongAction,
    SongContext,
    SongIntent,
    SongIntentRouter,
    SongPipeline,
    SongRequest,
    SongState,
    build_song_request_from_intent,
)
from agent_tools.tts import CURRENT_GPTSOVITS_PROVIDERS, GPTSoVITSTool, build_tts_adapter, load_tts_config
from agent_tools.visual import VisualDiffService
from hardware.respeaker.speech_worker import SpeechWorker, is_fatal_speech_error

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
except Exception:  # pragma: no cover - depends on the local Qt install
    QAudioOutput = None
    QMediaPlayer = None


BASE_DIR = Path(__file__).resolve().parents[1]
DIALOG_FILTER_PATH = BASE_DIR / "spica_data" / "diffs" / "ui" / "_mw_filter01.png"
DEBUG_NORMAL_WINDOW = False
MIN_UI_SCALE = 0.6
MAX_UI_SCALE = 1.8
MIN_WINDOW_SIZE = QSize(460, 360)
CHARACTER_HIT_ALPHA_THRESHOLD = 8
CHARACTER_HIT_MARGIN = 7
logger = logging.getLogger(__name__)


def scaled_px(value: float, scale: float) -> int:
    return max(1, round(value * scale))


def _microphone_icon() -> QIcon:
    icon = QIcon()
    icon.addPixmap(
        _microphone_pixmap(QColor("#168FC5"), QColor("#EAF8FF")),
        QIcon.Mode.Normal,
        QIcon.State.Off,
    )
    icon.addPixmap(
        _microphone_pixmap(QColor("#0B7FA8"), QColor("#FFFFFF")),
        QIcon.Mode.Active,
        QIcon.State.Off,
    )
    icon.addPixmap(
        _microphone_pixmap(QColor("#087EA4"), QColor("#FFFFFF")),
        QIcon.Mode.Normal,
        QIcon.State.On,
    )
    icon.addPixmap(
        _microphone_pixmap(QColor(142, 158, 168), QColor(238, 245, 248)),
        QIcon.Mode.Disabled,
        QIcon.State.Off,
    )
    icon.addPixmap(
        _microphone_pixmap(QColor(142, 158, 168), QColor(238, 245, 248)),
        QIcon.Mode.Disabled,
        QIcon.State.On,
    )
    return icon


def _microphone_pixmap(color: QColor, accent: QColor, size: int = 64) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    body = QRectF(size * 0.34, size * 0.10, size * 0.32, size * 0.48)
    body_path = QPainterPath()
    body_path.addRoundedRect(body, body.width() / 2, body.width() / 2)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(color))
    painter.drawPath(body_path)

    slot_pen = QPen(accent, max(2, round(size * 0.045)))
    slot_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(slot_pen)
    center_x = round(size * 0.50)
    painter.drawLine(center_x, round(size * 0.21), center_x, round(size * 0.45))

    outline_pen = QPen(color, max(3, round(size * 0.07)))
    outline_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    outline_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(outline_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    cradle = QPainterPath()
    cradle.moveTo(size * 0.24, size * 0.40)
    cradle.lineTo(size * 0.24, size * 0.50)
    cradle.cubicTo(size * 0.24, size * 0.70, size * 0.76, size * 0.70, size * 0.76, size * 0.50)
    cradle.lineTo(size * 0.76, size * 0.40)
    painter.drawPath(cradle)
    painter.drawLine(center_x, round(size * 0.70), center_x, round(size * 0.82))
    painter.drawLine(round(size * 0.36), round(size * 0.82), round(size * 0.64), round(size * 0.82))

    painter.end()
    return pixmap


class ChatWorker(QThread):
    stream_event = Signal(str, dict)
    failed = Signal(str)

    def __init__(
        self,
        agent: SimpleAgent,
        message: str,
        conversation_id: str,
        visual_overrides: dict[str, Any],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.agent = agent
        self.message = message
        self.conversation_id = conversation_id
        self.visual_overrides = visual_overrides

    def run(self) -> None:
        try:
            for event in self.agent.stream_voice(
                self.message,
                conversation_id=self.conversation_id,
                visual_overrides=self.visual_overrides,
            ):
                if self.isInterruptionRequested():
                    return
                if not isinstance(event, dict):
                    continue
                event_name = str(event.get("event") or "message")
                data = event.get("data") if isinstance(event.get("data"), dict) else {}
                self.stream_event.emit(event_name, data)
        except Exception as exc:
            self.failed.emit(str(exc))


class SongWorker(QThread):
    completed = Signal(int, dict)
    failed = Signal(int, str)

    def __init__(
        self,
        request: SongRequest,
        job_id: int,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.request = request
        self.job_id = job_id
        self.cancellation = CancellationToken()

    def cancel(self) -> None:
        self.cancellation.cancel()
        self.requestInterruption()

    def run(self) -> None:
        try:
            result = SongPipeline().run(self.request, self.cancellation)
            if self.isInterruptionRequested() or self.cancellation.cancelled():
                return
            if result.ok:
                self.completed.emit(self.job_id, result.to_payload())
            else:
                self.failed.emit(self.job_id, result.error or result.message or "唱歌任务失败。")
        except Exception as exc:
            if not self.isInterruptionRequested() and not self.cancellation.cancelled():
                self.failed.emit(self.job_id, str(exc))


class StartupWarmupWorker(QThread):
    status_changed = Signal(str)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        agent: SimpleAgent,
        tts_provider: Any,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.agent = agent
        self.tts_provider = tts_provider

    def run(self) -> None:
        try:
            model = str(getattr(self.agent, "model", "") or "unknown")
            self.status_changed.emit(f"LLM API 初始化完成：{model}")
            public_config = getattr(self.tts_provider, "public_config", None)
            warmup = getattr(self.tts_provider, "warmup", None)
            if public_config is None or warmup is None:
                provider_name = str(getattr(self.tts_provider, "name", None) or "TTS")
                self.finished_ok.emit(f"LLM API 已初始化，{provider_name} 无需启动预热。")
                return

            provider_name = str(getattr(self.tts_provider, "name", None) or "TTS")
            config = public_config()
            if not bool(config.get("warmup_on_startup", True)):
                self.finished_ok.emit(f"LLM API 已初始化，{provider_name} 启动预热已关闭。")
                return

            configured_emotions = config.get("warmup_emotions")
            if isinstance(configured_emotions, list) and configured_emotions:
                emotions = [str(item) for item in configured_emotions if str(item).strip()]
            else:
                emotions = [str(config.get("warmup_emotion") or "happy")]
            if not emotions:
                emotions = [str(config.get("warmup_emotion") or "happy")]

            self.status_changed.emit(f"正在预热 {provider_name} 模型...")
            results = [warmup(emotion=item, synthesize=True) for item in emotions]
            failed_results = [item for item in results if not item.get("ok")]
            total_duration_ms = sum(float(item.get("duration_ms") or 0) for item in results)
            if failed_results:
                messages = ", ".join(str(item.get("error") or "unknown") for item in failed_results)
                self.failed.emit(f"{provider_name} warmup failed：{messages}")
                return
            self.finished_ok.emit(f"{provider_name} 模型已就绪（{total_duration_ms:.0f}ms）。")
        except Exception as exc:
            self.failed.emit(f"启动预热失败：{exc}")


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


class InputPanel(QFrame):
    send_requested = Signal()
    voice_requested = Signal(bool)

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

        self.send_button = QPushButton("发送", self)
        self.send_button.setObjectName("sendButton")
        self.send_button.setFixedHeight(38)
        self.send_button.clicked.connect(lambda _checked=False: self.send_requested.emit())

        layout.addWidget(self.input, 1)
        layout.addWidget(self.voice_button)
        layout.addWidget(self.send_button)
        self.apply_scale(1.0)

    def set_busy(self, busy: bool, voice_enabled: bool = True) -> None:
        self.input.setEnabled(not busy)
        self.send_button.setEnabled(not busy)
        self.voice_button.setEnabled(voice_enabled)

    def set_voice_active(self, active: bool) -> None:
        self.voice_button.blockSignals(True)
        self.voice_button.setChecked(active)
        self.voice_button.blockSignals(False)
        self.voice_button.setToolTip("关闭语音模式" if active else "语音模式")

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
            """
        )
        self.layout().setContentsMargins(scaled_px(12, scale), scaled_px(8, scale), scaled_px(12, scale), scaled_px(8, scale))
        self.layout().setSpacing(scaled_px(8, scale))
        self.voice_button.setFixedSize(button_size, button_size)
        self.voice_button.setIconSize(QSize(max(1, icon_size - 2), max(1, icon_size - 2)))
        self.send_button.setFixedHeight(send_height)


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


class CornerResizeHandle(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("cornerResizeHandle")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.setToolTip("缩放窗口")
        self.apply_scale(1.0)

    def apply_scale(self, scale: float) -> None:
        size = scaled_px(28, scale)
        self.setFixedSize(size, size)
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        parent = self.parentWidget()
        if event.button() == Qt.MouseButton.LeftButton and hasattr(parent, "_start_corner_resize"):
            parent._start_corner_resize(event)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        parent = self.parentWidget()
        if hasattr(parent, "_corner_resize_to"):
            parent._corner_resize_to(event)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        parent = self.parentWidget()
        if hasattr(parent, "_finish_corner_resize"):
            parent._finish_corner_resize(event)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QColor(22, 143, 197, 170))
        width = self.width()
        height = self.height()
        for offset in (7, 13, 19):
            x1 = max(3, width - offset)
            y1 = height - 4
            x2 = width - 4
            y2 = max(3, height - offset)
            painter.drawLine(x1, y1, x2, y2)


class OverlayWindow(QWidget):
    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowTitle("Spica Overlay")
        self.setMinimumSize(MIN_WINDOW_SIZE)
        if DEBUG_NORMAL_WINDOW:
            self.setWindowFlags(Qt.WindowType.Window)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
            self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, False)
        else:
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Window
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAutoFillBackground(False)
        self.setStyleSheet("OverlayWindow { background: transparent; }")

        self.visual_tool: VisualDiffService | None = None
        self.tts_tool: GPTSoVITSTool | None = None
        self.tts_adapter: Any | None = None
        self.agent: SimpleAgent | None = None
        self.chat_worker: ChatWorker | None = None
        self.speech_worker: SpeechWorker | None = None
        self.voice_mode_active = False
        self.voice_session_id = 0
        self.startup_warmup_worker: StartupWarmupWorker | None = None
        self.conversation_id = str(uuid.uuid4())
        self.drag_offset: QPoint | None = None
        self.resize_origin_geometry: QRect | None = None
        self.resize_origin_pos: QPoint | None = None
        self.resize_origin_ui_scale = 1.0
        self.current_pixmap: QPixmap | None = None
        self.pixmap_cache: dict[str, QPixmap] = {}
        self.available_costumes: list[str] = []
        self.selected_costume: str | None = None
        self.interlocutor_name = DEFAULT_INTERLOCUTOR_NAME
        self.character_scale = 1.0
        self.ui_scale = 1.0
        self.typewriter_speed = 1.0
        self.cue_timers: list[QTimer] = []
        self.typing_timer: QTimer | None = None
        self.typing_text = ""
        self.typing_index = 0
        self.typing_finished_callback = None
        self.playback_items: list[dict[str, Any]] = []
        self.playback_index = 0
        self.playback_active = False
        self.current_audio_finished = False
        self.current_text_finished = False
        self.streaming_mode = False
        self.stream_pending_units: dict[int, dict[str, Any]] = {}
        self.next_stream_index = 0
        self.stream_done = False
        self.song_worker: SongWorker | None = None
        self.song_session_id = 0
        self.song_state = SongState.IDLE
        self.song_context = SongContext()
        self.song_router = SongIntentRouter()
        self.song_auto_play = True
        self.song_prelude_active = False
        self.song_clear_throat_active = False
        self.song_user_paused_preparing = False
        self.pending_song_audio_path: str | None = None
        self.song_preparing = False
        self.song_playback_active = False

        self.audio_output = None
        self.media_player = None
        self.song_audio_output = None
        self.song_media_player = None
        self.preloaded_audio_players: dict[int, tuple[Any, Any, Path]] = {}
        self.settings_panel: SettingsPanel | None = None

        self.character_label = QLabel(self)
        self.character_label.setObjectName("character")
        self.character_label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
        self.character_label.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.character_label.setStyleSheet("QLabel#character { background: transparent; }")
        self.character_label.setScaledContents(False)
        self.character_label.installEventFilter(self)

        try:
            shadow = QGraphicsDropShadowEffect(self.character_label)
            shadow.setBlurRadius(28)
            shadow.setOffset(0, 18)
            shadow.setColor(QColor(12, 18, 24, 86))
            self.character_label.setGraphicsEffect(shadow)
        except Exception:
            pass

        self.dialogue = TintedDialogueBox(self)
        self.dialogue.installEventFilter(self)

        self.input_panel = InputPanel(self)
        self.input_panel.send_requested.connect(self.send_message)
        self.input_panel.voice_requested.connect(self.toggle_voice)

        self.window_controls = WindowControls(self)
        self.window_controls.settings_requested.connect(self.open_settings_panel)
        self.window_controls.minimize_requested.connect(self.minimize_overlay)
        self.window_controls.close_requested.connect(self.close)
        self.window_controls.installEventFilter(self)

        self.resize_handle = CornerResizeHandle(self)

        self._apply_ui_scale()
        self._init_backend()
        self._load_default_character()
        self._size_to_screen()
        self._start_startup_warmup()

    def _init_backend(self) -> None:
        try:
            self.visual_tool = VisualDiffService()
            tts_config = load_tts_config()
            tts_provider = str(tts_config.get("provider") or tts_config.get("tts_provider") or "gptsovits_current")
            if tts_provider in CURRENT_GPTSOVITS_PROVIDERS:
                self.tts_tool = GPTSoVITSTool()
                self.tts_adapter = build_tts_adapter(tts_config, service=self.tts_tool)
            else:
                self.tts_tool = None
                self.tts_adapter = build_tts_adapter(tts_config)
            self.agent = SimpleAgent(tts_adapter=self.tts_adapter, visual_tool=self.visual_tool)
            self.interlocutor_name = self.agent.interlocutor_name
            provider_name = str(getattr(self.tts_adapter, "name", None) or tts_provider)
            self.dialogue.set_dialogue_text(f"LLM API 初始化完成，准备预热 {provider_name}...")
        except Exception as exc:
            if self.visual_tool is None:
                try:
                    self.visual_tool = VisualDiffService()
                except Exception:
                    self.visual_tool = None
            self.dialogue.set_dialogue_text(f"初始化后端失败：{exc}")

    def _start_startup_warmup(self) -> None:
        if self.agent is None or self.tts_adapter is None:
            return

        self.startup_warmup_worker = StartupWarmupWorker(self.agent, self.tts_adapter, self)
        self.startup_warmup_worker.status_changed.connect(self.dialogue.set_dialogue_text)
        self.startup_warmup_worker.finished_ok.connect(self.dialogue.set_dialogue_text)
        self.startup_warmup_worker.failed.connect(self.dialogue.set_dialogue_text)
        self.startup_warmup_worker.start()

    def _size_to_screen(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(760, 620)
            return

        available = screen.availableGeometry()
        width = min(max(720, int(available.width() * 0.48)), int(available.width() * 0.78))
        height = min(max(560, int(available.height() * 0.70)), int(available.height() * 0.82))
        x = available.x() + (available.width() - width) // 2
        y = available.y() + available.height() - height
        self.setGeometry(x, y, width, height)

    def _load_default_character(self) -> None:
        if self.visual_tool is None:
            return

        try:
            config = self.visual_tool.config
            costumes = self.visual_tool.list_costume_sets()
            costume, _mode = self.visual_tool.choose_costume(costumes, config=config)
            self.available_costumes = costumes
            self.selected_costume = costume
            self._set_default_character_for_costume(costume)

            dialog = config.get("dialog", {})
            self.dialogue.speaker_label.setText(str(dialog.get("speaker") or "spica").lower())
        except Exception as exc:
            self.dialogue.set_dialogue_text(f"载入差分失败：{exc}")

    def _set_default_character_for_costume(self, costume: str | None) -> None:
        if self.visual_tool is None or not costume:
            return

        config = self.visual_tool.config
        character = config.get("character", {})
        expression_id = str(character.get("default_expression_id") or "000").zfill(3)
        hand_pose = self.visual_tool.normalize_hand_pose(character.get("default_hand_pose") or "normal")
        image_path = self.visual_tool.resolve_expression_image(costume, hand_pose, expression_id)
        if image_path:
            self.set_character_image(image_path)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._layout_overlay()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        QTimer.singleShot(
            0,
            lambda: self.input_panel.input.setFocus(Qt.FocusReason.ActiveWindowFocusReason),
        )

    def _layout_overlay(self) -> None:
        width = self.width()
        height = self.height()
        scale = self.ui_scale

        controls_width = self.window_controls.sizeHint().width()
        controls_height = self.window_controls.sizeHint().height()
        top_margin = scaled_px(14, scale)
        self.window_controls.setGeometry(width - controls_width - top_margin, top_margin, controls_width, controls_height)

        horizontal_margin = max(scaled_px(18, scale), int(width * 0.055))
        input_height = scaled_px(58, scale)
        input_width = min(width - horizontal_margin * 2, scaled_px(760, scale))
        bottom_margin = max(scaled_px(16, scale), int(height * 0.022))
        input_x = (width - input_width) // 2
        input_y = height - bottom_margin - input_height
        self.input_panel.setGeometry(input_x, input_y, input_width, input_height)

        dialogue_width = min(width - horizontal_margin * 2, scaled_px(930, scale))
        dialogue_height = max(scaled_px(164, scale), min(scaled_px(250, scale), int(height * 0.24 * scale)))
        dialogue_x = (width - dialogue_width) // 2
        dialogue_y = input_y - scaled_px(14, scale) - dialogue_height
        self.dialogue.setGeometry(dialogue_x, dialogue_y, dialogue_width, dialogue_height)

        base_character_height = min(int(height * 0.86), dialogue_y + int(dialogue_height * 0.68))
        character_height = max(scaled_px(280, scale), min(int(base_character_height * self.character_scale * scale), int(height * 0.96)))
        character_width = self._character_width_for_height(character_height)
        character_width = min(character_width, int(width * 0.94))
        character_x = (width - character_width) // 2
        character_bottom = min(height - 8, input_y + int(input_height * 0.28))
        character_y = max(0, character_bottom - character_height)
        self.character_label.setGeometry(character_x, character_y, character_width, character_height)
        self._rescale_character()

        self.character_label.lower()
        self.dialogue.raise_()
        self.input_panel.raise_()
        if self.settings_panel and self.settings_panel.isVisible():
            panel_width = min(scaled_px(356, scale), max(scaled_px(318, scale), int(width * 0.34)))
            panel_room = max(scaled_px(230, scale), height - controls_height - scaled_px(46, scale) - top_margin)
            panel_height = min(scaled_px(326, scale), panel_room)
            self.settings_panel.setGeometry(width - panel_width - top_margin, controls_height + scaled_px(22, scale), panel_width, panel_height)
            self.settings_panel.raise_()
        handle_size = self.resize_handle.width()
        self.resize_handle.setGeometry(width - handle_size, height - handle_size, handle_size, handle_size)
        self.resize_handle.raise_()
        self.window_controls.raise_()
        self._update_click_through_mask()

    def _character_width_for_height(self, target_height: int) -> int:
        if self.current_pixmap is None or self.current_pixmap.isNull():
            return int(target_height * 0.55)
        ratio = self.current_pixmap.width() / max(1, self.current_pixmap.height())
        return max(220, int(target_height * ratio))

    def _rescale_character(self) -> None:
        if self.current_pixmap is None or self.current_pixmap.isNull():
            return
        scaled = self.current_pixmap.scaled(
            self.character_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.character_label.setPixmap(scaled)

    def set_character_image(self, path: str | Path | None) -> None:
        if not path:
            return
        cache_key = str(Path(path).resolve())
        cached_pixmap = self.pixmap_cache.get(cache_key)
        if cached_pixmap is not None and not cached_pixmap.isNull():
            self.current_pixmap = cached_pixmap
            self._layout_overlay()
            return

        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            return
        self.current_pixmap = self._trim_transparent_pixmap(pixmap)
        self.pixmap_cache[cache_key] = self.current_pixmap
        self._layout_overlay()

    def _trim_transparent_pixmap(self, pixmap: QPixmap) -> QPixmap:
        image = pixmap.toImage()
        if image.isNull() or not image.hasAlphaChannel():
            return pixmap

        left = image.width()
        top = image.height()
        right = -1
        bottom = -1
        for y in range(image.height()):
            for x in range(image.width()):
                if image.pixelColor(x, y).alpha() <= CHARACTER_HIT_ALPHA_THRESHOLD:
                    continue
                left = min(left, x)
                top = min(top, y)
                right = max(right, x)
                bottom = max(bottom, y)

        if right < left or bottom < top:
            return pixmap

        padding = 4
        left = max(0, left - padding)
        top = max(0, top - padding)
        right = min(image.width() - 1, right + padding)
        bottom = min(image.height() - 1, bottom + padding)
        crop_rect = QRect(left, top, right - left + 1, bottom - top + 1)
        return pixmap.copy(crop_rect)

    def _apply_ui_scale(self) -> None:
        self.dialogue.apply_scale(self.ui_scale)
        self.input_panel.apply_scale(self.ui_scale)
        self.window_controls.apply_scale(self.ui_scale)
        self.resize_handle.apply_scale(self.ui_scale)
        if self.settings_panel is not None:
            self.settings_panel.apply_scale(self.ui_scale)
        self._layout_overlay()

    def send_message(self) -> None:
        message = self.input_panel.input.text().strip()
        if not message:
            self.input_panel.input.setFocus()
            return

        intent = self.song_router.route(message, self.song_state, self.song_context)
        if intent.action not in {SongAction.NONE, SongAction.REJECT}:
            self.input_panel.input.clear()
            self._handle_song_intent(intent)
            return

        interrupt_song = self._is_song_busy()
        if interrupt_song:
            self._cancel_current_song(show_message=False)
        self._start_chat_message(message, interrupt_active=interrupt_song)

    def _start_chat_message(self, message: str, *, interrupt_active: bool = False) -> None:
        if self.agent is None:
            self.dialogue.set_dialogue_text("后端未初始化，请检查 OPENAI_API_KEY 和本地依赖。")
            return
        if self.chat_worker and self.chat_worker.isRunning():
            if not interrupt_active:
                return
            self.chat_worker.requestInterruption()

        self._clear_cue_timers()
        self._stop_audio()
        self._reset_stream_state()
        self.input_panel.input.clear()
        self.set_busy(True)
        self._start_typewriter("……", interval_ms=180)

        visual_overrides = self._visual_overrides()
        self.chat_worker = ChatWorker(self.agent, message, self.conversation_id, visual_overrides, self)
        self.chat_worker.stream_event.connect(self._handle_stream_event)
        self.chat_worker.failed.connect(self._handle_chat_error)
        self.chat_worker.start()

    def _handle_song_intent(self, intent: SongIntent) -> None:
        if intent.action == SongAction.SING:
            request = build_song_request_from_intent(intent)
            if request is None:
                self._start_typewriter("想听哪一首？可以说歌名，或者说‘周杰伦的稻香’。", interval_ms=45)
                return
            if self.song_state in {SongState.PREPARING, SongState.READY, SongState.PLAYING, SongState.PAUSED}:
                self._cancel_current_song(show_message=False)
            self._start_song_request_with_prelude(request)
            return

        if intent.action == SongAction.SEARCH:
            self.song_state = SongState.INTENT_CONFIRMING
            self.song_context.state = self.song_state
            self._start_typewriter("想听哪一首？可以说歌名，或者说‘周杰伦的稻香’。", interval_ms=45)
            self.input_panel.input.setFocus(Qt.FocusReason.OtherFocusReason)
            return

        if intent.action == SongAction.PAUSE:
            self._pause_song()
            return

        if intent.action == SongAction.RESUME:
            self._resume_song()
            return

        if intent.action == SongAction.CANCEL:
            self._cancel_current_song(show_message=True)
            return

        if intent.action == SongAction.CHANGE:
            if intent.query or intent.title:
                request = build_song_request_from_intent(
                    SongIntent(
                        action=SongAction.SING,
                        confidence=intent.confidence,
                        query=intent.query,
                        title=intent.title,
                        artist=intent.artist,
                        original_text=intent.original_text,
                        source=intent.source,
                        reason=intent.reason,
                    )
                )
                if request is not None:
                    self._cancel_current_song(show_message=False)
                    self._start_song_request_with_prelude(request)
                    return
            self._start_typewriter("想换成哪首？", interval_ms=45)
            self.input_panel.input.setFocus(Qt.FocusReason.OtherFocusReason)
            return

        if intent.action == SongAction.RESTART:
            request = self.song_context.pending_request or self.song_context.last_request
            if request is not None:
                self._cancel_current_song(show_message=False)
                self._start_song_request_with_prelude(request)
                return
            self._start_typewriter("还没有可以重唱的歌曲。", interval_ms=45)
            self.input_panel.input.setFocus(Qt.FocusReason.OtherFocusReason)
            return

        if intent.action == SongAction.REJECT:
            self._start_chat_message(intent.original_text)

    def _start_song_request_with_prelude(self, request: SongRequest) -> None:
        if self.agent is None:
            self._start_song_request(request)
            return

        self._stop_conversation_for_song()
        self.song_prelude_active = True
        self.song_user_paused_preparing = False
        self._start_song_request(request, auto_play=False, show_message=False, stop_conversation=False)
        self._start_song_prelude_chat(request)

    def _start_song_prelude_chat(self, request: SongRequest) -> None:
        if self.agent is None:
            self._finish_song_prelude()
            return
        if self.chat_worker and self.chat_worker.isRunning():
            self.chat_worker.requestInterruption()

        self._clear_cue_timers()
        self._stop_audio()
        self._reset_stream_state()
        self.set_busy(True)
        self._start_typewriter("……", interval_ms=180)

        prompt = self._song_prelude_prompt(request)
        visual_overrides = self._visual_overrides()
        self.chat_worker = ChatWorker(self.agent, prompt, self.conversation_id, visual_overrides, self)
        self.chat_worker.stream_event.connect(self._handle_stream_event)
        self.chat_worker.failed.connect(self._handle_chat_error)
        self.chat_worker.start()

    def _song_prelude_prompt(self, request: SongRequest) -> str:
        song_name = request.search_keyword()
        return (
            f"用户想听你唱《{song_name}》。"
            "请以 Spica 的口吻，用一句很短、自然、可直接朗读的话回应，表示你要准备唱这首歌了。"
            "不要解释流程，不要提到工具、模型、下载、生成、缓存或技术细节。"
        )

    def _finish_song_prelude(self) -> None:
        if not self.song_prelude_active:
            return
        self.song_prelude_active = False

        if self.song_state not in {SongState.PREPARING, SongState.READY}:
            return
        if self.song_user_paused_preparing:
            if self.song_state == SongState.READY:
                self._start_typewriter("准备好了。说继续我再唱。", interval_ms=45)
            else:
                self._start_typewriter("好，准备好后先不播放。说继续我再唱。", interval_ms=45)
            self.input_panel.input.setFocus(Qt.FocusReason.OtherFocusReason)
            return

        self.song_auto_play = True
        self.song_context.auto_play = True
        self.song_clear_throat_active = True
        self.song_auto_play = False
        self.song_context.auto_play = False
        self._start_typewriter(
            "Spica 正在清嗓",
            interval_ms=70,
            on_finished=lambda: QTimer.singleShot(250, self._finish_song_clear_throat),
        )

    def _finish_song_clear_throat(self) -> None:
        self.song_clear_throat_active = False
        if self.song_user_paused_preparing:
            return
        self.song_auto_play = True
        self.song_context.auto_play = True
        if self.song_state == SongState.READY:
            self._play_ready_song_after_prelude()

    def _play_ready_song_after_prelude(self) -> None:
        if self.song_state != SongState.READY:
            return
        audio_path = self.pending_song_audio_path or self.song_context.pending_audio_path
        if not audio_path:
            return
        self.song_state = SongState.PLAYING
        self.song_context.state = self.song_state
        self.song_playback_active = True
        self.set_busy(False)
        self._start_typewriter("唱歌中", interval_ms=70)
        self._play_song_audio(audio_path, self.song_session_id)

    def _stop_song_prelude(self) -> None:
        self.song_prelude_active = False
        self.song_clear_throat_active = False
        self.song_user_paused_preparing = False
        if self.chat_worker and self.chat_worker.isRunning():
            self.chat_worker.requestInterruption()
        self.streaming_mode = False
        self.stream_pending_units = {}
        self.stream_done = False
        self.playback_active = False
        self.playback_items = []
        self.playback_index = 0
        self.current_audio_finished = False
        self.current_text_finished = False
        self._stop_audio()
        self._release_preloaded_audio_players()

    def _pause_song(self) -> None:
        if self.song_state == SongState.PLAYING:
            if self.song_media_player is None:
                self._start_typewriter("现在没有正在播放的歌曲。", interval_ms=45)
                return
            self.song_media_player.pause()
            self.song_state = SongState.PAUSED
            self.song_context.state = self.song_state
            self.song_playback_active = False
            self.set_busy(False)
            self._start_typewriter("先暂停。", interval_ms=45)
            self.input_panel.input.setFocus(Qt.FocusReason.OtherFocusReason)
            return

        if self.song_state == SongState.PREPARING:
            self.song_auto_play = False
            self.song_context.auto_play = False
            self.song_user_paused_preparing = True
            self._start_typewriter("好，准备好后先不播放。说继续我再唱。", interval_ms=45)
            self.input_panel.input.setFocus(Qt.FocusReason.OtherFocusReason)
            return

        self._start_typewriter("现在没有正在播放的歌曲。", interval_ms=45)
        self.input_panel.input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _resume_song(self) -> None:
        if self.song_state == SongState.PAUSED:
            if self.song_media_player is None:
                self._start_typewriter("现在没有可以继续的歌曲。", interval_ms=45)
                return
            self.song_media_player.play()
            self.song_state = SongState.PLAYING
            self.song_context.state = self.song_state
            self.song_playback_active = True
            self.set_busy(False)
            self._start_typewriter("继续唱。", interval_ms=45)
            return

        if self.song_state == SongState.READY:
            if self.song_prelude_active:
                self._stop_song_prelude()
            audio_path = self.pending_song_audio_path or self.song_context.pending_audio_path
            if not audio_path:
                self._start_typewriter("现在没有可以继续的歌曲。", interval_ms=45)
                return
            self.song_auto_play = True
            self.song_context.auto_play = True
            self.song_user_paused_preparing = False
            self.song_clear_throat_active = False
            self.song_state = SongState.PLAYING
            self.song_context.state = self.song_state
            self.song_playback_active = True
            self.set_busy(False)
            self._start_typewriter("继续唱。", interval_ms=45)
            self._play_song_audio(audio_path, self.song_session_id)
            return

        self._start_typewriter("现在没有可以继续的歌曲。", interval_ms=45)
        self.input_panel.input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _start_song_request(
        self,
        request: SongRequest,
        *,
        auto_play: bool = True,
        show_message: bool = True,
        stop_conversation: bool = True,
    ) -> None:
        if stop_conversation:
            self._stop_conversation_for_song()
        self.song_session_id += 1
        job_id = self.song_session_id
        self.song_state = SongState.PREPARING
        self.song_context.state = self.song_state
        self.song_auto_play = auto_play
        self.song_context.auto_play = auto_play
        self.song_clear_throat_active = False
        self.song_user_paused_preparing = False
        self.song_context.pending_request = request
        self.song_context.pending_audio_path = None
        self.pending_song_audio_path = None
        self.song_preparing = True
        self.song_playback_active = False
        self.set_busy(False)
        if show_message:
            self._start_typewriter("Spica 正在清嗓", interval_ms=70)
        self.input_panel.input.setFocus(Qt.FocusReason.OtherFocusReason)

        self.song_worker = SongWorker(request, job_id, self)
        self.song_worker.completed.connect(self._handle_song_ready)
        self.song_worker.failed.connect(self._handle_song_error)
        self.song_worker.finished.connect(lambda jid=job_id: self._handle_song_worker_finished(jid))
        self.song_worker.start()

    def _stop_conversation_for_song(self) -> None:
        if self.chat_worker and self.chat_worker.isRunning():
            self.chat_worker.requestInterruption()
        if self.speech_worker and self.speech_worker.isRunning():
            self.voice_session_id += 1
            self.speech_worker.requestInterruption()
        self.streaming_mode = False
        self.stream_pending_units = {}
        self.stream_done = False
        self._clear_cue_timers()
        self._stop_audio()
        self._release_preloaded_audio_players()
        self.playback_items = []
        self.playback_index = 0
        self.playback_active = False
        self.current_audio_finished = False
        self.current_text_finished = False

    def _is_song_busy(self) -> bool:
        return bool(
            self.song_preparing
            or self.song_playback_active
            or self.song_state in {SongState.PREPARING, SongState.READY, SongState.PLAYING, SongState.PAUSED, SongState.CANCELLING}
        )

    def _cancel_current_song(self, show_message: bool = True) -> None:
        had_song = self._is_song_busy()
        self.song_session_id += 1
        if self.song_prelude_active:
            self._stop_song_prelude()
        if self.song_worker and self.song_worker.isRunning():
            self.song_worker.cancel()
        self._release_song_audio_player()
        self.song_state = SongState.IDLE
        self.song_context.state = self.song_state
        self.song_auto_play = True
        self.song_context.auto_play = True
        self.song_clear_throat_active = False
        self.song_user_paused_preparing = False
        self.song_context.pending_request = None
        self.song_context.pending_audio_path = None
        self.pending_song_audio_path = None
        self.song_preparing = False
        self.song_playback_active = False
        self.set_busy(False)
        if show_message and had_song:
            self._start_typewriter("好，先不唱了。", interval_ms=45)
            if self.voice_mode_active:
                self._schedule_next_voice_recording(500)

    def _handle_song_ready(self, job_id: int, payload: dict[str, Any]) -> None:
        if job_id != self.song_session_id:
            return
        if not bool(payload.get("ok")):
            self._handle_song_error(job_id, str(payload.get("error") or "唱歌任务失败。"))
            return
        audio_path = payload.get("final_audio_path")
        if not audio_path:
            self._handle_song_error(job_id, "唱歌任务没有返回音频文件。")
            return
        self.song_preparing = False
        self.pending_song_audio_path = str(audio_path)
        self.song_context.pending_audio_path = str(audio_path)
        self.set_busy(False)
        if not self.song_auto_play:
            self.song_state = SongState.READY
            self.song_context.state = self.song_state
            self.song_playback_active = False
            if (self.song_prelude_active or self.song_clear_throat_active) and not self.song_user_paused_preparing:
                return
            self._start_typewriter("准备好了。说继续我再唱。", interval_ms=45)
            self.input_panel.input.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        self.song_state = SongState.PLAYING
        self.song_context.state = self.song_state
        self.song_playback_active = True
        self._start_typewriter("唱歌中", interval_ms=70)
        self._play_song_audio(audio_path, job_id)

    def _handle_song_error(self, job_id: int, message: str) -> None:
        if job_id != self.song_session_id:
            return
        self.song_state = SongState.ERROR
        self.song_context.state = self.song_state
        self.song_auto_play = True
        self.song_context.auto_play = True
        self.song_prelude_active = False
        self.song_clear_throat_active = False
        self.song_user_paused_preparing = False
        self.song_context.pending_request = None
        self.song_context.pending_audio_path = None
        self.pending_song_audio_path = None
        self.song_preparing = False
        self.song_playback_active = False
        self._release_song_audio_player()
        self.set_busy(False)
        self._start_typewriter(f"唱歌失败：{message}", interval_ms=45)
        if self.voice_mode_active:
            self._schedule_next_voice_recording(900)

    def _handle_song_worker_finished(self, job_id: int) -> None:
        del job_id
        worker = self.sender()
        if worker is self.song_worker:
            self.song_worker = None
        if worker is not None:
            worker.deleteLater()

    def _play_song_audio(self, audio_path: Any, job_id: int) -> None:
        self._release_song_audio_player()
        if QMediaPlayer is None or QAudioOutput is None:
            self._handle_song_error(job_id, "当前 Qt 环境没有可用的音频播放组件。")
            return
        path = Path(str(audio_path))
        if not path.exists():
            self._handle_song_error(job_id, f"音频文件不存在：{path}")
            return
        self.song_audio_output = QAudioOutput(self)
        self.song_audio_output.setVolume(0.92)
        self.song_media_player = QMediaPlayer(self)
        self.song_media_player.setAudioOutput(self.song_audio_output)
        self.song_media_player.mediaStatusChanged.connect(
            lambda status, jid=job_id: self._handle_song_media_status(status, jid)
        )
        self.song_media_player.setSource(QUrl.fromLocalFile(str(path)))
        self.song_media_player.play()

    def _handle_song_media_status(self, status, job_id: int) -> None:
        if job_id != self.song_session_id or not self.song_playback_active:
            return
        if status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._handle_song_error(job_id, "歌曲音频无法播放。")
            return
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._finish_song_playback()

    def _finish_song_playback(self) -> None:
        self._release_song_audio_player()
        self.song_context.last_request = self.song_context.pending_request
        self.song_context.last_audio_path = self.song_context.pending_audio_path
        self.song_context.pending_request = None
        self.song_context.pending_audio_path = None
        self.pending_song_audio_path = None
        self.song_state = SongState.IDLE
        self.song_context.state = self.song_state
        self.song_auto_play = True
        self.song_context.auto_play = True
        self.song_prelude_active = False
        self.song_clear_throat_active = False
        self.song_user_paused_preparing = False
        self.song_preparing = False
        self.song_playback_active = False
        self.set_busy(False)
        self._start_typewriter("唱完了。", interval_ms=45)
        if self.voice_mode_active:
            self._schedule_next_voice_recording(500)
        else:
            self.input_panel.input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _release_song_audio_player(self) -> None:
        media_player = self.song_media_player
        audio_output = self.song_audio_output
        self.song_media_player = None
        self.song_audio_output = None
        if media_player is not None:
            try:
                media_player.stop()
            except Exception:
                pass
            try:
                media_player.deleteLater()
            except Exception:
                pass
        if audio_output is not None:
            try:
                audio_output.deleteLater()
            except Exception:
                pass

    def _reset_stream_state(self) -> None:
        self._release_preloaded_audio_players()
        self.streaming_mode = True
        self.stream_pending_units = {}
        self.next_stream_index = 0
        self.stream_done = False
        self.playback_items = []
        self.playback_index = 0
        self.playback_active = False
        self.current_audio_finished = False
        self.current_text_finished = False

    def _handle_stream_event(self, event_name: str, data: dict[str, Any]) -> None:
        if self._is_song_busy() and not self.song_prelude_active:
            return
        if event_name == "status":
            self._handle_stream_status(data)
            return
        if event_name == "unit_ready":
            self._handle_stream_unit_ready(data)
            return
        if event_name == "done":
            self._handle_stream_done(data)
            return
        if event_name == "error":
            self._handle_chat_error(str(data.get("message") or "请求失败。"))
            return

    def _handle_stream_status(self, data: dict[str, Any]) -> None:
        state = str(data.get("state") or "")
        if state == "tools" and not self.playback_active:
            self._start_typewriter("正在处理工具...", interval_ms=55)

    def _handle_stream_unit_ready(self, data: dict[str, Any]) -> None:
        item = self._playback_item_from_stream_unit(data)
        self.stream_pending_units[int(item["index"])] = item
        if self.playback_active:
            self._preload_audio_for_item(item)
        self._pump_stream_playback()

    def _handle_stream_done(self, data: dict[str, Any]) -> None:
        self.stream_done = True
        answer = str(data.get("answer") or "").strip()
        units_count = int(data.get("units_count") or 0)
        if units_count == 0 and answer and self.next_stream_index == 0:
            self.stream_pending_units[0] = {"index": 0, "text": answer, "audio_path": None, "cue": {}}
        self._pump_stream_playback()

    def _playback_item_from_stream_unit(self, data: dict[str, Any]) -> dict[str, Any]:
        visual = data.get("visual") if isinstance(data.get("visual"), dict) else {}
        self._apply_visual(visual)
        cues = visual.get("cues") if isinstance(visual.get("cues"), list) else []
        cue = cues[0] if cues and isinstance(cues[0], dict) else {}
        if not cue:
            maybe_cue = visual.get("cue") if isinstance(visual.get("cue"), dict) else {}
            cue = maybe_cue

        try:
            index = int(data.get("index") or 0)
        except (TypeError, ValueError):
            index = self.next_stream_index
        text = str(data.get("display_text") or data.get("tts_text") or "……")
        return {
            "index": index,
            "text": text,
            "audio_path": data.get("audio_path"),
            "cue": cue,
        }

    def _pump_stream_playback(self) -> None:
        if not self.streaming_mode or self.playback_active:
            return

        item = self.stream_pending_units.pop(self.next_stream_index, None)
        if item is not None:
            self.next_stream_index += 1
            self.playback_items = [item]
            self.playback_index = 0
            self.playback_active = True
            self._play_next_tts_item()
            return

        if self.stream_done:
            self._end_stream_playback()

    def _end_stream_playback(self) -> None:
        self.streaming_mode = False
        self.stream_pending_units = {}
        self.playback_items = []
        self.playback_index = 0
        self.playback_active = False
        self.current_audio_finished = False
        self.current_text_finished = False
        self._release_preloaded_audio_players()
        self.set_busy(False)
        if self.song_prelude_active:
            self._finish_song_prelude()
            return
        if self.voice_mode_active:
            self._schedule_next_voice_recording(320)
        else:
            self.input_panel.input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _handle_chat_result(self, result: dict[str, Any]) -> None:
        if result.get("error"):
            message = result["error"].get("message") if isinstance(result["error"], dict) else str(result["error"])
            self._handle_chat_error(message or "请求失败。")
            return

        answer = str(result.get("answer") or "没有返回回答。")
        visual = result.get("visual") if isinstance(result.get("visual"), dict) else {}
        self._apply_visual(visual)
        self._play_tts_sequence(result, visual, answer)

    def _handle_chat_error(self, message: str) -> None:
        if self.song_prelude_active:
            logger.warning("Song prelude chat failed: %s", message)
            self.streaming_mode = False
            self.stream_pending_units = {}
            self.playback_active = False
            self._stop_audio()
            self._release_preloaded_audio_players()
            self._finish_song_prelude()
            return
        if self._is_song_busy():
            return
        self.streaming_mode = False
        self.stream_pending_units = {}
        self.playback_active = False
        self._stop_audio()
        self._release_preloaded_audio_players()
        self._stop_typewriter()
        self.dialogue.set_dialogue_text(f"请求失败：{message}")
        self.set_busy(False)
        self._schedule_next_voice_recording(900)

    def _visual_overrides(self) -> dict[str, str]:
        if self.selected_costume:
            return {"costume_mode": "fixed", "costume_set": self.selected_costume}
        return {"costume_mode": "random"}

    def _apply_visual(self, visual: dict[str, Any]) -> None:
        dialog = visual.get("dialog") if isinstance(visual.get("dialog"), dict) else {}
        speaker = str(dialog.get("speaker") or "spica").lower()
        self.dialogue.speaker_label.setText(speaker)

    def _play_tts_sequence(self, result: dict[str, Any], visual: dict[str, Any], fallback_text: str) -> None:
        self._clear_cue_timers()
        self._stop_audio()
        self._release_preloaded_audio_players()
        self.playback_items = self._build_tts_playback_items(result, visual, fallback_text)
        self.playback_index = 0
        self.playback_active = bool(self.playback_items)

        if not self.playback_items:
            self._start_typewriter(fallback_text, on_finished=self._finish_playback)
            return

        self._play_next_tts_item()

    def _build_tts_playback_items(
        self,
        result: dict[str, Any],
        visual: dict[str, Any],
        fallback_text: str,
    ) -> list[dict[str, Any]]:
        chunk_audio = result.get("tts_chunk_audio") if isinstance(result.get("tts_chunk_audio"), list) else []
        tts_chunks = result.get("tts_chunks") if isinstance(result.get("tts_chunks"), list) else []
        cues = visual.get("cues") if isinstance(visual.get("cues"), list) else []
        items = []

        if chunk_audio:
            for index, audio_item in enumerate(chunk_audio):
                if not isinstance(audio_item, dict):
                    continue
                text = str(audio_item.get("text") or (tts_chunks[index] if index < len(tts_chunks) else "") or "")
                cue = self._cue_for_tts_chunk(cues, index)
                items.append(
                    {
                        "index": index,
                        "text": text or fallback_text,
                        "audio_path": audio_item.get("audio_path"),
                        "cue": cue,
                    }
                )
            return items

        if result.get("audio_path"):
            cue = self._cue_for_tts_chunk(cues, 0)
            return [
                {
                    "index": 0,
                    "text": fallback_text,
                    "audio_path": result.get("audio_path"),
                    "cue": cue,
                }
            ]

        return [{"index": 0, "text": fallback_text, "audio_path": None, "cue": self._cue_for_tts_chunk(cues, 0)}]

    def _cue_for_tts_chunk(self, cues: list[Any], index: int) -> dict[str, Any]:
        if not cues:
            return {}
        cue = cues[min(index, len(cues) - 1)]
        return cue if isinstance(cue, dict) else {}

    def _play_next_tts_item(self) -> None:
        if not self.playback_active:
            self._finish_playback()
            return
        if self.playback_index >= len(self.playback_items):
            self._finish_playback()
            return

        item = self.playback_items[self.playback_index]
        self.current_audio_finished = False
        self.current_text_finished = False
        cue = item.get("cue") if isinstance(item.get("cue"), dict) else {}
        image_path = cue.get("image_path")
        if image_path:
            self.set_character_image(BASE_DIR / str(image_path))

        self._start_typewriter(str(item.get("text") or "……"), on_finished=self._mark_text_finished)
        self._play_chunk_audio(item.get("audio_path"))
        self._preload_next_playback_item()

    def _current_playback_item_index(self) -> Any:
        if 0 <= self.playback_index < len(self.playback_items):
            item = self.playback_items[self.playback_index]
            if isinstance(item, dict):
                return item.get("index", self.playback_index)
        return self.playback_index

    def _audio_item_key(self, item_index: Any) -> int | None:
        try:
            return int(item_index)
        except (TypeError, ValueError):
            return None

    def _release_audio_player(self) -> None:
        media_player = self.media_player
        audio_output = self.audio_output
        self.media_player = None
        self.audio_output = None

        if media_player is not None:
            try:
                media_player.mediaStatusChanged.disconnect(self._handle_media_status)
            except Exception:
                pass
            try:
                media_player.stop()
            except Exception:
                pass
            try:
                media_player.deleteLater()
            except Exception:
                pass

        if audio_output is not None:
            try:
                audio_output.deleteLater()
            except Exception:
                pass

        if media_player is not None or audio_output is not None:
            logger.debug("Released audio player item=%s", self._current_playback_item_index())

    def _release_preloaded_audio_players(self, item_key: int | None = None) -> None:
        if item_key is None:
            items = list(self.preloaded_audio_players.items())
            self.preloaded_audio_players.clear()
        else:
            preloaded = self.preloaded_audio_players.pop(item_key, None)
            items = [(item_key, preloaded)] if preloaded is not None else []

        for key, preloaded in items:
            media_player, audio_output, _path = preloaded
            if media_player is not None:
                try:
                    media_player.mediaStatusChanged.disconnect(self._handle_media_status)
                except Exception:
                    pass
                try:
                    media_player.stop()
                except Exception:
                    pass
                try:
                    media_player.deleteLater()
                except Exception:
                    pass
            if audio_output is not None:
                try:
                    audio_output.deleteLater()
                except Exception:
                    pass
            logger.debug("Released preloaded audio player item=%s", key)

    def _preload_audio_for_item(self, item: dict[str, Any]) -> None:
        if QMediaPlayer is None or QAudioOutput is None:
            return

        item_key = self._audio_item_key(item.get("index"))
        if item_key is None or item_key in self.preloaded_audio_players:
            return

        audio_path = item.get("audio_path")
        if not audio_path:
            return

        path = Path(str(audio_path))
        if not path.exists():
            return

        audio_output = None
        media_player = None
        try:
            audio_output = QAudioOutput(self)
            audio_output.setVolume(0.86)
            media_player = QMediaPlayer(self)
            media_player.setAudioOutput(audio_output)
            media_player.setSource(QUrl.fromLocalFile(str(path)))
        except Exception:
            if media_player is not None:
                try:
                    media_player.deleteLater()
                except Exception:
                    pass
            if audio_output is not None:
                try:
                    audio_output.deleteLater()
                except Exception:
                    pass
            logger.debug("Audio preload failed item=%s path=%s", item_key, path, exc_info=True)
            return

        self.preloaded_audio_players[item_key] = (media_player, audio_output, path)
        logger.debug("Audio preloaded item=%s path=%s", item_key, path)

    def _preload_next_playback_item(self) -> None:
        next_index = self.playback_index + 1
        if 0 <= next_index < len(self.playback_items):
            item = self.playback_items[next_index]
            if isinstance(item, dict):
                self._preload_audio_for_item(item)

    def _play_chunk_audio(self, audio_path: Any) -> None:
        self._release_audio_player()
        item_index = self._current_playback_item_index()
        item_key = self._audio_item_key(item_index)
        if not audio_path or QMediaPlayer is None or QAudioOutput is None:
            logger.debug("Audio fallback item=%s reason=missing_path_or_qt", item_index)
            self.current_audio_finished = True
            self._maybe_advance_playback()
            return

        path = Path(str(audio_path))
        if not path.exists():
            logger.debug("Audio fallback item=%s reason=missing_file path=%s", item_index, path)
            self.current_audio_finished = True
            self._maybe_advance_playback()
            return

        if item_key is not None:
            preloaded = self.preloaded_audio_players.pop(item_key, None)
            if preloaded is not None:
                media_player, audio_output, preloaded_path = preloaded
                if preloaded_path == path:
                    self.media_player = media_player
                    self.audio_output = audio_output
                    try:
                        self.media_player.mediaStatusChanged.connect(self._handle_media_status)
                    except Exception:
                        self._release_audio_player()
                        logger.debug("Audio preloaded connect failed item=%s path=%s", item_index, path, exc_info=True)
                    else:
                        logger.debug("Audio play start item=%s path=%s preloaded=true", item_index, path)
                        self.media_player.play()
                        return
                else:
                    self.preloaded_audio_players[item_key] = preloaded
                    self._release_preloaded_audio_players(item_key)

        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.86)
        self.media_player = QMediaPlayer(self)
        self.media_player.setAudioOutput(self.audio_output)
        self.media_player.mediaStatusChanged.connect(self._handle_media_status)
        self.media_player.setSource(QUrl.fromLocalFile(str(path)))
        logger.debug("Audio play start item=%s path=%s", item_index, path)
        self.media_player.play()

    def _handle_media_status(self, status) -> None:
        sender = self.sender()
        if sender is not None and sender is not self.media_player:
            logger.debug("Ignored stale media status item=%s status=%s", self._current_playback_item_index(), status)
            return

        if status in (QMediaPlayer.MediaStatus.EndOfMedia, QMediaPlayer.MediaStatus.InvalidMedia):
            item_index = self._current_playback_item_index()
            logger.debug("Audio media finished item=%s status=%s", item_index, status)
            self._release_audio_player()
            self.current_audio_finished = True
            self._maybe_advance_playback()

    def _mark_text_finished(self) -> None:
        self.current_text_finished = True
        self._maybe_advance_playback()

    def _maybe_advance_playback(self) -> None:
        if not self.playback_active:
            return
        if not self.current_audio_finished or not self.current_text_finished:
            return
        item_index = self._current_playback_item_index()
        self.playback_index += 1
        self.current_audio_finished = False
        self.current_text_finished = False
        logger.debug("Advance playback item=%s next_index=%s", item_index, self.playback_index)
        QTimer.singleShot(0, self._play_next_tts_item)

    def _finish_playback(self) -> None:
        self.playback_active = False
        self.playback_items = []
        self.playback_index = 0
        self.current_audio_finished = False
        self.current_text_finished = False
        if self.streaming_mode:
            QTimer.singleShot(0, self._pump_stream_playback)
            return
        self._release_preloaded_audio_players()
        self.set_busy(False)
        if self.song_prelude_active:
            self._finish_song_prelude()
            return
        if self.voice_mode_active:
            self._schedule_next_voice_recording(320)
        else:
            self.input_panel.input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _show_cue(self, cue: dict[str, Any]) -> None:
        self._start_typewriter(str(cue.get("text") or "……"))
        image_path = cue.get("image_path")
        if image_path:
            self.set_character_image(BASE_DIR / str(image_path))

    def _clear_cue_timers(self) -> None:
        for timer in self.cue_timers:
            timer.stop()
            timer.deleteLater()
        self.cue_timers.clear()
        self._stop_typewriter()
        self.playback_active = False

    def _cue_duration_ms(self, text: str) -> int:
        char_count = max(1, len(text or ""))
        return max(1500, min(5200, char_count * 58 + 900))

    def _start_typewriter(self, text: str, interval_ms: int | None = None, on_finished=None) -> None:
        self._stop_typewriter()
        self.typing_text = text or "……"
        self.typing_index = 0
        self.typing_finished_callback = on_finished
        self.dialogue.set_dialogue_text("")
        self.typing_timer = QTimer(self)
        self.typing_timer.timeout.connect(self._type_next_character)
        self.typing_timer.start(interval_ms or self._typewriter_delay(""))
        self._type_next_character()

    def _type_next_character(self) -> None:
        if self.typing_index >= len(self.typing_text):
            self._complete_typewriter()
            return

        char = self.typing_text[self.typing_index]
        self.typing_index += 1
        self.dialogue.set_dialogue_text(self.typing_text[:self.typing_index])
        if self.typing_timer is not None:
            self.typing_timer.setInterval(self._typewriter_delay(char))

    def _typewriter_delay(self, char: str) -> int:
        if char in "。！？!?":
            delay = scaled_px(220, self.ui_scale)
        elif char in "、，,；;：:":
            delay = scaled_px(92, self.ui_scale)
        else:
            delay = max(22, min(46, scaled_px(34, self.ui_scale)))
        return max(8, round(delay / self.typewriter_speed))

    def _complete_typewriter(self) -> None:
        if self.typing_timer is not None:
            self.typing_timer.stop()
            self.typing_timer.deleteLater()
            self.typing_timer = None
        callback = self.typing_finished_callback
        self.typing_finished_callback = None
        if callback:
            callback()

    def _stop_typewriter(self) -> None:
        if self.typing_timer is None:
            self.typing_finished_callback = None
            return
        self.typing_timer.stop()
        self.typing_timer.deleteLater()
        self.typing_timer = None
        self.typing_finished_callback = None

    def _play_audio(self, audio_path: Any) -> None:
        if not audio_path or QMediaPlayer is None or QAudioOutput is None:
            return
        path = Path(str(audio_path))
        if not path.exists():
            return

        self._release_audio_player()
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.86)
        self.media_player = QMediaPlayer(self)
        self.media_player.setAudioOutput(self.audio_output)
        self.media_player.setSource(QUrl.fromLocalFile(str(path)))
        self.media_player.play()

    def _stop_audio(self) -> None:
        self._release_audio_player()

    def toggle_voice(self, checked: bool = False) -> None:
        del checked
        if self.voice_mode_active:
            self._stop_voice_mode()
            return
        self._start_voice_mode()

    def _start_voice_mode(self) -> None:
        if self.agent is None:
            self.input_panel.set_voice_active(False)
            self.dialogue.set_dialogue_text("后端未初始化，请检查 OPENAI_API_KEY 和本地依赖。")
            return

        self.voice_mode_active = True
        self.voice_session_id += 1
        self.input_panel.set_voice_active(True)
        self.set_busy(self._is_conversation_busy())
        self._maybe_start_voice_recording(self.voice_session_id)

    def _stop_voice_mode(self) -> None:
        self.voice_mode_active = False
        self.voice_session_id += 1
        self.input_panel.set_voice_active(False)
        if self.speech_worker and self.speech_worker.isRunning():
            self.speech_worker.requestInterruption()
        self.dialogue.set_dialogue_text("语音模式已关闭。")
        self.set_busy(self._is_conversation_busy())

    def _start_speech_worker(self) -> None:
        self.set_busy(True)
        session_id = self.voice_session_id
        self.speech_worker = SpeechWorker(self)
        self.speech_worker.status_changed.connect(
            lambda message, sid=session_id: self._handle_speech_status(message, sid)
        )
        self.speech_worker.recognized.connect(
            lambda text, sid=session_id: self._handle_recognized_speech(text, sid)
        )
        self.speech_worker.failed.connect(
            lambda message, sid=session_id: self._handle_speech_error(message, sid)
        )
        self.speech_worker.finished.connect(lambda sid=session_id: self._handle_speech_finished(sid))
        self.speech_worker.start()

    def _maybe_start_voice_recording(self, session_id: int | None = None) -> None:
        if session_id is not None and session_id != self.voice_session_id:
            return
        if not self.voice_mode_active:
            return
        if self.speech_worker and self.speech_worker.isRunning():
            return
        if self._is_conversation_busy():
            return
        self._start_speech_worker()

    def _schedule_next_voice_recording(self, delay_ms: int = 320) -> None:
        if not self.voice_mode_active:
            return
        session_id = self.voice_session_id
        QTimer.singleShot(delay_ms, lambda sid=session_id: self._maybe_start_voice_recording(sid))

    def _is_conversation_busy(self) -> bool:
        return bool(
            (self.chat_worker and self.chat_worker.isRunning())
            or self.playback_active
            or self.streaming_mode
            or self._is_song_busy()
        )

    def _handle_speech_status(self, message: str, session_id: int) -> None:
        if session_id == self.voice_session_id and self.voice_mode_active:
            self.dialogue.set_dialogue_text(message)

    def _handle_recognized_speech(self, text: str, session_id: int) -> None:
        if session_id != self.voice_session_id or not self.voice_mode_active:
            return
        text = (text or "").strip()
        if not text:
            self._schedule_next_voice_recording(600)
            return
        self.input_panel.input.setText(text)
        self.send_message()

    def _handle_speech_error(self, message: str, session_id: int) -> None:
        if session_id != self.voice_session_id or not self.voice_mode_active:
            return
        self.dialogue.set_dialogue_text(message)
        if self._speech_error_is_fatal(message):
            self.voice_mode_active = False
            self.voice_session_id += 1
            self.input_panel.set_voice_active(False)
            self.set_busy(False)

    def _speech_error_is_fatal(self, message: str) -> bool:
        return is_fatal_speech_error(message)

    def _handle_speech_finished(self, session_id: int) -> None:
        if self.speech_worker and not self.speech_worker.isRunning():
            self.speech_worker.deleteLater()
            self.speech_worker = None
        if session_id != self.voice_session_id:
            return
        if not self.voice_mode_active:
            self.set_busy(self._is_conversation_busy())
            return
        if self._is_conversation_busy():
            self.set_busy(True)
            return
        self.set_busy(False)
        self._schedule_next_voice_recording(650)

    def set_busy(self, busy: bool) -> None:
        if self._is_song_busy():
            self.input_panel.set_busy(False, voice_enabled=True)
            return
        self.input_panel.set_busy(busy, voice_enabled=(not busy or self.voice_mode_active))

    def open_settings_panel(self) -> None:
        if self.settings_panel is None:
            self.settings_panel = SettingsPanel(self)
            self.settings_panel.costume_changed.connect(self.set_costume)
            self.settings_panel.interlocutor_name_changed.connect(self.set_interlocutor_name)
            self.settings_panel.scale_changed.connect(self.set_character_scale)
            self.settings_panel.overall_scale_changed.connect(self.set_overall_scale)
            self.settings_panel.typing_speed_changed.connect(self.set_typewriter_speed)
            self.settings_panel.apply_scale(self.ui_scale)
            self.settings_panel.hide()

        if self.visual_tool is not None:
            self.available_costumes = self.visual_tool.list_costume_sets()
        self.settings_panel.set_costumes(self.available_costumes, self.selected_costume)
        self.settings_panel.set_interlocutor_name(self.interlocutor_name)
        self.settings_panel.set_scale(self.character_scale)
        self.settings_panel.set_overall_scale(self.ui_scale)
        self.settings_panel.set_typing_speed(self.typewriter_speed)
        self.settings_panel.setVisible(not self.settings_panel.isVisible())
        self._layout_overlay()

    def minimize_overlay(self) -> None:
        self.showMinimized()

    def set_costume(self, costume: str) -> None:
        costume = (costume or "").strip()
        if not costume:
            return
        self.selected_costume = costume
        self._set_default_character_for_costume(costume)

    def set_interlocutor_name(self, name: str) -> None:
        name = (name or DEFAULT_INTERLOCUTOR_NAME).strip() or DEFAULT_INTERLOCUTOR_NAME
        self.interlocutor_name = name
        if self.agent is not None:
            self.interlocutor_name = self.agent.set_interlocutor_name(name)
        if self.settings_panel is not None:
            self.settings_panel.set_interlocutor_name(self.interlocutor_name)

    def set_character_scale(self, scale: float) -> None:
        self.character_scale = max(0.5, min(1.8, float(scale)))
        self._layout_overlay()

    def set_overall_scale(self, scale: float) -> None:
        self.ui_scale = max(MIN_UI_SCALE, min(MAX_UI_SCALE, float(scale)))
        self._apply_ui_scale()

    def set_typewriter_speed(self, speed: float) -> None:
        self.typewriter_speed = max(0.5, min(3.0, float(speed)))
        if self.typing_timer is not None:
            self.typing_timer.setInterval(self._typewriter_delay(""))

    def _start_corner_resize(self, event: QMouseEvent) -> None:
        self.drag_offset = None
        self.resize_origin_geometry = self.geometry()
        self.resize_origin_pos = event.globalPosition().toPoint()
        self.resize_origin_ui_scale = self.ui_scale

    def _corner_resize_to(self, event: QMouseEvent) -> None:
        if self.resize_origin_geometry is None or self.resize_origin_pos is None:
            return

        origin = self.resize_origin_geometry
        delta = event.globalPosition().toPoint() - self.resize_origin_pos
        width_ratio = (origin.width() + delta.x()) / max(1, origin.width())
        height_ratio = (origin.height() + delta.y()) / max(1, origin.height())
        factor = max(width_ratio, height_ratio)

        min_factor = max(
            MIN_WINDOW_SIZE.width() / max(1, origin.width()),
            MIN_WINDOW_SIZE.height() / max(1, origin.height()),
            MIN_UI_SCALE / max(0.01, self.resize_origin_ui_scale),
        )
        max_factor = MAX_UI_SCALE / max(0.01, self.resize_origin_ui_scale)

        available_geometry: QRect | None = None
        screen = QGuiApplication.screenAt(origin.center()) or QGuiApplication.primaryScreen()
        if screen is not None:
            available_geometry = screen.availableGeometry()
            max_width = max(MIN_WINDOW_SIZE.width(), available_geometry.width())
            max_height = max(MIN_WINDOW_SIZE.height(), available_geometry.height())
            max_factor = min(
                max_factor,
                max_width / max(1, origin.width()),
                max_height / max(1, origin.height()),
            )

        if max_factor < min_factor:
            max_factor = min_factor
        factor = max(min_factor, min(max_factor, factor))
        new_width = max(MIN_WINDOW_SIZE.width(), round(origin.width() * factor))
        new_height = max(MIN_WINDOW_SIZE.height(), round(origin.height() * factor))
        new_x = origin.x()
        new_y = origin.y()
        if available_geometry is not None:
            new_x = min(new_x, available_geometry.right() + 1 - new_width)
            new_y = min(new_y, available_geometry.bottom() + 1 - new_height)
            new_x = max(available_geometry.x(), new_x)
            new_y = max(available_geometry.y(), new_y)
        self.ui_scale = max(MIN_UI_SCALE, min(MAX_UI_SCALE, self.resize_origin_ui_scale * factor))
        if self.settings_panel is not None:
            self.settings_panel.set_overall_scale(self.ui_scale)
        self.setGeometry(new_x, new_y, new_width, new_height)
        self._apply_ui_scale()

    def _finish_corner_resize(self, event: QMouseEvent) -> None:
        del event
        self.resize_origin_geometry = None
        self.resize_origin_pos = None
        self._update_click_through_mask()

    def _update_click_through_mask(self) -> None:
        if DEBUG_NORMAL_WINDOW:
            self.clearMask()
            return
        if self.width() <= 1 or self.height() <= 1:
            return

        region = QRegion(self._controls_drag_rect())
        region = region.united(self._character_hit_region())
        for widget, margin in (
            (self.dialogue, 1),
            (self.input_panel, 1),
            (self.window_controls, 2),
            (self.settings_panel, 1),
            (self.resize_handle, 2),
        ):
            region = region.united(self._widget_hit_region(widget, margin))

        if region.isEmpty():
            self.clearMask()
            return
        self.setMask(region.intersected(QRegion(self.rect())))

    def _controls_drag_rect(self) -> QRect:
        controls_rect = self.window_controls.geometry()
        if controls_rect.isEmpty():
            return QRect()
        top_margin = max(1, controls_rect.y())
        height = controls_rect.height() + top_margin * 2
        return QRect(0, 0, self.width(), min(self.height(), height))

    def _widget_hit_region(self, widget: QWidget | None, margin: int = 0) -> QRegion:
        if widget is None or widget.isHidden():
            return QRegion()
        rect = widget.geometry().adjusted(-margin, -margin, margin, margin).intersected(self.rect())
        if rect.isEmpty():
            return QRegion()
        return QRegion(rect)

    def _character_hit_region(self) -> QRegion:
        if self.character_label.isHidden():
            return QRegion()
        pixmap = self.character_label.pixmap()
        if pixmap is None or pixmap.isNull():
            return QRegion()

        pixmap_rect = self._character_pixmap_rect(pixmap)
        if pixmap_rect.isEmpty():
            return QRegion()
        if self.resize_origin_geometry is not None:
            return QRegion(
                pixmap_rect.adjusted(
                    -CHARACTER_HIT_MARGIN,
                    -CHARACTER_HIT_MARGIN,
                    CHARACTER_HIT_MARGIN,
                    CHARACTER_HIT_MARGIN,
                ).intersected(self.rect())
            )

        image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
        region = self._alpha_hit_region(image, pixmap_rect.topLeft())
        if region.isEmpty():
            return QRegion(pixmap_rect.intersected(self.rect()))
        return region.intersected(QRegion(self.rect()))

    def _character_pixmap_rect(self, pixmap: QPixmap) -> QRect:
        label_rect = self.character_label.geometry()
        pixmap_width = pixmap.width()
        pixmap_height = pixmap.height()
        alignment = self.character_label.alignment()

        x = label_rect.x()
        if bool(alignment & Qt.AlignmentFlag.AlignHCenter):
            x += (label_rect.width() - pixmap_width) // 2
        elif bool(alignment & Qt.AlignmentFlag.AlignRight):
            x += label_rect.width() - pixmap_width

        y = label_rect.y()
        if bool(alignment & Qt.AlignmentFlag.AlignVCenter):
            y += (label_rect.height() - pixmap_height) // 2
        elif bool(alignment & Qt.AlignmentFlag.AlignBottom):
            y += label_rect.height() - pixmap_height

        return QRect(x, y, pixmap_width, pixmap_height)

    def _alpha_hit_region(self, image: QImage, origin: QPoint) -> QRegion:
        if image.isNull():
            return QRegion()

        width = image.width()
        height = image.height()
        margin = CHARACTER_HIT_MARGIN
        region = QRegion()

        def add_run(start: int, stop: int, y: int) -> None:
            nonlocal region
            left = max(0, start - margin)
            right = min(width, stop + margin)
            top = max(0, y - margin)
            bottom = min(height, y + margin + 1)
            if right <= left or bottom <= top:
                return
            region = region.united(
                QRegion(QRect(origin.x() + left, origin.y() + top, right - left, bottom - top))
            )

        for y in range(height):
            run_start = -1
            for x in range(width):
                if image.pixelColor(x, y).alpha() > CHARACTER_HIT_ALPHA_THRESHOLD:
                    if run_start < 0:
                        run_start = x
                elif run_start >= 0:
                    add_run(run_start, x, y)
                    run_start = -1
            if run_start >= 0:
                add_run(run_start, width, y)

        return region

    def eventFilter(self, watched: QObject, event) -> bool:  # noqa: N802 - Qt override
        draggable_widgets = (
            getattr(self, "character_label", None),
            getattr(self, "dialogue", None),
            getattr(self, "window_controls", None),
        )
        if watched in draggable_widgets:
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._start_drag(event)
                return False
            if event.type() == QEvent.Type.MouseMove and self.drag_offset is not None:
                self._drag_to(event)
                return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                self.drag_offset = None
                return False
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self._start_drag(event)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if self.drag_offset is not None:
            self._drag_to(event)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        self.drag_offset = None
        super().mouseReleaseEvent(event)

    def _start_drag(self, event: QMouseEvent) -> None:
        self.drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def _drag_to(self, event: QMouseEvent) -> None:
        self.move(event.globalPosition().toPoint() - self.drag_offset)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._clear_cue_timers()
        self._stop_typewriter()
        self._stop_audio()
        self._release_preloaded_audio_players()
        self._release_song_audio_player()
        if self.song_worker and self.song_worker.isRunning():
            self.song_worker.cancel()
            self.song_worker.wait(1500)
        if self.chat_worker and self.chat_worker.isRunning():
            self.chat_worker.requestInterruption()
            self.chat_worker.wait(1500)
        if self.speech_worker and self.speech_worker.isRunning():
            self.voice_mode_active = False
            self.voice_session_id += 1
            self.speech_worker.requestInterruption()
            self.speech_worker.quit()
            self.speech_worker.wait(1500)
        if self.startup_warmup_worker and self.startup_warmup_worker.isRunning():
            self.startup_warmup_worker.quit()
            self.startup_warmup_worker.wait(1500)
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    window = OverlayWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
