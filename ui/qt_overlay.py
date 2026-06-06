from __future__ import annotations

import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, QTimer, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QMouseEvent, QPixmap, QRegion
from PySide6.QtWidgets import QApplication, QGraphicsDropShadowEffect, QLabel, QWidget

from agent.character_loader import DEFAULT_INTERLOCUTOR_NAME
from spica.host.app_host import AppHost
from ui.controllers.audio_controller import AudioController
from ui.controllers.chat_stream_controller import ChatStreamController
from ui.controllers.interaction_controller import InteractionController
from ui.controllers.song_controller import SongController
from ui.controllers.typewriter_controller import TypewriterController
from ui.controllers.voice_input_controller import VoiceInputController
from ui.overlay_config import OverlayConfig, load_overlay_config
from ui.workers.screenshot_worker import ScreenshotWorker
from ui.workers.startup_warmup_worker import StartupWarmupWorker
from ui.widgets.common import MAX_UI_SCALE, MIN_UI_SCALE, scaled_px
from ui.widgets.dialogue_box import TintedDialogueBox
from ui.widgets.input_panel import InputPanel
from ui.widgets.resize_handle import CornerResizeHandle
from ui.widgets.screenshot_selector import ScreenshotSelectionOverlay
from ui.widgets.settings_panel import SettingsPanel
from ui.widgets.window_controls import WindowControls

BASE_DIR = Path(__file__).resolve().parents[1]
DEBUG_NORMAL_WINDOW = False
MIN_WINDOW_SIZE = QSize(460, 360)
CHARACTER_HIT_ALPHA_THRESHOLD = 8
CHARACTER_HIT_MARGIN = 7

logger = logging.getLogger(__name__)


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

        self.overlay_config: OverlayConfig = load_overlay_config()
        self.host: AppHost | None = None
        self.visual_tool: Any | None = None
        self.tts_tool: Any | None = None
        self.tts_adapter: Any | None = None
        self.agent: Any | None = None
        self.chat_stream_controller: ChatStreamController | None = None
        self.song_controller: SongController | None = None
        self.voice_input_controller: VoiceInputController | None = None
        self.interaction_controller: InteractionController | None = None
        self.startup_warmup_worker: StartupWarmupWorker | None = None
        self.screenshot_worker: ScreenshotWorker | None = None
        self.conversation_id = str(uuid.uuid4())
        self.drag_offset: QPoint | None = None
        self.resize_origin_geometry: QRect | None = None
        self.resize_origin_pos: QPoint | None = None
        self.resize_origin_ui_scale = 1.0
        self.current_pixmap: QPixmap | None = None
        self.current_pixmap_cache_key: str | None = None
        self.pixmap_cache: dict[str, QPixmap] = {}
        self.scaled_pixmap_cache: dict[tuple[str, int, int, float, float], QPixmap] = {}
        self.available_costumes: list[str] = []
        self.selected_costume: str | None = None
        self.interlocutor_name = DEFAULT_INTERLOCUTOR_NAME
        self.character_scale = self.overlay_config.default_character_scale
        self.ui_scale = self.overlay_config.default_ui_scale
        self.character_label_height_scale = self.overlay_config.character_label_height_scale
        self.overlay_initial_height_scale = self.overlay_config.overlay_initial_height_scale
        self.character_max_height_ratio = self.overlay_config.character_max_height_ratio
        self._last_layout_log_state: tuple[Any, ...] | None = None
        self.settings_panel: SettingsPanel | None = None
        self.screenshot_selector: ScreenshotSelectionOverlay | None = None
        self.pending_screen_attachment: dict[str, Any] | None = None

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
        self.typewriter_controller = TypewriterController(
            self,
            self.dialogue.set_dialogue_text,
            default_speed=self.overlay_config.default_typewriter_speed,
        )
        self.audio_controller = AudioController(self)

        self.input_panel = InputPanel(self)
        self.input_panel.send_requested.connect(self.send_message)
        self.input_panel.voice_requested.connect(self.toggle_voice)
        self.input_panel.screenshot_requested.connect(self.toggle_screenshot_selection)
        self.voice_input_controller = VoiceInputController(
            parent=self,
            set_voice_active=self.input_panel.set_voice_active,
            set_busy=self.set_busy,
            is_conversation_busy=self._is_conversation_busy,
            set_dialogue_text=self.dialogue.set_dialogue_text,
            on_recognized_text=lambda text: None,
            backend_ready=lambda: self.agent is not None,
        )
        self.song_controller = SongController(
            parent=self,
            chat_stream_controller=None,
            audio_controller=self.audio_controller,
            typewriter_controller=self.typewriter_controller,
            visual_overrides_provider=self._visual_overrides,
            set_busy=self.set_busy,
            focus_input=self._focus_input,
            stop_conversation_for_song=lambda: None,
            voice_mode_active_provider=self._is_voice_mode_active,
            schedule_voice_recording=self._schedule_next_voice_recording,
        )
        self.interaction_controller = InteractionController(
            parent=self,
            chat_stream_controller=None,
            song_controller=self.song_controller,
            audio_controller=self.audio_controller,
            voice_input_controller=self.voice_input_controller,
            focus_input=self._focus_input,
            set_busy=self.set_busy,
            screen_attachment_provider=lambda: self.pending_screen_attachment,
            consume_screen_attachment=self.consume_pending_screenshot,
        )
        self.voice_input_controller.set_on_recognized_text(self.interaction_controller.handle_user_text)
        self.song_controller.set_stop_conversation_for_song(self.interaction_controller.stop_conversation_for_song)

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
        # Composition root now lives in AppHost.initialize() (Phase 1). The UI no
        # longer constructs services; it reads them back from the host. Qt wiring
        # (chat stream controller, dialogue messages) stays here.
        self.host = AppHost()
        try:
            self.host.initialize()
            self.visual_tool = self.host.visual_tool
            self.tts_tool = self.host.tts_tool
            self.tts_adapter = self.host.tts_adapter
            self.agent = self.host.conversation_surface
            self._init_chat_stream_controller()
            self.interlocutor_name = self.agent.interlocutor_name
            provider_name = str(getattr(self.tts_adapter, "name", None) or self.host.tts_provider)
            self.dialogue.set_dialogue_text(f"LLM API 初始化完成，准备预热 {provider_name}...")
        except Exception as exc:
            # initialize() salvages visual_tool best-effort before re-raising, so
            # the character can still render even when the backend fails.
            self.visual_tool = self.host.visual_tool
            self.dialogue.set_dialogue_text(f"初始化后端失败：{exc}")

    def _init_chat_stream_controller(self) -> None:
        if self.agent is None:
            self.chat_stream_controller = None
            return
        self.chat_stream_controller = ChatStreamController(
            parent=self,
            agent=self.agent,
            conversation_id_provider=lambda: self.conversation_id,
            visual_overrides_provider=self._visual_overrides,
            audio_controller=self.audio_controller,
            typewriter_controller=self.typewriter_controller,
            set_character_image=lambda image: self.set_character_image(BASE_DIR / str(image)),
            set_busy=self.set_busy,
            on_chat_done=self._handle_chat_stream_done,
            on_error=self._handle_chat_error,
            apply_visual=self._apply_visual,
        )
        if self.song_controller is not None:
            self.song_controller.set_chat_stream_controller(self.chat_stream_controller)
        if self.interaction_controller is not None:
            self.interaction_controller.set_chat_stream_controller(self.chat_stream_controller)

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
        base_height = min(max(560, int(available.height() * 0.70)), int(available.height() * 0.82))
        height = min(
            available.height(),
            max(MIN_WINDOW_SIZE.height(), int(base_height * self.overlay_initial_height_scale)),
        )
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
        self._clear_scaled_pixmap_cache("resize")
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
        raw_character_height = int(
            base_character_height
            * self.character_scale
            * self.character_label_height_scale
            * scale
        )
        max_character_height = int(height * self.character_max_height_ratio)
        character_height = max(scaled_px(280, scale), min(raw_character_height, max_character_height))
        character_width = self._character_width_for_height(character_height)
        character_width = min(character_width, int(width * 0.94))
        character_x = (width - character_width) // 2
        character_bottom = min(height - 8, input_y + int(input_height * 0.28))
        character_y = max(0, character_bottom - character_height)
        self.character_label.setGeometry(character_x, character_y, character_width, character_height)
        self._log_overlay_layout_config()
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

    def _log_overlay_layout_config(self) -> None:
        state = (
            round(float(self.character_scale), 4),
            round(float(self.ui_scale), 4),
            round(float(self.typewriter_controller.typewriter_speed), 4),
            round(float(self.character_label_height_scale), 4),
            round(float(self.overlay_initial_height_scale), 4),
            round(float(self.character_max_height_ratio), 4),
            self.character_label.width(),
            self.character_label.height(),
            self.width(),
            self.height(),
        )
        if state == self._last_layout_log_state:
            return
        self._last_layout_log_state = state
        logger.info(
            "event=overlay_layout_config default_character_scale=%s default_ui_scale=%s "
            "default_typewriter_speed=%s character_label_height_scale=%s "
            "overlay_initial_height_scale=%s character_max_height_ratio=%s "
            "final_character_label_size=%sx%s window_size=%sx%s",
            self.overlay_config.default_character_scale,
            self.overlay_config.default_ui_scale,
            self.overlay_config.default_typewriter_speed,
            self.character_label_height_scale,
            self.overlay_initial_height_scale,
            self.character_max_height_ratio,
            self.character_label.width(),
            self.character_label.height(),
            self.width(),
            self.height(),
        )

    def _character_width_for_height(self, target_height: int) -> int:
        if self.current_pixmap is None or self.current_pixmap.isNull():
            return int(target_height * 0.55)
        ratio = self.current_pixmap.width() / max(1, self.current_pixmap.height())
        return max(220, int(target_height * ratio))

    def _now_ms(self) -> float:
        return round(time.perf_counter() * 1000.0, 2)

    def _duration_ms(self, started_at_ms: float) -> float:
        return round(self._now_ms() - started_at_ms, 2)

    def _log_character_image_event(self, event: str, **fields: Any) -> None:
        field_parts = " ".join(f"{key}={value!r}" for key, value in fields.items())
        suffix = f" {field_parts}" if field_parts else ""
        logger.debug("event=%s monotonic_ms=%s%s", event, self._now_ms(), suffix)

    def _clear_scaled_pixmap_cache(self, reason: str) -> None:
        if not self.scaled_pixmap_cache:
            return
        cache_size = len(self.scaled_pixmap_cache)
        self.scaled_pixmap_cache.clear()
        self._log_character_image_event("scaled_cache_clear", reason=reason, cache_size=cache_size)

    def _scaled_pixmap_cache_key(self) -> tuple[str, int, int, float, float] | None:
        if not self.current_pixmap_cache_key:
            return None
        size = self.character_label.size()
        if size.width() <= 0 or size.height() <= 0:
            return None
        return (
            self.current_pixmap_cache_key,
            size.width(),
            size.height(),
            round(float(self.ui_scale), 4),
            round(float(self.character_scale), 4),
        )

    def _rescale_character(self) -> None:
        if self.current_pixmap is None or self.current_pixmap.isNull():
            return
        scaled_cache_key = self._scaled_pixmap_cache_key()
        if scaled_cache_key is not None:
            cached_scaled = self.scaled_pixmap_cache.get(scaled_cache_key)
            if cached_scaled is not None and not cached_scaled.isNull():
                self._log_character_image_event(
                    "scaled_cache_hit",
                    path=scaled_cache_key[0],
                    label_size=f"{scaled_cache_key[1]}x{scaled_cache_key[2]}",
                    cache_size=len(self.scaled_pixmap_cache),
                )
                label_started_at_ms = self._now_ms()
                self._log_character_image_event("label_update_start")
                self.character_label.setPixmap(cached_scaled)
                self._log_character_image_event(
                    "label_update_done",
                    duration_ms=self._duration_ms(label_started_at_ms),
                )
                return

            self._log_character_image_event(
                "scaled_cache_miss",
                path=scaled_cache_key[0] if scaled_cache_key is not None else None,
                label_size=f"{self.character_label.width()}x{self.character_label.height()}",
                cache_size=len(self.scaled_pixmap_cache),
            )

        scale_started_at_ms = self._now_ms()
        self._log_character_image_event(
            "pixmap_scale_start",
            label_size=f"{self.character_label.width()}x{self.character_label.height()}",
            pixmap_size=f"{self.current_pixmap.width()}x{self.current_pixmap.height()}",
        )
        scaled = self.current_pixmap.scaled(
            self.character_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._log_character_image_event(
            "pixmap_scale_done",
            duration_ms=self._duration_ms(scale_started_at_ms),
            scaled_size=f"{scaled.width()}x{scaled.height()}",
        )
        if scaled_cache_key is not None and not scaled.isNull():
            self.scaled_pixmap_cache[scaled_cache_key] = scaled
        label_started_at_ms = self._now_ms()
        self._log_character_image_event("label_update_start")
        self.character_label.setPixmap(scaled)
        self._log_character_image_event(
            "label_update_done",
            duration_ms=self._duration_ms(label_started_at_ms),
        )

    def set_character_image(self, path: str | Path | None) -> None:
        if not path:
            return
        started_at_ms = self._now_ms()
        cache_key = str(Path(path).resolve())
        self._log_character_image_event("set_character_image_start", path=cache_key)
        raw_pixmap = self.pixmap_cache.get(cache_key)
        if raw_pixmap is not None and not raw_pixmap.isNull():
            self._log_character_image_event("cache_hit", path=cache_key, cache_size=len(self.pixmap_cache))
            self._log_character_image_event("raw_cache_hit", path=cache_key, cache_size=len(self.pixmap_cache))
            self.current_pixmap = raw_pixmap
            self.current_pixmap_cache_key = cache_key
            self._layout_overlay()
            duration_ms = self._duration_ms(started_at_ms)
            self._log_character_image_event("set_character_image_done", path=cache_key, duration_ms=duration_ms)
            if duration_ms > 100:
                logger.warning("event=set_character_image_slow monotonic_ms=%s path=%r duration_ms=%s", self._now_ms(), cache_key, duration_ms)
            return

        self._log_character_image_event("cache_miss", path=cache_key, cache_size=len(self.pixmap_cache))
        self._log_character_image_event("raw_cache_miss", path=cache_key, cache_size=len(self.pixmap_cache))
        load_started_at_ms = self._now_ms()
        self._log_character_image_event("image_load_start", path=cache_key)
        pixmap = QPixmap(str(path))
        self._log_character_image_event(
            "image_load_done",
            path=cache_key,
            duration_ms=self._duration_ms(load_started_at_ms),
            is_null=pixmap.isNull(),
        )
        if pixmap.isNull():
            duration_ms = self._duration_ms(started_at_ms)
            self._log_character_image_event("set_character_image_done", path=cache_key, duration_ms=duration_ms, loaded=False)
            if duration_ms > 100:
                logger.warning("event=set_character_image_slow monotonic_ms=%s path=%r duration_ms=%s", self._now_ms(), cache_key, duration_ms)
            return
        self.current_pixmap = pixmap
        self.current_pixmap_cache_key = cache_key
        self.pixmap_cache[cache_key] = self.current_pixmap
        self._layout_overlay()
        duration_ms = self._duration_ms(started_at_ms)
        self._log_character_image_event("set_character_image_done", path=cache_key, duration_ms=duration_ms, loaded=True)
        if duration_ms > 100:
            logger.warning("event=set_character_image_slow monotonic_ms=%s path=%r duration_ms=%s", self._now_ms(), cache_key, duration_ms)

    def _trim_transparent_pixmap(self, pixmap: QPixmap) -> QPixmap:
        # This is an O(width * height) Python alpha scan. Keep it out of
        # playback hot paths; use only for offline/explicit image processing.
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
        self._clear_scaled_pixmap_cache("ui_scale")
        self.typewriter_controller.set_scale(self.ui_scale)
        self.dialogue.apply_scale(self.ui_scale)
        self.input_panel.apply_scale(self.ui_scale)
        self.window_controls.apply_scale(self.ui_scale)
        self.resize_handle.apply_scale(self.ui_scale)
        if self.settings_panel is not None:
            self.settings_panel.apply_scale(self.ui_scale)
        self._layout_overlay()

    def send_message(self) -> None:
        message = self.input_panel.input.text().strip()
        if not message and self.pending_screen_attachment is None:
            self.input_panel.input.setFocus()
            return

        self.input_panel.input.clear()
        if self.interaction_controller is not None:
            self.interaction_controller.handle_user_text(message)

    def toggle_screenshot_selection(self) -> None:
        if self.pending_screen_attachment is not None:
            self.clear_pending_screenshot(show_message=True)
            return
        if self._is_conversation_busy():
            self.input_panel.set_screenshot_pending(False)
            return
        self.input_panel.set_screenshot_pending(False)
        self._open_screenshot_selector()

    def _open_screenshot_selector(self) -> None:
        if self.screenshot_selector is not None:
            try:
                self.screenshot_selector.close()
            except Exception:
                pass
            self.screenshot_selector = None

        screen = QGuiApplication.screenAt(self.frameGeometry().center()) or QGuiApplication.primaryScreen()
        self.screenshot_selector = ScreenshotSelectionOverlay(screen=screen)
        self.screenshot_selector.selection_finished.connect(self._handle_screenshot_selection_finished)
        self.screenshot_selector.selection_cancelled.connect(self._handle_screenshot_selection_cancelled)
        self.dialogue.set_dialogue_text("拖拽选择要让 Spica 查看的一块区域，按 Esc 取消。")
        self.screenshot_selector.begin()

    def _handle_screenshot_selection_finished(self, payload: dict[str, Any]) -> None:
        self.screenshot_selector = None
        QTimer.singleShot(120, lambda data=dict(payload): self._capture_selected_region(data))

    def _handle_screenshot_selection_cancelled(self, reason: str) -> None:
        self.screenshot_selector = None
        self.input_panel.set_screenshot_pending(False)
        self.pending_screen_attachment = None
        if reason == "截图区域太小":
            self.dialogue.set_dialogue_text("截图区域太小")
        else:
            self.dialogue.set_dialogue_text("已取消截图。")

    def _capture_selected_region(self, payload: dict[str, Any]) -> None:
        self._start_screenshot_worker(payload)

    def _start_screenshot_worker(self, payload: dict[str, Any]) -> None:
        if self.screenshot_worker is not None and self.screenshot_worker.isRunning():
            self.dialogue.set_dialogue_text("正在处理截图...")
            return

        self.pending_screen_attachment = None
        self.input_panel.set_screenshot_pending(False)
        self.input_panel.screenshot_button.setEnabled(False)
        self.dialogue.set_dialogue_text("正在处理截图...")

        worker = ScreenshotWorker(payload)
        self.screenshot_worker = worker
        worker.finished_ok.connect(self._handle_screenshot_worker_done)
        worker.failed.connect(self._handle_screenshot_worker_failed)
        worker.finished.connect(self._handle_screenshot_worker_finished)
        worker.start()

    def _handle_screenshot_worker_done(self, attachment: dict[str, Any]) -> None:
        self.pending_screen_attachment = attachment
        self.input_panel.screenshot_button.setEnabled(True)
        self.input_panel.set_screenshot_pending(True)
        self.dialogue.set_dialogue_text("截图已准备好。输入问题后发送，或直接发送让我概括。")
        self._focus_input()

    def _handle_screenshot_worker_failed(self, message: str) -> None:
        self.pending_screen_attachment = None
        self.input_panel.screenshot_button.setEnabled(True)
        self.input_panel.set_screenshot_pending(False)
        self.dialogue.set_dialogue_text(f"截图失败：{message}")

    def _handle_screenshot_worker_finished(self) -> None:
        worker = self.screenshot_worker
        self.screenshot_worker = None
        if worker is not None:
            worker.deleteLater()

    def clear_pending_screenshot(self, show_message: bool = False) -> None:
        self.pending_screen_attachment = None
        self.input_panel.set_screenshot_pending(False)
        if show_message:
            self.dialogue.set_dialogue_text("已取消待发送截图。")

    def consume_pending_screenshot(self) -> dict[str, Any] | None:
        attachment = self.pending_screen_attachment
        self.pending_screen_attachment = None
        self.input_panel.set_screenshot_pending(False)
        return attachment

    def _is_song_busy(self) -> bool:
        return bool(self.song_controller is not None and self.song_controller.is_busy())

    def _focus_input(self) -> None:
        self.input_panel.input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _handle_chat_stream_done(self) -> None:
        if self._is_voice_mode_active():
            self._schedule_next_voice_recording(320)
        else:
            self._focus_input()

    def _handle_chat_error(self, message: str) -> None:
        if self._is_song_busy():
            return
        self.typewriter_controller.stop()
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

    def toggle_voice(self, checked: bool = False) -> None:
        del checked
        if self.voice_input_controller is not None:
            self.voice_input_controller.toggle()

    def _schedule_next_voice_recording(self, delay_ms: int = 320) -> None:
        if self.voice_input_controller is not None:
            self.voice_input_controller.schedule_next_recording(delay_ms)

    def _is_voice_mode_active(self) -> bool:
        return bool(self.voice_input_controller is not None and self.voice_input_controller.voice_mode_active)

    def _is_conversation_busy(self) -> bool:
        return bool(
            (self.chat_stream_controller is not None and self.chat_stream_controller.is_busy())
            or self._is_song_busy()
        )

    def set_busy(self, busy: bool) -> None:
        if self._is_song_busy():
            self.input_panel.set_busy(False, voice_enabled=True)
            if self.screenshot_worker is not None and self.screenshot_worker.isRunning():
                self.input_panel.screenshot_button.setEnabled(False)
            return
        self.input_panel.set_busy(busy, voice_enabled=(not busy or self._is_voice_mode_active()))
        if self.screenshot_worker is not None and self.screenshot_worker.isRunning():
            self.input_panel.screenshot_button.setEnabled(False)

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
        self.settings_panel.set_typing_speed(self.typewriter_controller.typewriter_speed)
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
        next_scale = max(0.5, min(1.8, float(scale)))
        if next_scale != self.character_scale:
            self._clear_scaled_pixmap_cache("character_scale")
        self.character_scale = next_scale
        self._layout_overlay()

    def set_overall_scale(self, scale: float) -> None:
        self.ui_scale = max(MIN_UI_SCALE, min(MAX_UI_SCALE, float(scale)))
        self._apply_ui_scale()

    def set_typewriter_speed(self, speed: float) -> None:
        self.typewriter_controller.set_speed(speed)

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
        self.typewriter_controller.stop()
        self.audio_controller.stop_all()
        if self.song_controller is not None:
            self.song_controller.shutdown(1500)
        if self.chat_stream_controller is not None:
            self.chat_stream_controller.shutdown(1500)
        if self.voice_input_controller is not None:
            self.voice_input_controller.shutdown(1500)
        if self.screenshot_selector is not None:
            try:
                self.screenshot_selector.close()
            except Exception:
                pass
            self.screenshot_selector = None
        if self.screenshot_worker is not None and self.screenshot_worker.isRunning():
            self.screenshot_worker.quit()
            self.screenshot_worker.wait(1500)
            self.screenshot_worker = None
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
