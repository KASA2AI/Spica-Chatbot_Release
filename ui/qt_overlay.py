from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractAnimation, QEvent, QObject, QPoint, QRect, QSize, QThread, QTimer, Qt, Signal
from PySide6.QtGui import (
    QBitmap,
    QColor,
    QGuiApplication,
    QImage,
    QMouseEvent,
    QPainter,
    QPixmap,
    QRegion,
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QLabel,
    QMessageBox,
    QPushButton,
    QWidget,
)

from spica.config.secrets import LoadedSecrets, load_secrets
from spica.conversation.character_loader import DEFAULT_INTERLOCUTOR_NAME
from spica.core.proactive import NO_COMMENT_SENTINEL, ProactiveTurnArbiter
from spica.host.app_host import AppHost
from ui.controllers.anime_controller import AnimeController
from ui.controllers.audio_controller import AudioController
from ui.controllers.character_settings_controller import CharacterSettingsController
from ui.controllers.application_settings_controller import ApplicationSettingsController
from ui.controllers.chat_stream_controller import ChatStreamController
from ui.controllers.companion_event_bridge import CompanionEventBridge
from ui.controllers.dialogue_visibility_controller import (
    DialogueVisibilityController,
)
from ui.controllers.galgame_controller import (
    GalgameController,
    ScreenGeometry,
    physical_point_to_screen_index,
    selection_to_physical_screen_rect,
)
from ui.controllers.interaction_controller import InteractionController
from ui.controllers.song_controller import SongController
from ui.controllers.typewriter_controller import TypewriterController
from ui.controllers.voice_input_controller import ReactionVoiceDuckGate, VoiceInputController
from ui.layered_sprite_store import LocalSpriteStore, PixmapByteCache
from ui.overlay_config import (
    load_dialogue_opacity,
    load_overlay_preferences,
    save_dialogue_opacity,
    save_dialogue_box_visible,
    save_overlay_config_value,
)
from ui.widgets.window_picker_dialog import WindowPickerDialog
from ui.workers.companion_action_worker import CompanionActionWorker
from ui.workers.screenshot_worker import ScreenshotWorker
from ui.workers.startup_warmup_worker import StartupWarmupWorker
from ui.widgets.character_sprite import CharacterSpriteView
from ui.widgets.common import (
    MAX_DIALOGUE_OPACITY, MIN_DIALOGUE_OPACITY, MAX_UI_SCALE, MIN_UI_SCALE, scaled_px,
)
from ui.widgets.dialogue_box import TintedDialogueBox
from ui.widgets.input_panel import InputPanel
from ui.widgets.resize_handle import CornerResizeHandle
from ui.widgets.screenshot_selector import ScreenshotSelectionOverlay
from ui.widgets.settings_panel import PANEL_SLIDE_PX, SettingsPanel
from ui.widgets.window_controls import WindowControls

BASE_DIR = Path(__file__).resolve().parents[1]
DEBUG_NORMAL_WINDOW = False
MIN_WINDOW_SIZE = QSize(460, 360)
CHARACTER_HIT_ALPHA_THRESHOLD = 8
CHARACTER_HIT_MARGIN = 7
SCALED_PIXMAP_CACHE_BYTES = 48 * 1024 * 1024
# Voice-mode visualisation (display-only): how long the recognized whole sentence
# lingers in the input box before it is cleared. The turn auto-submits immediately
# regardless, so this governs ONLY when the box returns to normal -- long enough to
# read, always shorter than the gap before the next utterance (her reply + rearm +
# the user speaking again). See OverlayWindow._on_voice_recognized_text.
_VOICE_TRANSCRIPT_LINGER_MS = 1200
_FORCED_CLOSE_WARNING = (
    "后台未能安全停止；再次关闭将立即强制退出。未完成的回复将被丢弃。"
)

logger = logging.getLogger(__name__)


def _force_process_exit(exit_code: int) -> None:
    """Fail-stop without running Python/Qt teardown on live native owners."""

    os._exit(exit_code)


def _log_desktop_close_error(message: str, *args: Any) -> None:
    """Best-effort evidence that cannot block the forced-close ladder."""

    try:
        logger.error(message, *args)
    except Exception:
        pass


class OverlayWindow(QWidget):
    # Marshal a worker-thread (reaction-engine) system-turn start onto the GUI
    # thread -- see _start_system_turn. Qt signals must be class attributes.
    _system_turn_requested = Signal(object)  # carries a ProactiveTurnRequest

    def __init__(self, *, loaded_secrets: LoadedSecrets | None = None) -> None:
        super().__init__(None)
        self._loaded_secrets = loaded_secrets
        self._restart_requested = False
        self._restart_timer = QTimer(self)
        self._restart_timer.setInterval(250)
        self._restart_timer.timeout.connect(self.close)
        self._forced_close_armed = False
        self._forced_close_owners: tuple[str, ...] = ()
        self._forced_close_armed_at: float | None = None
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
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAutoFillBackground(False)
        self.setStyleSheet("OverlayWindow { background: transparent; }")

        (
            self.overlay_config,
            self.dialogue_box_visible,
        ) = load_overlay_preferences()
        self.dialogue_opacity = load_dialogue_opacity()
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
        # Display-only mirror of the last STT whole sentence written into the input
        # box (voice-mode visualisation). Tracked so the linger timer clears ONLY our
        # own preview -- never a user draft or a newer sentence. See
        # _on_voice_recognized_text / _clear_voice_transcript.
        self._voice_transcript_shown: str | None = None
        # STABLE conversation id (FINDINGS #16): the Initial-release uuid4 here
        # siloed every long-term memory under spica::<per-launch-uuid>, making
        # character memory amnesiac across restarts. "default" aligns with
        # run_voice/remember/§27①/the play-history card -- the persistent silo.
        self.conversation_id = "default"
        self.drag_offset: QPoint | None = None
        self._drag_press_pos: QPoint | None = None
        self.resize_origin_geometry: QRect | None = None
        self.resize_origin_pos: QPoint | None = None
        self.resize_origin_ui_scale = 1.0
        self.current_pixmap: QPixmap | None = None
        self.current_pixmap_cache_key: str | None = None
        self.sprite_store = LocalSpriteStore()
        self.scaled_pixmap_cache = PixmapByteCache(
            max_bytes=SCALED_PIXMAP_CACHE_BYTES,
        )
        self.available_costumes: list[str] = []
        self.selected_costume: str | None = None
        self.interlocutor_name = DEFAULT_INTERLOCUTOR_NAME
        self.character_scale = self.overlay_config.default_character_scale
        self.ui_scale = self.overlay_config.default_ui_scale
        self.character_label_height_scale = self.overlay_config.character_label_height_scale
        self.overlay_initial_height_scale = self.overlay_config.overlay_initial_height_scale
        self.character_max_height_ratio = self.overlay_config.character_max_height_ratio
        self.spica_voice_volume = self.overlay_config.spica_voice_volume
        self._voice_volume_save_timer = QTimer(self)
        self._voice_volume_save_timer.setSingleShot(True)
        self._voice_volume_save_timer.setInterval(600)
        self._voice_volume_save_timer.timeout.connect(self.persist_spica_voice_volume)
        self._opacity_save_timer = QTimer(self)
        self._opacity_save_timer.setSingleShot(True)
        self._opacity_save_timer.setInterval(600)
        self._opacity_save_timer.timeout.connect(self.persist_dialogue_opacity)
        self._character_region_key = None
        self._character_region = QRegion()
        self._applied_visual_scale: float | None = None
        self._last_layout_log_state: tuple[Any, ...] | None = None
        self.settings_panel: SettingsPanel | None = None
        self.screenshot_selector: ScreenshotSelectionOverlay | None = None
        self.pending_screen_attachment: dict[str, Any] | None = None
        # galgame companion (stage 3): bridge + UI coordinator + status chip.
        self.companion_bridge: CompanionEventBridge | None = None
        self.galgame_controller: GalgameController | None = None
        self.companion_region_selector: ScreenshotSelectionOverlay | None = None
        self.dangling_recovery_worker: CompanionActionWorker | None = None
        self._dangling_recovery_started = False
        # anime-watch (Phase 4): its own bridge instance (the host keeps the
        # anime sink OFF the companion tee) + download controller + status chip.
        self.anime_bridge: CompanionEventBridge | None = None
        self.anime_controller: AnimeController | None = None
        # Mouse press and click can straddle queued ready/new-request events.
        # Keep the identity visible at press until that matching click consumes it.
        self._anime_cancel_pressed_request_id: str | None = None

        self.character_label = CharacterSpriteView(self)
        self.character_label.setObjectName("character")
        self.character_label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
        self.character_label.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.character_label.setStyleSheet("QLabel#character { background: transparent; }")
        self.character_label.setScaledContents(False)
        self.character_label.installEventFilter(self)
        self.character_label.setCursor(Qt.CursorShape.OpenHandCursor)
        self.character_label.setToolTip("按住人物拖动窗口")

        try:
            shadow = QGraphicsDropShadowEffect(self.character_label)
            shadow.setBlurRadius(2)
            shadow.setOffset(0, 1)
            shadow.setColor(QColor(57, 76, 93, 28))
            self.character_label.setGraphicsEffect(shadow)
        except Exception:
            pass

        self.dialogue = TintedDialogueBox(self)
        self.dialogue.set_opacity(self.dialogue_opacity)
        self.dialogue.installEventFilter(self)
        self.dialogue.speaker_label.installEventFilter(self)
        self.dialogue_visibility_controller = DialogueVisibilityController(
            self.dialogue,
            update_click_through_mask=self._update_click_through_mask,
            user_hidden=not self.dialogue_box_visible,
            parent=self,
        )
        self.typewriter_controller = TypewriterController(
            self,
            self.dialogue_visibility_controller.show_dialogue_line,
            default_speed=self.overlay_config.default_typewriter_speed,
        )
        self.typewriter_controller.active_changed.connect(self.dialogue.set_typing_active)
        self.typewriter_controller.revealed.connect(self.dialogue.show_tail)
        self.typewriter_controller.completed.connect(self.dialogue.show_tail)
        self.audio_controller = AudioController(self)
        # Apply the persisted her-voice volume at startup (default 0.86 == unchanged).
        self.audio_controller.set_chat_volume(self.spica_voice_volume)

        self.input_panel = InputPanel(self)
        self.input_panel.set_opacity(self.dialogue_opacity)
        self.input_panel.send_requested.connect(self.send_message)
        self.input_panel.voice_requested.connect(self.toggle_voice)
        self.input_panel.screenshot_requested.connect(self.toggle_screenshot_selection)
        self.input_panel.stop_requested.connect(self._on_stop_requested)
        self.voice_input_controller = VoiceInputController(
            parent=self,
            set_voice_active=self.input_panel.set_voice_active,
            set_busy=self.set_busy,
            is_conversation_busy=self._is_conversation_busy,
            set_dialogue_text=self.dialogue_visibility_controller.show_system_message,
            on_recognized_text=lambda text: None,
            backend_ready=lambda: self.agent is not None,
        )
        # P3: the mode-agnostic proactive-turn arbiter. busy = conversation
        # (chat/song) OR the user MID-UTTERANCE (never talk over the user). A mic
        # that is merely idle-listening is NOT busy -- the duck gate stops it the
        # instant she speaks, so a galgame reaction can fire in voice mode instead
        # of being perpetually busy_drop'd (option A: preempt idle, never active).
        self.proactive_arbiter = ProactiveTurnArbiter(
            is_busy=self._is_proactive_busy,
            start_turn=self._start_system_turn,
            input_gate=ReactionVoiceDuckGate(self.voice_input_controller),
        )
        # Reaction (worker-thread) system-turn starts hop to the GUI thread via this
        # queued signal. Explicit QueuedConnection + bound method (NOT a lambda)
        # avoids the AutoConnection/closure -> DirectConnection regression documented
        # in galgame_controller._run. Song starts on the GUI thread (direct call).
        self._system_turn_requested.connect(
            self._start_system_turn_gui, Qt.QueuedConnection
        )
        self.song_controller = SongController(
            parent=self,
            chat_stream_controller=None,
            audio_controller=self.audio_controller,
            set_song_status=self._set_song_status,
            set_busy=self.set_busy,
            focus_input=self._focus_input,
            stop_conversation_for_song=lambda: None,
            voice_mode_active_provider=self._is_voice_mode_active,
            schedule_voice_recording=self._schedule_next_voice_recording,
            request_proactive_turn=self.proactive_arbiter.try_speak,
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
        # Voice visualisation seam: intercept the recognized whole sentence at the
        # single stable on_recognized_text indirection point to mirror it into the
        # input box (display-only) BEFORE the unchanged auto-submit. Runs on the GUI
        # thread (recognized is delivered queued from the worker), so it is widget-safe.
        self.voice_input_controller.set_on_recognized_text(self._on_voice_recognized_text)
        self.song_controller.set_stop_conversation_for_song(self.interaction_controller.stop_conversation_for_song)

        self.window_controls = WindowControls(self)
        self.window_controls.settings_requested.connect(self.open_settings_panel)
        self.window_controls.minimize_requested.connect(self.minimize_overlay)
        self.window_controls.close_requested.connect(self.close)
        self.window_controls.companion_requested.connect(self._on_companion_requested)
        self.window_controls.installEventFilter(self)

        # Minimal companion status chip (stage 3): a text label left of the window
        # controls; hidden when idle. Text written ONLY via _set_companion_status.
        self.companion_status_label = QLabel(self)
        self.companion_status_label.setObjectName("companionStatus")
        self.companion_status_label.setStyleSheet(
            "QLabel#companionStatus { background-color: rgba(38, 45, 52, 138);"
            " border: 1px solid rgba(255, 255, 255, 34); border-radius: 12px;"
            " color: #EAF8FF; font-size: 13px; padding: 4px 10px; }"
        )
        self.companion_status_label.hide()

        # B2: song status chip -- the control-feedback surface that replaced the
        # canned typewriter lines (F14: conversational speech only from run_turn;
        # control feedback is UI state). Same styling family as the companion chip,
        # placed to its left in _layout_overlay.
        self.song_status_label = QLabel(self)
        self.song_status_label.setObjectName("songStatus")
        self.song_status_label.setStyleSheet(
            "QLabel#songStatus { background-color: rgba(52, 38, 52, 138);"
            " border: 1px solid rgba(255, 255, 255, 34); border-radius: 12px;"
            " color: #FFEAF8; font-size: 13px; padding: 4px 10px; }"
        )
        self.song_status_label.hide()

        # Phase 4: anime download status chip -- same control-feedback family as
        # the song chip (download progress / done / error; never fake speech).
        self.anime_status_label = QLabel(self)
        self.anime_status_label.setObjectName("animeStatus")
        self.anime_status_label.setStyleSheet(
            "QLabel#animeStatus { background-color: rgba(38, 52, 45, 138);"
            " border: 1px solid rgba(255, 255, 255, 34); border-radius: 12px;"
            " color: #EAFFF3; font-size: 13px; padding: 4px 10px; }"
        )
        self.anime_status_label.hide()

        self.anime_cancel_button = QPushButton("停止下载", self)
        self.anime_cancel_button.setObjectName("animeCancelButton")
        self.anime_cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.anime_cancel_button.setStyleSheet(
            "QPushButton#animeCancelButton { background-color: rgba(73, 42, 45, 190);"
            " border: 1px solid rgba(255, 255, 255, 44); border-radius: 10px;"
            " color: #FFECEF; font-size: 12px; padding: 3px 9px; }"
            "QPushButton#animeCancelButton:hover {"
            " background-color: rgba(105, 49, 55, 210); }"
            "QPushButton#animeCancelButton:disabled {"
            " color: rgba(255, 236, 239, 145); }"
        )
        self.anime_cancel_button.pressed.connect(
            self._on_anime_cancel_pressed)
        self.anime_cancel_button.clicked.connect(
            self._on_anime_cancel_clicked)
        self.anime_cancel_button.hide()

        self.resize_handle = CornerResizeHandle(self)

        self._apply_ui_scale()
        self._init_backend()
        self._load_default_character()
        self._size_to_screen()
        self._start_startup_warmup()
        if loaded_secrets is not None and not loaded_secrets.secrets.openai_api_key:
            QTimer.singleShot(0, self.open_application_settings)

    def _init_backend(self) -> None:
        # Composition root now lives in AppHost.initialize() (Phase 1). The UI no
        # longer constructs services; it reads them back from the host. Qt wiring
        # (chat stream controller, dialogue messages) stays here.
        self.host = AppHost(loaded_secrets=self._loaded_secrets)
        # P0b 2b: the song controller was constructed before the host exists
        # (UI wiring order); hand it the host-resolved song config now.
        self.song_controller.song_config = self.host.song_config
        try:
            self.host.initialize()
            if self.host.dialogue_style is not None:
                from ui.widgets.dialogue_style_art import DialogueStyleArt
                style_art = DialogueStyleArt(self.host.dialogue_style.root, self.host.dialogue_style.style)
                self.dialogue.set_style(style_art)
                self.input_panel.set_style(style_art)
            self.input_panel.input.setPlaceholderText(
                f"对 {self.host.character_package.char_name} 说点什么…"
            )
            self.song_controller.song_config = self.host.song_config
            self.visual_tool = self.host.visual_tool
            self.tts_tool = self.host.tts_tool
            self.tts_adapter = self.host.tts_adapter
            self.agent = self.host.conversation_surface
            # Plan B: inject the resident local STT adapter into the voice loop
            # (delayed -- the controller is built before the host). None when
            # backend=google -> SpeechWorker uses the legacy fallback.
            if self.voice_input_controller is not None:
                self.voice_input_controller.set_stt_port(self.host.stt_adapter)
                # W3: same delayed-wiring shape -- the resolved mic backend
                # string reaches each SpeechWorker via the controller.
                self.voice_input_controller.set_mic_backend(self.host.effective_mic_backend)
            self._init_chat_stream_controller()
            self._init_companion_ui()
            self._init_anime_ui()
            self.interlocutor_name = self.agent.interlocutor_name
            provider_name = str(getattr(self.tts_adapter, "name", None) or self.host.tts_provider)
            self.dialogue_visibility_controller.show_system_message(
                f"LLM API 初始化完成，准备预热 {provider_name}..."
            )
        except Exception as exc:
            # initialize() salvages visual_tool best-effort before re-raising, so
            # the character can still render even when the backend fails.
            self.visual_tool = self.host.visual_tool
            self.dialogue_visibility_controller.show_system_message(
                f"初始化后端失败：{exc}"
            )

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


    def _init_companion_ui(self) -> None:
        """Wire the galgame companion UI (stage 3). MUST run inside __init__ (UI
        events -- the only path that builds the companion controller singleton --
        cannot fire before app.exec(), so the sink is attached strictly BEFORE the
        first companion_controller() construction: structural ordering)."""
        self.companion_bridge = CompanionEventBridge()
        self.host.attach_companion_sink(self.companion_bridge.sink)
        # B2: song events ride the SAME RuntimeEvent bridge (it is a generic
        # event channel despite the name); this slot only reacts to song kinds.
        self.companion_bridge.companion_event.connect(self._on_song_runtime_event)
        # P5: reaction engine wiring -- the arbiter handoff (same shape as song's
        # request_proactive_turn) + the system-turn outcome report channel.
        self.host.attach_reaction_arbiter(self.proactive_arbiter.try_speak)
        if self.chat_stream_controller is not None:
            self.chat_stream_controller.on_system_stream_done = self._on_system_stream_done_for_reaction
        self.galgame_controller = GalgameController(
            self.companion_bridge,
            parent=self,
            host=self.host,
            set_status=self._set_companion_status,
            set_companion_active=self.window_controls.set_companion_active,
            toast=self.dialogue_visibility_controller.show_system_message,
            pick_window=lambda candidates: WindowPickerDialog.pick(candidates, self),
            select_region=self._select_companion_region,
            ask_active_action=self._ask_companion_active_action,
            # window_lost fix: hand GalgameController this overlay's own window id
            # (read at start time) so check_safety's focus exemption fires while
            # typing to her. W1/A3: the id FORMAT is the locator adapter's business
            # (X11 hex / Win32 decimal) -- the native int never leaves this lambda.
            overlay_window_id_provider=lambda: self.host.services.window_locator_adapter.format_native_window_id(
                int(self.winId())
            ),
        )

    def _init_anime_ui(self) -> None:
        """Wire the anime-watch download UI (Phase 4). Own bridge instance (the
        host keeps ``_anime_sink`` off the companion tee); the controller gets
        ONLY host-injected closures -- write authority (library/pending files,
        playback validation) stays on the host (P1-6 / P0-4c). Tolerant of a
        host built before the anime assembly (attrs missing -> skip wiring)."""
        host = self.host
        if not hasattr(host, "anime_register_download"):
            logger.warning("anime assembly not installed; anime UI not wired")
            return
        self.anime_bridge = CompanionEventBridge()
        self.anime_controller = AnimeController(
            self,
            set_anime_status=self._set_anime_status,
            set_anime_cancel_state=self._set_anime_cancel_state,
            request_proactive_turn=self.proactive_arbiter.try_speak,
            play_file=host.anime_play_file,
            register_download=host.anime_register_download,
            mark_played=host.anime_mark_played,
            note_task_id=host.anime_note_task_id,
            list_pending=host.anime_list_pending,
            drop_pending=host.anime_drop_pending,
            is_played=host.anime_is_played,
            is_busy=self._is_proactive_busy,
            galgame_active=self._anime_galgame_active,
            anime_config=lambda: self.host.config.anime,
            torrent_provider=lambda: self.host.anime_torrent,
            download_dir=host.anime_download_dir,
            cookies_file=host.anime_cookies_file,
        )
        self.anime_bridge.companion_event.connect(self._on_anime_runtime_event)
        # sink + F8 in-flight seam attach together: from this point watch_anime
        # is supplied (when enabled) and the busy gate reads real worker state.
        host.attach_anime_sink(self.anime_bridge.sink,
                               in_flight=self.anime_controller.in_flight_state)
        # startup reconcile (P1-9): deferred -- talks to qbt, never in initialize
        self.anime_controller.start_reconcile()

    def _anime_galgame_active(self) -> bool:
        """Read-only companion-active peek for the auto-play gate (P1-7); same
        no-side-effect read as GalgameController._companion_is_active."""
        controller = getattr(self.host, "_companion_controller", None)
        return bool(controller is not None and controller.is_active)

    def _set_anime_status(self, text: str) -> None:
        self.anime_status_label.setText(text)
        self.anime_status_label.setVisible(bool(text))
        self._layout_overlay()

    def _set_anime_cancel_state(
        self,
        active: bool,
        cancelling: bool,
    ) -> None:
        self.anime_cancel_button.setText(
            "停止中…" if cancelling else "停止下载")
        self.anime_cancel_button.setEnabled(bool(active and not cancelling))
        self.anime_cancel_button.setVisible(bool(active))
        self._layout_overlay()

    def _on_anime_cancel_pressed(self) -> None:
        controller = self.anime_controller
        state = controller.in_flight_state() if controller is not None else None
        self._anime_cancel_pressed_request_id = str(
            (state or {}).get("request_id") or "")

    def _on_anime_cancel_clicked(self) -> None:
        expected_request_id = self._anime_cancel_pressed_request_id or ""
        self._anime_cancel_pressed_request_id = None
        if self.anime_controller is not None:
            self.anime_controller.cancel_current_download(expected_request_id)

    def _on_anime_runtime_event(self, event: Any) -> None:
        """Bridge dispatch (Phase 4): the host watch_anime closure emitted an
        AnimeRequestEvent; start the download worker on the UI thread. The
        worker's completion payload (AnimeReadyEvent) stays INSIDE the UI --
        worker Qt signal -> controller -- and never rides this bridge (P2-19)."""
        if self.anime_controller is None:
            return
        kind = getattr(event, "kind", "")
        if kind == "anime_request":
            self.anime_controller.handle_anime_request_event(event)
        elif kind == "anime_cancel_request":
            self.anime_controller.handle_anime_cancel_event(event)

    def _on_companion_requested(self) -> None:
        if self.galgame_controller is None:
            self.dialogue_visibility_controller.show_system_message(
                "后端未初始化，无法开始陪玩。"
            )
            return
        self.galgame_controller.on_companion_clicked()

    def _set_companion_status(self, text: str) -> None:
        self.companion_status_label.setText(text)
        self.companion_status_label.setVisible(bool(text))
        self._layout_overlay()  # re-place the chip + refresh the click-through mask

    def _set_song_status(self, text: str) -> None:
        self.song_status_label.setText(text)
        self.song_status_label.setVisible(bool(text))
        self._layout_overlay()

    def _on_song_runtime_event(self, event: Any) -> None:
        """Bridge dispatch for song events (B2): the host's sing_song closure
        emitted a SongRequestEvent; start the worker on the UI thread."""
        if getattr(event, "kind", "") != "song_request" or self.song_controller is None:
            return
        self.song_controller.handle_song_request_event(
            query=getattr(event, "query", ""),
            title=getattr(event, "title", ""),
            artist=getattr(event, "artist", ""),
        )

    def _ask_companion_active_action(self) -> str | None:
        box = QMessageBox(self)
        box.setWindowTitle("陪玩")
        box.setText("正在陪玩中，要做什么？")
        stop_button = box.addButton("停止陪玩", QMessageBox.ButtonRole.DestructiveRole)
        switch_button = box.addButton("换个游戏陪玩", QMessageBox.ButtonRole.ActionRole)
        recalibrate_button = box.addButton("重新校准对白区域", QMessageBox.ButtonRole.ActionRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is stop_button:
            return "stop"
        if clicked is switch_button:
            return "switch"
        if clicked is recalibrate_button:
            return "recalibrate"
        return None

    def _select_companion_region(self, window_id: str, on_done) -> None:
        """Calibration region selection: the generic rect selector over the screen
        the GAME window is on; the result is converted to physical coords and
        handed back (None = cancelled)."""
        if self.companion_region_selector is not None:
            try:
                self.companion_region_selector.close()
            except Exception:
                pass
            self.companion_region_selector = None
        selector = ScreenshotSelectionOverlay(screen=self._screen_for_window(window_id))
        self.companion_region_selector = selector

        def _finished(payload: dict) -> None:
            self.companion_region_selector = None
            rect = payload.get("logical_rect")
            # W1/L3: per-screen dpr + origin folding (dpr=1 == the old uniform
            # scaling byte-for-byte; goldens pin the equivalence).
            on_done(
                selection_to_physical_screen_rect(
                    (rect.x(), rect.y(), rect.width(), rect.height()),
                    self._screen_geometries(QGuiApplication.screens()),
                )
            )

        def _cancelled(_reason: str) -> None:
            self.companion_region_selector = None
            on_done(None)

        selector.selection_finished.connect(_finished)
        selector.selection_cancelled.connect(_cancelled)
        selector.begin()

    @staticmethod
    def _screen_geometries(screens) -> list[ScreenGeometry]:
        """Collect the screen layout as pure data for the W1 geometry functions
        (L3/L4 seams). The physical rect is derived as logical*dpr per screen --
        exact on X11 (dpr=1); real per-monitor-DPI values are validated in W2."""
        geometries: list[ScreenGeometry] = []
        for screen in screens:
            geometry = screen.geometry()
            dpr = float(screen.devicePixelRatio() or 1.0)
            geometries.append(
                ScreenGeometry(
                    logical=(geometry.x(), geometry.y(), geometry.width(), geometry.height()),
                    physical=(
                        round(geometry.x() * dpr),
                        round(geometry.y() * dpr),
                        round(geometry.width() * dpr),
                        round(geometry.height() * dpr),
                    ),
                    device_pixel_ratio=dpr,
                )
            )
        return geometries

    def _screen_for_window(self, window_id: str):
        try:
            geom = self.host.services.window_locator_adapter.get_window_geometry(window_id)
            if geom is not None:
                # W1/L4: wmctrl geometry is PHYSICAL; QGuiApplication.screenAt
                # expects LOGICAL coords -- match against per-screen PHYSICAL
                # rects instead (identical at dpr=1; goldens pin it).
                screens = QGuiApplication.screens()
                index = physical_point_to_screen_index(
                    (geom.x + geom.width // 2, geom.y + geom.height // 2),
                    self._screen_geometries(screens),
                )
                if index is not None:
                    return screens[index]
        except Exception:  # noqa: BLE001 -- fall back to the primary screen
            pass
        return QGuiApplication.primaryScreen()

    def _start_dangling_recovery(self, _message: str = "") -> None:
        """Startup crash recovery (§12, stage 3): silently 補總結 dangling play
        sessions on a background worker AFTER warmup (serialized startup load).
        Log-only by design -- the ask-user UI stays deferred."""
        if self._restart_requested or self._dangling_recovery_started or self.host is None or self.host.services is None:
            return
        self._dangling_recovery_started = True
        worker = CompanionActionWorker(self.host.recover_dangling_companion_sessions, self)
        self.dangling_recovery_worker = worker
        worker.finished_ok.connect(
            lambda ids: logger.info("galgame dangling sessions recovered: %s", ids)
        )
        worker.failed.connect(
            lambda message: logger.warning("galgame dangling recovery failed: %s", message)
        )
        worker.start()

    def _start_startup_warmup(self) -> None:
        if self.agent is None or self.tts_adapter is None:
            return

        self.startup_warmup_worker = StartupWarmupWorker(self.host, self)
        self.startup_warmup_worker.status_changed.connect(
            self.dialogue_visibility_controller.show_system_message
        )
        self.startup_warmup_worker.finished_ok.connect(
            self.dialogue_visibility_controller.show_system_message
        )
        self.startup_warmup_worker.failed.connect(
            self.dialogue_visibility_controller.show_system_message
        )
        # Dangling-session recovery runs after warmup either way (success or not).
        self.startup_warmup_worker.finished_ok.connect(self._start_dangling_recovery)
        self.startup_warmup_worker.failed.connect(self._start_dangling_recovery)
        self.startup_warmup_worker.start()

    def _size_to_screen(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(760, 620)
            return

        available = screen.availableGeometry().intersected(screen.geometry())
        width = min(scaled_px(824, self.ui_scale), int(available.width() * 0.90))
        base_height = min(scaled_px(720, self.ui_scale), int(available.height() * 0.90))
        height = min(
            available.height(),
            max(MIN_WINDOW_SIZE.height(), int(base_height * self.overlay_initial_height_scale)),
        )
        x = max(available.x(), available.right() + 1 - width - 32)
        y = max(available.y(), available.bottom() + 1 - height - 24)
        self.setGeometry(x, y, width, height)

    def _load_default_character(self) -> None:
        if self.visual_tool is None:
            return

        try:
            config = self.visual_tool.config
            if isinstance(config.get("sprite_map"), dict):
                self.sprite_store = LocalSpriteStore(bundle_root=None)
            self.character_label.set_eye_rigs(config.get("eye_rigs", {}))
            costumes = self.visual_tool.list_costume_sets()
            costume, _mode = self.visual_tool.choose_costume(costumes, config=config)
            self.available_costumes = costumes
            self.selected_costume = costume
            self._set_default_character_for_costume(costume)

            dialog = config.get("dialog", {})
            self.dialogue.speaker_label.setText(str(dialog.get("speaker") or "Spica"))
        except Exception as exc:
            self.dialogue_visibility_controller.show_system_message(
                f"载入差分失败：{exc}"
            )

    def _set_default_character_for_costume(
        self, costume: str | None, *, animated_only: bool = False,
    ) -> None:
        if self.visual_tool is None or not costume:
            return

        config = self.visual_tool.config
        character = config.get("character", {})
        expression_id = str(character.get("default_expression_id") or "000").zfill(3)
        hand_pose = self.visual_tool.normalize_hand_pose(character.get("default_hand_pose") or "normal")
        image_path = self.visual_tool.resolve_expression_image(costume, hand_pose, expression_id)
        if image_path and (
            not animated_only or str(image_path.absolute()) in config.get("eye_rigs", {})
        ):
            self.set_character_image(image_path)

    def _restore_animated_idle_character(self) -> None:
        rigs = getattr(self.visual_tool, "config", {}).get("eye_rigs", {})
        if not rigs or self.current_pixmap_cache_key in rigs:
            return
        # Playback may have started another turn since the terminal callback.
        # Keep the director's image while a reply is playing or awaiting a unit.
        controller = self.chat_stream_controller
        if controller is not None and (controller.streaming_mode or controller.playback_active):
            return
        self._set_default_character_for_costume(self.selected_costume, animated_only=True)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._clear_scaled_pixmap_cache("resize")
        self._layout_overlay()

    def event(self, event) -> bool:
        handled = super().event(event)
        if event.type() == QEvent.Type.DevicePixelRatioChange and hasattr(self, "resize_handle"):
            # Windows can change monitor density without a logical resize.
            # Reuse the DPR-keyed raster cache and preserve sprite/eye identity.
            self._layout_overlay()
        return handled

    def _visual_scale(self) -> float:
        # Keep targets legible but fitted when the window is resized independently
        # of the saved UI preference (small displays / restored geometry).
        return min(self.ui_scale, max(0.65, self.width() / 824), max(0.65, self.height() / 538))

    def _layout_overlay(self) -> None:
        width, height = self.width(), self.height()
        scale = self._visual_scale()
        if scale != self._applied_visual_scale:
            self._applied_visual_scale = scale
            self.typewriter_controller.set_scale(scale)
            self.dialogue.apply_scale(scale)
            self.input_panel.apply_scale(scale)
            self.window_controls.apply_scale(scale)
            self.resize_handle.apply_scale(scale)
            if self.settings_panel is not None:
                self.settings_panel.apply_scale(scale)

        horizontal_margin = scaled_px(12, scale)
        frame_width = min(width - horizontal_margin * 2, scaled_px(800, scale))
        frame_x = (width - frame_width) // 2
        frame_right = frame_x + frame_width
        # Keep the character's available height independent of UI text scale.
        bottom_margin = max(8, round(height / 90))
        input_height = scaled_px(67, scale)
        input_y = height - bottom_margin - input_height
        self.input_panel.setGeometry(frame_x, input_y, frame_width, input_height)
        dialogue_height = scaled_px(153, scale)
        dialogue_y = input_y - dialogue_height
        # Header controls leave the full, stable reading width available.
        # The typewriter must not change line lengths.
        margins = self.dialogue.layout().contentsMargins()
        self.dialogue.text_label.setFixedWidth(max(1, frame_width - margins.left() - margins.right()))
        self.dialogue.setGeometry(frame_x, dialogue_y, frame_width, dialogue_height)
        self.dialogue.layout().activate()

        controls_width = self.window_controls.sizeHint().width()
        controls_height = self.window_controls.sizeHint().height()
        self.window_controls.setGeometry(
            frame_right - controls_width - scaled_px(90, scale),
            dialogue_y + scaled_px(21, scale), controls_width, controls_height,
        )

        # Real operation status stays outside the dialogue; no fake spoken lines.
        chip_height = scaled_px(28, scale)
        chip_y = max(0, dialogue_y - chip_height - scaled_px(10, scale))
        chip_right_edge = frame_right
        for chip, fraction in (
            (self.anime_cancel_button, 0.25),
            (self.anime_status_label, 0.4),
            (self.song_status_label, 0.4),
            (self.companion_status_label, 0.5),
        ):
            if chip.isVisible():
                available = max(0, chip_right_edge - frame_x)
                chip_width = min(chip.sizeHint().width(), max(120, int(width * fraction)), available)
                chip.setGeometry(chip_right_edge - chip_width, chip_y, chip_width, chip_height)
                chip.raise_()
                chip_right_edge -= chip_width + scaled_px(8, scale)

        # Both the sprite and its shadow terminate at the shared frame bottom.
        character_bottom = input_y + input_height
        # Character size remains independent of the dialogue/control scale.
        raw_character_height = int(height * (516 / 720) * self.character_scale * self.character_label_height_scale)
        max_character_height = min(character_bottom, int(height * self.character_max_height_ratio))
        character_height = min(max_character_height, max(1, raw_character_height))
        character_width = min(self._character_width_for_height(character_height), int(width * 0.94))
        character_center = frame_x + frame_width // 2
        character_x = max(0, min(width - character_width, character_center - character_width // 2))
        character_y = character_bottom - character_height
        self.character_label.setGeometry(character_x, character_y, character_width, character_height)
        self._log_overlay_layout_config()
        self._rescale_character()
        self.character_label.lower()
        self.dialogue.raise_()
        self.input_panel.raise_()

        if self.settings_panel is not None:
            self.settings_panel.stop_motion_for_layout()
        if self.settings_panel is not None and self.settings_panel.isVisible():
            panel_width = min(scaled_px(360, scale), frame_width - controls_width - scaled_px(28, scale))
            top_margin = scaled_px(14, scale)
            panel_bottom = input_y - scaled_px(10, scale)
            panel_height = min(scaled_px(560, scale), panel_bottom - top_margin)
            self.settings_panel.setGeometry(
                frame_x + scaled_px(12, scale),
                panel_bottom - panel_height, panel_width, panel_height,
            )
            self.settings_panel.raise_()

        handle_size = self.resize_handle.width()
        self.resize_handle.setGeometry(
            min(width - handle_size, frame_right + scaled_px(3, scale)),
            character_bottom - handle_size, handle_size, handle_size,
        )
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
        logger.debug(  # layout tracing is profiling material, not user-facing
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

    def _scaled_pixmap_cache_key(self) -> tuple[Any, ...] | None:
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
            self.devicePixelRatioF(),
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
                self.character_label.set_sprite(
                    cached_scaled, self.current_pixmap_cache_key
                )
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
        dpr = self.devicePixelRatioF()
        scaled = self.current_pixmap.scaled(
            self.character_label.size() * dpr,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(dpr)
        self._log_character_image_event(
            "pixmap_scale_done",
            duration_ms=self._duration_ms(scale_started_at_ms),
            scaled_size=f"{scaled.width()}x{scaled.height()}",
        )
        if scaled_cache_key is not None and not scaled.isNull():
            self.scaled_pixmap_cache.put(scaled_cache_key, scaled)
        label_started_at_ms = self._now_ms()
        self._log_character_image_event("label_update_start")
        self.character_label.set_sprite(scaled, self.current_pixmap_cache_key)
        self._log_character_image_event(
            "label_update_done",
            duration_ms=self._duration_ms(label_started_at_ms),
        )

    def set_character_image(self, path: str | Path | None) -> None:
        if not path:
            return
        started_at_ms = self._now_ms()
        cache_key = str(Path(path).absolute())
        self._log_character_image_event("set_character_image_start", path=cache_key)
        load_started_at_ms = self._now_ms()
        self._log_character_image_event("image_load_start", path=cache_key)
        resolved = self.sprite_store.resolve(path, identity=cache_key)
        pixmap = resolved.pixmap if resolved is not None else QPixmap()
        self._log_character_image_event(
            "image_load_done",
            path=cache_key,
            duration_ms=self._duration_ms(load_started_at_ms),
            is_null=pixmap.isNull(),
            layered=resolved.layered if resolved is not None else False,
        )
        cache_event = "cache_hit" if resolved is not None and resolved.cache_hit else "cache_miss"
        self._log_character_image_event(
            cache_event,
            path=cache_key,
            cache_size=self.sprite_store.entry_count,
        )
        self._log_character_image_event(
            f"raw_{cache_event}",
            path=cache_key,
            cache_size=self.sprite_store.entry_count,
        )
        if pixmap.isNull():
            duration_ms = self._duration_ms(started_at_ms)
            self._log_character_image_event("set_character_image_done", path=cache_key, duration_ms=duration_ms, loaded=False)
            if duration_ms > 100:
                logger.warning("event=set_character_image_slow monotonic_ms=%s path=%r duration_ms=%s", self._now_ms(), cache_key, duration_ms)
            return
        assert resolved is not None
        if self.current_pixmap_cache_key != resolved.cache_key or self.current_pixmap is None:
            # The native game PNG has substantial transparent padding. Trim in
            # Qt once per source change so the visible character stays centered.
            bounds = QRegion(pixmap.mask()).boundingRect()
            self.current_pixmap = pixmap.copy(bounds) if not bounds.isEmpty() else pixmap
        self.current_pixmap_cache_key = resolved.cache_key
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
        self._applied_visual_scale = None
        self._layout_overlay()

    def send_message(self) -> None:
        message = self.input_panel.input.text().strip()
        if not message and self.pending_screen_attachment is None:
            self.input_panel.input.setFocus()
            return

        self.input_panel.input.clear()
        # Give the new typed turn priority. Anime completion intent survives;
        # its controller waits until the aggregate conversation becomes idle.
        if self.anime_controller is not None:
            self.anime_controller.notify_user_activity()
        if self.interaction_controller is not None:
            self.interaction_controller.handle_user_text(message)

    def _on_voice_recognized_text(self, text: str) -> None:
        """Voice-mode visualisation (display-only): mirror the recognized whole
        sentence into the input box, then hand off to the UNCHANGED auto-submit path.

        Wired in place of ``interaction_controller.handle_user_text`` at the single
        ``on_recognized_text`` indirection point (set in __init__). ``handle_user_text``
        is ALWAYS called with the same ``text`` -- the preview is purely additive and
        never alters submit semantics (hard constraint #1). ``text`` arrives already
        stripped and voice-mode-gated from ``VoiceInputController.handle_recognized``.

        The preview is skipped (never clobbered) when the box holds a user draft, and
        only overwrites a stale preview of our own. It is cleared after a brief linger."""
        if self._is_voice_mode_active():
            current = self.input_panel.input.text().strip()
            if not current or current == self._voice_transcript_shown:
                self.input_panel.set_voice_transcript(text)
                self._voice_transcript_shown = text
                QTimer.singleShot(_VOICE_TRANSCRIPT_LINGER_MS, self._clear_voice_transcript)
        # Same priority handoff for voice input; completion intent is retained.
        if self.anime_controller is not None:
            self.anime_controller.notify_user_activity()
        if self.interaction_controller is not None:
            self.interaction_controller.handle_user_text(text)

    def _clear_voice_transcript(self) -> None:
        """Clear the lingering voice preview -- but ONLY if the box still holds the
        exact sentence we wrote, so a user draft or a newer recognized sentence typed
        in the meantime is never wiped. Defensive no-op if the widget is already gone
        (window closed before the linger fired)."""
        shown = self._voice_transcript_shown
        self._voice_transcript_shown = None
        if shown is None:
            return
        try:
            if self.input_panel.input.text() == shown:
                self.input_panel.clear_voice_transcript()
        except RuntimeError:
            pass  # underlying QLineEdit already destroyed

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
        self.dialogue_visibility_controller.show_system_message(
            "拖拽选择要让 Spica 查看的一块区域，按 Esc 取消。"
        )
        self.screenshot_selector.begin()

    def _handle_screenshot_selection_finished(self, payload: dict[str, Any]) -> None:
        self.screenshot_selector = None
        QTimer.singleShot(120, lambda data=dict(payload): self._capture_selected_region(data))

    def _handle_screenshot_selection_cancelled(self, reason: str) -> None:
        self.screenshot_selector = None
        self.input_panel.set_screenshot_pending(False)
        self.pending_screen_attachment = None
        if reason == "截图区域太小":
            self.dialogue_visibility_controller.show_system_message(
                "截图区域太小"
            )
        else:
            self.dialogue_visibility_controller.show_system_message(
                "已取消截图。"
            )

    def _capture_selected_region(self, payload: dict[str, Any]) -> None:
        self._start_screenshot_worker(payload)

    def _start_screenshot_worker(self, payload: dict[str, Any]) -> None:
        if self.screenshot_worker is not None and self.screenshot_worker.isRunning():
            self.dialogue_visibility_controller.show_system_message(
                "正在处理截图..."
            )
            return

        self.pending_screen_attachment = None
        self.input_panel.set_screenshot_pending(False)
        self.input_panel.screenshot_button.setEnabled(False)
        self.dialogue_visibility_controller.show_system_message(
            "正在处理截图..."
        )

        worker = ScreenshotWorker(
            payload, config=self.host.screen_config if self.host else None
        )
        self.screenshot_worker = worker
        worker.finished_ok.connect(self._handle_screenshot_worker_done)
        worker.failed.connect(self._handle_screenshot_worker_failed)
        worker.finished.connect(self._handle_screenshot_worker_finished)
        worker.start()

    def _handle_screenshot_worker_done(self, attachment: dict[str, Any]) -> None:
        self.pending_screen_attachment = attachment
        self.input_panel.screenshot_button.setEnabled(True)
        self.input_panel.set_screenshot_pending(True)
        self.dialogue_visibility_controller.show_system_message(
            "截图已准备好。输入问题后发送，或直接发送让我概括。"
        )
        self._focus_input()

    def _handle_screenshot_worker_failed(self, message: str) -> None:
        self.pending_screen_attachment = None
        self.input_panel.screenshot_button.setEnabled(True)
        self.input_panel.set_screenshot_pending(False)
        self.dialogue_visibility_controller.show_system_message(
            f"截图失败：{message}"
        )

    def _handle_screenshot_worker_finished(self) -> None:
        worker = self.screenshot_worker
        self.screenshot_worker = None
        if worker is not None:
            worker.deleteLater()

    def clear_pending_screenshot(self, show_message: bool = False) -> None:
        self.pending_screen_attachment = None
        self.input_panel.set_screenshot_pending(False)
        if show_message:
            self.dialogue_visibility_controller.show_system_message(
                "已取消待发送截图。"
            )

    def consume_pending_screenshot(self) -> dict[str, Any] | None:
        attachment = self.pending_screen_attachment
        self.pending_screen_attachment = None
        self.input_panel.set_screenshot_pending(False)
        return attachment

    def _is_song_busy(self) -> bool:
        return bool(self.song_controller is not None and self.song_controller.is_busy())

    def _focus_input(self) -> None:
        # A completed reply must not take focus from a setting or another app.
        if not self.isActiveWindow():
            return
        if self.settings_panel is not None and self.settings_panel.isVisible():
            return
        focused = QApplication.focusWidget()
        if focused is not None and focused not in (self, self.input_panel.input):
            return
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
        self.dialogue_visibility_controller.show_system_message(
            f"请求失败：{message}"
        )
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

    def _is_recording(self) -> bool:
        """A user voice segment is in flight (a SpeechWorker is running, idle OR
        mid-utterance). Drives input-lock A (the text box stays disabled while the
        mic owns the turn). The P3 arbiter NO LONGER shares this -- it gates on the
        narrower ``_is_user_speaking`` so an idle mic doesn't block reactions."""
        worker = (
            self.voice_input_controller.speech_worker
            if self.voice_input_controller is not None
            else None
        )
        return bool(worker is not None and worker.isRunning())

    def _is_user_speaking(self) -> bool:
        """The user is ACTIVELY mid-utterance (hardware VAD has detected speech),
        as opposed to the mic merely idle-listening. The P3 arbiter's busy truth:
        a reaction may fire during idle-listen gaps (the duck gate stops the mic
        when she speaks) but never over a half-spoken sentence."""
        return bool(
            self.voice_input_controller is not None
            and self.voice_input_controller.is_capturing_user_speech()
        )

    def _is_proactive_busy(self) -> bool:
        """P3 arbiter busy truth: conversation busy (chat/song) OR the user
        mid-utterance -- NOT a merely idle-listening mic (else reactions are
        perpetually busy_drop'd in voice mode)."""
        return self._restart_requested or self._is_conversation_busy() or self._is_user_speaking()

    def _start_system_turn(self, request: Any) -> None:
        """P3 arbiter start callback. Invoked EITHER on the GUI thread (song, via
        QMediaPlayer.mediaStatusChanged) OR on the reaction-engine WORKER thread
        (galgame reaction). All the Qt widget/QTimer/QThread work lives in
        _start_system_turn_gui and MUST run on the GUI thread, so a worker-thread
        call is marshalled there via a queued signal -- driving Qt from the worker
        thread was the 2026-06-26 libQt6Gui general-protection-fault. The hop is
        non-blocking on purpose: a BlockingQueued hop to our OWN thread (the song
        path) would deadlock, and try_speak's bool ("started" == "not busy") never
        depended on the turn having actually started, so the async hop changes no
        accounting -- _in_flight is reconciled by notify_turn_finished as before."""
        if QThread.currentThread() is self.thread():
            self._start_system_turn_gui(request)
        else:
            self._system_turn_requested.emit(request)

    def _start_system_turn_gui(self, request: Any) -> None:
        """The real system-turn launch -- ALWAYS on the GUI thread (direct call
        from _start_system_turn when already on the GUI thread, or via the queued
        _system_turn_requested signal when started from a worker thread)."""
        if self._restart_requested or self.chat_stream_controller is None:
            self.proactive_arbiter.system_speech_finished()
            self._reaction_stream_closed()
            return
        self.chat_stream_controller.start_system_turn(request)
        self.chat_stream_controller.notify_on_current_stream_done(
            self.proactive_arbiter.system_speech_finished
        )
        # P5: a stopped/errored system stream never reaches stream_done -- this
        # one-shot clears the reaction engine's in-flight slot (no-op when the
        # real report already arrived, or for non-reaction turns like song).
        self.chat_stream_controller.notify_on_current_stream_done(
            self._reaction_stream_closed
        )

    def _on_system_stream_done_for_reaction(self, answer: str) -> None:
        """P5 report channel: forward the system turn's outcome to the reaction
        engine (beat recording + NO_COMMENT refund). Ignored by the engine when
        it has nothing in flight (e.g. a song report turn)."""
        engine = getattr(self.host, "reaction_engine", None)
        if engine is not None:
            engine.notify_turn_finished(answer, silent=(answer == NO_COMMENT_SENTINEL))

    def _reaction_stream_closed(self) -> None:
        engine = getattr(self.host, "reaction_engine", None)
        if engine is not None:
            engine.notify_turn_finished("", silent=False)

    def _on_stop_requested(self) -> None:
        """B: the cross-mode stop affordance -- a PURE stop (no message). Rides the
        same stop_current as a new turn, so #1's worker.cancel halts the backend
        producer cleanly (tool / memory / LLM-delta checkpoints). Because stop_current
        does NOT call on_chat_done, voice mode resumes mic monitoring here, mirroring
        _handle_chat_stream_done. The stop itself touches no microphone/recording
        state (no VAD, no SpeechWorker) -- the resume is the standard turn-done rearm."""
        self.dialogue.hide_tail()
        if self.chat_stream_controller is not None:
            self.chat_stream_controller.stop_current()
        if self._is_voice_mode_active():
            self._schedule_next_voice_recording(320)

    def set_busy(self, busy: bool) -> None:
        if not busy:
            # All playback terminal paths (done, stop and error) reach this seam.
            # Defer past state cleanup and any immediate replacement user turn.
            QTimer.singleShot(0, self._restore_animated_idle_character)
        if self.settings_panel is not None:
            self.settings_panel.set_costume_enabled(not self._is_conversation_busy())
        # B: stop button visible iff a chat/reaction turn is in flight. Driven by the
        # chat-stream busy truth -- cross-mode, independent of voice/text AND of the
        # `busy` arg (which is also True during a mic recording segment, when there is
        # no turn to stop). Set first so it applies on every busy transition.
        turn_active = self.chat_stream_controller is not None and self.chat_stream_controller.is_busy()
        self.input_panel.set_turn_active(turn_active)
        if self._is_song_busy():
            self.input_panel.set_busy(False, voice_enabled=True)
            if self.screenshot_worker is not None and self.screenshot_worker.isRunning():
                self.input_panel.screenshot_button.setEnabled(False)
            return
        self.input_panel.set_busy(
            busy,
            voice_enabled=(not busy or self._is_voice_mode_active()),
            # A: typeable while she speaks (turn active); locked only while a mic
            # segment is in flight AND voice mode is on, so a send cannot race the
            # segment into a double turn. The voice-mode guard is essential: toggling
            # voice OFF leaves the in-flight worker briefly running, but stop() bumps
            # voice_session_id so its finished->handle_finished early-returns without
            # re-enabling the box; without this guard the text input stayed permanently
            # disabled after a voice on/off toggle. With voice off a lingering worker's
            # result is discarded (session-id / voice_mode_active gated) -- no double
            # turn to guard -- so the box must stay usable.
            input_enabled=not (self._is_recording() and self._is_voice_mode_active()),
        )
        if self.screenshot_worker is not None and self.screenshot_worker.isRunning():
            self.input_panel.screenshot_button.setEnabled(False)

    def open_settings_panel(self) -> None:
        if self.settings_panel is not None and self.settings_panel.isVisible():
            self._close_settings_panel()
            return
        if self.settings_panel is None:
            self.settings_panel = SettingsPanel(self)
            self.character_settings_controller = CharacterSettingsController(self, self.settings_panel)
            self.application_settings_controller = ApplicationSettingsController(self, self.settings_panel)
            self.settings_panel.close_requested.connect(self._close_settings_panel)
            self.settings_panel.exit_requested.connect(self.close)
            self.settings_panel.restart_requested.connect(self.restart_application)
            self.settings_panel.opacity_changed.connect(self.set_dialogue_opacity)
            self.settings_panel.opacity_commit_requested.connect(self.persist_dialogue_opacity)
            self.settings_panel.motion_animation.finished.connect(self._update_click_through_mask)
            self.settings_panel.costume_changed.connect(self.set_costume)
            self.settings_panel.scale_changed.connect(self.set_character_scale)
            self.settings_panel.overall_scale_changed.connect(self.set_overall_scale)
            self.settings_panel.typing_speed_changed.connect(self.set_typewriter_speed)
            self.settings_panel.voice_volume_changed.connect(self.set_spica_voice_volume)
            self.settings_panel.voice_volume_commit_requested.connect(
                self.persist_spica_voice_volume
            )
            self.settings_panel.dialogue_visibility_changed.connect(
                self.set_dialogue_box_visible
            )
            self.settings_panel.apply_scale(self._visual_scale())
            self.settings_panel.hide()

        if self.visual_tool is not None:
            self.available_costumes = self.visual_tool.list_costume_sets()
        self.settings_panel.set_costumes(self.available_costumes, self.selected_costume)
        self.settings_panel.set_costume_enabled(not self._is_conversation_busy())
        self.character_settings_controller.refresh()
        self.application_settings_controller.refresh()
        self.settings_panel.set_scale(self.character_scale)
        self.settings_panel.set_overall_scale(self.ui_scale)
        self.settings_panel.set_typing_speed(self.typewriter_controller.typewriter_speed)
        self.settings_panel.set_voice_volume(self.spica_voice_volume)
        self.settings_panel.set_opacity(self.dialogue_opacity)
        self.settings_panel.set_dialogue_box_visible(
            self.dialogue_box_visible
        )
        self.settings_panel.setVisible(True)
        self._layout_overlay()
        self.settings_panel.play_open_motion()
        self._update_click_through_mask()
        self.settings_panel.close_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def open_application_settings(self) -> None:
        if self.settings_panel is None or not self.settings_panel.isVisible():
            self.open_settings_panel()
        self.settings_panel.tabs.setCurrentWidget(self.settings_panel.application_page)

    def restart_application(self) -> None:
        if self._restart_requested or self._forced_close_armed:
            return
        controller = getattr(self, "character_settings_controller", None)
        if controller is not None and controller.worker is not None:
            return  # Finish the package/config write before stopping its owner.
        if self.settings_panel is not None:
            QApplication.inputMethod().commit()
        application = getattr(self, "application_settings_controller", None)
        if application is not None and not application.prepare_restart():
            return
        panel = self.settings_panel
        if panel is not None:
            if controller is not None and not controller.save_interlocutor_name(panel.name_input.text()):
                return
            panel.name_input.clearFocus()
            panel.restart_button.setEnabled(False)
            panel.restart_button.setText("正在重启…")
        self._restart_requested = True
        self.input_panel.setEnabled(False)
        if panel is not None:
            panel.setEnabled(False)
        # Keep the request while live owners unwind. closeEvent retries their
        # existing shutdown; only main(), after a clean close, may relaunch.
        self.close()

    def _close_settings_panel(self) -> None:
        if self.settings_panel is not None:
            self.settings_panel.name_input.clearFocus()
            self.settings_panel.play_close_motion(on_hidden=self._update_click_through_mask)
            self._update_click_through_mask()

    def minimize_overlay(self) -> None:
        self.showMinimized()

    def set_costume(self, costume: str) -> None:
        if self._is_conversation_busy():
            if self.settings_panel is not None:
                self.settings_panel.set_costumes(self.available_costumes, self.selected_costume)
            return
        costume = (costume or "").strip()
        visual_tool = self.visual_tool
        if not costume or visual_tool is None:
            return
        try:
            canonical = str(visual_tool.set_costume(costume))
        except Exception as exc:
            logger.warning(
                "event=desktop_costume_write_failed error_type=%s",
                type(exc).__name__,
            )
            return
        self._apply_costume_selection(canonical)

    def _apply_costume_selection(self, costume: str) -> None:
        canonical = str(costume or "").strip()
        if not canonical:
            return
        self.selected_costume = canonical
        if self.settings_panel is not None:
            self.settings_panel.set_costumes(
                self.available_costumes,
                canonical,
            )
        self._set_default_character_for_costume(canonical)

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

    def set_spica_voice_volume(self, volume: float) -> None:
        """Apply her-voice volume live and schedule bounded persistence."""
        self.spica_voice_volume = max(0.0, min(1.0, float(volume)))
        self.audio_controller.set_chat_volume(self.spica_voice_volume)
        self._voice_volume_save_timer.start()

    def persist_spica_voice_volume(self) -> None:
        """Persist at edit completion or after the bounded debounce."""
        self._voice_volume_save_timer.stop()
        save_overlay_config_value("spica_voice_volume", self.spica_voice_volume)

    def set_dialogue_opacity(self, opacity: float) -> None:
        self.dialogue_opacity = max(MIN_DIALOGUE_OPACITY, min(MAX_DIALOGUE_OPACITY, float(opacity)))
        self.dialogue.set_opacity(self.dialogue_opacity)
        self.input_panel.set_opacity(self.dialogue_opacity)
        self._update_click_through_mask()
        self._opacity_save_timer.start()

    def persist_dialogue_opacity(self) -> None:
        self._opacity_save_timer.stop()
        save_dialogue_opacity(self.dialogue_opacity)

    def set_dialogue_box_visible(self, visible: bool) -> None:
        """Apply and persist the UI-only preference immediately."""

        self.dialogue_box_visible = bool(visible)
        self.dialogue_visibility_controller.set_user_hidden(
            not self.dialogue_box_visible
        )
        if self.settings_panel is not None:
            self.settings_panel.set_dialogue_box_visible(
                self.dialogue_box_visible
            )
        save_dialogue_box_visible(self.dialogue_box_visible)

    def _start_corner_resize(self, event: QMouseEvent) -> None:
        self.drag_offset = None
        self._drag_press_pos = None
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

        region = self._character_hit_region()
        for widget, margin in (
            (self.dialogue, 1),
            (self.input_panel, 1),
            (self.window_controls, 2),
            (self.companion_status_label, 1),  # setMask clips RENDERING too -- must be in
            (self.song_status_label, 1),
            (self.anime_status_label, 1),
            (self.anime_cancel_button, 1),
            (self.settings_panel, 1),
            (self.resize_handle, 2),
        ):
            region = region.united(self._widget_hit_region(widget, margin))

        if region.isEmpty():
            self.clearMask()
            return
        # The shared bottom edge clips the sprite's legacy drop shadow as well.
        frame_bounds = QRect(0, 0, self.width(), self.input_panel.geometry().bottom() + 1)
        self.setMask(region.intersected(QRegion(self.rect().intersected(frame_bounds))))

    def _controls_drag_rect(self) -> QRect:
        if self.dialogue.isHidden():
            return QRect()
        return self.dialogue.drag_rect().translated(self.dialogue.pos())

    def _widget_hit_region(self, widget: QWidget | None, margin: int = 0) -> QRegion:
        if widget is None or widget.isHidden():
            return QRegion()
        if widget is self.dialogue:
            return self.dialogue.hit_region().translated(self.dialogue.pos())
        if widget is self.input_panel:
            return self.input_panel.hit_region().translated(self.input_panel.pos())
        rect = widget.geometry().adjusted(-margin, -margin, margin, margin).intersected(self.rect())
        if widget is self.settings_panel and self.settings_panel.motion_animation.state() != QAbstractAnimation.State.Stopped:
            # Reserve the small slide travel during the fade, then remove it.
            rect = rect.adjusted(-PANEL_SLIDE_PX, 0, PANEL_SLIDE_PX, 0).intersected(self.rect())
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

        key = (pixmap.cacheKey(), pixmap_rect.x(), pixmap_rect.y())
        if key != self._character_region_key:
            image = pixmap.toImage().scaled(pixmap_rect.size(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            image = image.convertToFormat(QImage.Format.Format_ARGB32)
            image.setDevicePixelRatio(1.0)
            self._character_region = self._alpha_hit_region(image, pixmap_rect.topLeft())
            self._character_region_key = key
        region = self._character_region
        if region.isEmpty():
            return QRegion(pixmap_rect.intersected(self.rect()))
        return region.intersected(QRegion(self.rect()))

    def _character_pixmap_rect(self, pixmap: QPixmap) -> QRect:
        label_rect = self.character_label.geometry()
        logical_size = pixmap.deviceIndependentSize().toSize()
        pixmap_width = logical_size.width()
        pixmap_height = logical_size.height()
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
        # QImage.createAlphaMask uses a fixed 128 alpha cutoff. Drawing the
        # source 15 times with additive composition maps our historical
        # ``alpha > 8`` rule exactly onto that cutoff: 8*15=120 stays out,
        # 9*15=135 enters. All pixel traversal and run construction then stay
        # inside Qt/C++ instead of blocking the GUI thread in Python.
        amplified = QImage(
            image.size(),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        amplified.fill(Qt.GlobalColor.transparent)
        painter = QPainter(amplified)
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_Plus
        )
        for _ in range(15):
            painter.drawImage(0, 0, image)
        painter.end()

        opaque = QRegion(QBitmap.fromImage(amplified.createAlphaMask()))
        if opaque.isEmpty():
            return QRegion()

        horizontal = QRegion()
        for dx in range(-margin, margin + 1):
            horizontal = horizontal.united(opaque.translated(dx, 0))
        expanded = QRegion()
        for dy in range(-margin, margin + 1):
            expanded = expanded.united(horizontal.translated(0, dy))
        clipped = expanded.intersected(QRegion(QRect(0, 0, width, height)))
        return clipped.translated(origin)

    def eventFilter(self, watched: QObject, event) -> bool:  # noqa: N802 - Qt override
        dialogue = getattr(self, "dialogue", None)
        if dialogue is not None and watched in (dialogue, dialogue.speaker_label, self.character_label):
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._start_drag(event)
                watched.setCursor(Qt.CursorShape.ClosedHandCursor)
                return True
            if event.type() == QEvent.Type.MouseMove and self._drag_press_pos is not None:
                self._drag_to(event)
                return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                self.drag_offset = None
                self._drag_press_pos = None
                watched.setCursor(Qt.CursorShape.OpenHandCursor)
                return True
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton and self._controls_drag_rect().contains(event.position().toPoint()):
            self._start_drag(event)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if self._drag_press_pos is not None:
            self._drag_to(event)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        self.drag_offset = None
        self._drag_press_pos = None
        super().mouseReleaseEvent(event)

    def _start_drag(self, event: QMouseEvent) -> None:
        self._drag_press_pos = event.globalPosition().toPoint()
        self.drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def _drag_to(self, event: QMouseEvent) -> None:
        if self._drag_press_pos is None or self.drag_offset is None:
            return
        point = event.globalPosition().toPoint()
        if (point - self._drag_press_pos).manhattanLength() < QApplication.startDragDistance():
            return
        target = point - self.drag_offset
        screen = QGuiApplication.screenAt(point) or self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            target.setX(max(available.left(), min(target.x(), max(available.left(), available.right() + 1 - self.width()))))
            target.setY(max(available.top(), min(target.y(), max(available.top(), available.bottom() + 1 - self.height()))))
        self.move(target)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        application = getattr(self, "application_settings_controller", None)
        if application is not None and application.worker is not None and application._operation in {"save", "secret"}:
            application.page.status.setText("正在保存，请稍候再退出。")
            event.ignore()
            return
        if self._forced_close_armed:
            owners = ",".join(self._forced_close_owners) or "unknown"
            armed_at = self._forced_close_armed_at or 0.0
            _log_desktop_close_error(
                "event=desktop_force_exit owners=%s armed_monotonic=%.6f "
                "elapsed=%.6f",
                owners,
                armed_at,
                max(0.0, time.monotonic() - armed_at),
            )
            event.accept()
            _force_process_exit(1)
            return

        # The first close retains its existing grace period. Later restart
        # attempts only poll, so a slow cancelled worker cannot freeze Qt.
        shutdown_deadline = time.monotonic() + (0.0 if self._restart_timer.isActive() else 1.5)
        nonclean_owners: list[str] = []
        if self._voice_volume_save_timer.isActive():
            try:
                self.persist_spica_voice_volume()
            except Exception as exc:
                nonclean_owners.append("overlay_config")
                _log_desktop_close_error(
                    "event=desktop_close_rejected "
                    "reason=overlay_config_persist_exception exception_type=%s",
                    type(exc).__name__,
                )
        if self._opacity_save_timer.isActive():
            try:
                self.persist_dialogue_opacity()
            except Exception as exc:
                nonclean_owners.append("overlay_config")
                _log_desktop_close_error(
                    "event=desktop_close_rejected "
                    "reason=overlay_config_persist_exception exception_type=%s",
                    type(exc).__name__,
                )
        try:
            self.typewriter_controller.stop()
        except Exception as exc:
            nonclean_owners.append("typewriter")
            _log_desktop_close_error(
                "event=desktop_close_rejected "
                "reason=typewriter_stop_exception exception_type=%s",
                type(exc).__name__,
            )
        try:
            self.audio_controller.stop_all()
        except Exception as exc:
            nonclean_owners.append("audio")
            _log_desktop_close_error(
                "event=desktop_close_rejected "
                "reason=audio_stop_exception exception_type=%s",
                type(exc).__name__,
            )
        if self.chat_stream_controller is not None:
            remaining_ms = max(
                0,
                int((shutdown_deadline - time.monotonic()) * 1000),
            )
            try:
                chat_stopped = self.chat_stream_controller.shutdown(remaining_ms)
            except Exception as exc:
                chat_stopped = None
                nonclean_owners.append("chat")
                _log_desktop_close_error(
                    "event=desktop_close_rejected "
                    "reason=chat_qthread_shutdown_exception exception_type=%s",
                    type(exc).__name__,
                )
            if chat_stopped is False:
                nonclean_owners.append("chat")
                _log_desktop_close_error(
                    "event=desktop_close_rejected reason=chat_qthread_timeout"
                )
        if self.galgame_controller is not None:
            try:
                galgame_stopped = self.galgame_controller.shutdown(
                    deadline=shutdown_deadline
                )
            except Exception as exc:
                galgame_stopped = False
                _log_desktop_close_error(
                    "event=desktop_close_rejected "
                    "reason=galgame_qthread_shutdown_exception exception_type=%s",
                    type(exc).__name__,
                )
            if galgame_stopped is False:
                nonclean_owners.append("galgame")
                _log_desktop_close_error(
                    "event=desktop_close_rejected "
                    "reason=galgame_qthread_timeout"
                )
        if self.companion_region_selector is not None:
            try:
                self.companion_region_selector.close()
            except Exception:
                pass
            self.companion_region_selector = None
        recovery_worker = self.dangling_recovery_worker
        if recovery_worker is not None:
            try:
                recovery_running = recovery_worker.isRunning()
            except Exception as exc:
                recovery_running = True
                _log_desktop_close_error(
                    "event=desktop_close_rejected "
                    "reason=galgame_recovery_qthread_state_exception "
                    "exception_type=%s",
                    type(exc).__name__,
                )
            if recovery_running:
                remaining_ms = max(
                    0,
                    int((shutdown_deadline - time.monotonic()) * 1000),
                )
                try:
                    recovery_stopped = recovery_worker.wait(remaining_ms)
                    recovery_running = recovery_worker.isRunning()
                except Exception as exc:
                    recovery_stopped = False
                    recovery_running = True
                    _log_desktop_close_error(
                        "event=desktop_close_rejected "
                        "reason=galgame_recovery_qthread_shutdown_exception "
                        "exception_type=%s",
                        type(exc).__name__,
                    )
                if recovery_stopped is False or recovery_running:
                    nonclean_owners.append("galgame_recovery")
                    _log_desktop_close_error(
                        "event=desktop_close_rejected "
                        "reason=galgame_recovery_qthread_timeout"
                    )
        for controller, component in (
            (self.song_controller, "song"),
            (self.anime_controller, "anime"),
            (self.voice_input_controller, "voice_input"),
        ):
            if controller is None:
                continue
            remaining_ms = max(
                0,
                int((shutdown_deadline - time.monotonic()) * 1000),
            )
            try:
                stopped = controller.shutdown(remaining_ms)
            except Exception as exc:
                stopped = False
                _log_desktop_close_error(
                    "event=desktop_close_rejected "
                    "reason=%s_qthread_shutdown_exception exception_type=%s",
                    component,
                    type(exc).__name__,
                )
            if stopped is False:
                nonclean_owners.append(component)
                _log_desktop_close_error(
                    "event=desktop_close_rejected reason=%s_qthread_timeout",
                    component,
                )
        if self.screenshot_selector is not None:
            try:
                self.screenshot_selector.close()
            except Exception:
                pass
            self.screenshot_selector = None
        for attribute, component in (
            ("screenshot_worker", "screenshot"),
            ("startup_warmup_worker", "startup_warmup"),
        ):
            worker = getattr(self, attribute, None)
            if worker is None:
                continue
            try:
                running = worker.isRunning()
            except Exception as exc:
                running = True
                _log_desktop_close_error(
                    "event=desktop_close_rejected "
                    "reason=%s_qthread_state_exception exception_type=%s",
                    component,
                    type(exc).__name__,
                )
            if not running:
                if attribute == "screenshot_worker":
                    self.screenshot_worker = None
                continue
            try:
                worker.quit()
                remaining_ms = max(
                    0,
                    int((shutdown_deadline - time.monotonic()) * 1000),
                )
                stopped = worker.wait(remaining_ms)
                still_running = worker.isRunning()
            except Exception as exc:
                stopped = False
                still_running = True
                _log_desktop_close_error(
                    "event=desktop_close_rejected "
                    "reason=%s_qthread_shutdown_exception exception_type=%s",
                    component,
                    type(exc).__name__,
                )
            if stopped is False or still_running:
                nonclean_owners.append(component)
                _log_desktop_close_error(
                    "event=desktop_close_rejected reason=%s_qthread_timeout",
                    component,
                )
            elif attribute == "screenshot_worker":
                self.screenshot_worker = None
        if nonclean_owners:
            if self._restart_requested:
                if self.settings_panel is not None:
                    self.settings_panel.restart_button.setText("等待后台退出…")
                    self.settings_panel.character_status.setText("重启请求已接受，后台退出完成后自动重启。")
                self._restart_timer.start()
                event.ignore()
                return
            self._forced_close_armed = True
            self._forced_close_owners = tuple(nonclean_owners)
            self._forced_close_armed_at = time.monotonic()
            owners = ",".join(self._forced_close_owners)
            _log_desktop_close_error(
                "event=desktop_force_exit_armed owners=%s armed_monotonic=%.6f",
                owners,
                self._forced_close_armed_at,
            )
            try:
                self.dialogue_visibility_controller.show_system_message(
                    _FORCED_CLOSE_WARNING
                )
            except Exception as exc:
                _log_desktop_close_error(
                    "event=desktop_close_warning_failed exception_type=%s",
                    type(exc).__name__,
                )
            event.ignore()
            return
        self._restart_timer.stop()
        super().closeEvent(event)


def main() -> int:
    # FIRST: prime the environment from xiaosan.env BEFORE constructing anything
    # (CLAUDE.md #10, F19). Construction-time env readers (the song intent
    # classifier in SongController) used to run before AppHost.initialize()'s
    # load_secrets(), read an un-primed environment, and stay disabled forever.
    startup_secrets = load_secrets(with_environment_snapshot=True)
    # Keep the caller's directory for relative script/module restart arguments,
    # then anchor resources before loading app configuration or starting workers.
    # load_secrets above already anchors its dotenv path to the repository.
    restart_cwd = Path.cwd()
    if not getattr(sys, "frozen", False):
        os.chdir(Path(__file__).resolve().parents[1])
    # INFO baseline (log-cleanup pass): user-visible events (tool runs, companion
    # state, warmup/recover, WARN/ERROR) show by default; the verification
    # scaffolding ([TIMING], stream state machine, vendored TTS chatter) now sits
    # at DEBUG -- flip the level here when profiling.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs one INFO "HTTP Request: ... 200 OK" per LLM call (a companion turn
    # is probe + streamed followup, >=2 lines) -- pure noise at INFO; reversible.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Capture before Qt or model imports can consume arguments.
    restart_args = (
        [sys.executable, *sys.argv[1:]]
        if getattr(sys, "frozen", False)
        else [sys.executable, *sys.orig_argv[1:]]
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    window = OverlayWindow(loaded_secrets=startup_secrets)
    window.show()
    exit_code = app.exec()
    if window._restart_requested and exit_code == 0:
        try:
            os.chdir(restart_cwd)
            # Replace this process, preserving the interpreter, launch mode,
            # arguments and original launch environment. Fresh dotenv on restart.
            os.execve(sys.executable, restart_args, startup_secrets.restart_environment())
        except OSError as exc:
            logger.error("event=desktop_restart_failed exception_type=%s", type(exc).__name__)
            QMessageBox.critical(None, "重启失败", "桌面程序已停止，但未能重新启动。请从原入口手动启动。")
            return 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
