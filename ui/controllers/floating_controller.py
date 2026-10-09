"""Switch desktop presentation without opening another core or voice session."""

import html
import logging
import time

from PySide6.QtCore import QObject, QPoint, QTimer
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QMenu, QToolTip

from ui.desktop_input_activity import DesktopInputActivity
from ui.overlay_config import load_floating_preferences, save_voice_ui_preference
from ui.widgets.floating_character import FloatingCharacterWindow
from ui.widgets.floating_bubble import FloatingDialogueBubble
from ui.widgets.floating_controls import FloatingControls

logger = logging.getLogger(__name__)


class FloatingController(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.view = FloatingCharacterWindow()
        self.bubble = FloatingDialogueBubble(self.view)
        self.controls = FloatingControls(self.view)
        self.enabled = False
        self._closed = False
        self._mute_started_at = None
        self._mute_feedback = ''
        self._full_notice = None
        self._bubble_kind = None
        self._package_key = None
        self._return_after_settings = False
        self._hover_since = None
        self._hover_until = 0.
        self._current_notice = None
        self._notice_animation_id = None
        self._dozing = False
        preferences = load_floating_preferences()
        self._reduced_motion = preferences.get('reduced_motion', False)
        self.view.set_reduced_motion(self._reduced_motion)
        self._initially_enabled = preferences['enabled']
        self.view.set_package(getattr(window.host, 'character_package', None), window.current_pixmap)
        self.view.set_display_size(preferences['size'])
        position = preferences['position']
        if position is None:
            screen = QGuiApplication.primaryScreen()
            rect = screen.availableGeometry().intersected(screen.geometry()) if screen else window.geometry()
            point = QPoint(rect.right() - self.view.width() - 24, rect.bottom() - self.view.height() - 24)
        else:
            point = QPoint(*position)
        self.view.move_visible(point)
        self.view.menu_requested.connect(self.show_menu)
        self.view.position_changed.connect(lambda point: self._save('floating_position', [point.x(), point.y()]))
        self.view.quit_requested.connect(window.close)
        self.view.resource_error.connect(self.notify)
        self.view.wake_requested.connect(self._manual_wake)
        self.view.geometry_changed.connect(self._position_bubble)
        self.controls.wake_requested.connect(self._manual_wake)
        self.controls.pet_requested.connect(lambda: self._interact('petting'))
        self.controls.expand_requested.connect(lambda: self.set_enabled(False))
        self.controls.notice_requested.connect(self._dismiss_notice)
        # A visible miniature or local notice needs this presentation refresh;
        # the timer never grants microphone permissions or drives a conversation.
        self._refresh = QTimer(self)
        self._refresh.setInterval(100)
        self._refresh.timeout.connect(self.refresh)
        self._mute_timer = QTimer(self)
        self._mute_timer.setInterval(100)
        self._mute_timer.timeout.connect(self._check_mute_release)
        voice = window.voice_input_controller
        voice.set_manual_muted(preferences['muted'])
        voice.manual_mute_changed.connect(self._mute_changed)
        voice.capture_state_changed.connect(self.refresh)
        from ui.desktop_power import DesktopPowerEvents
        self._power = DesktopPowerEvents(self)
        self._input_activity = DesktopInputActivity(self)
        self._power.suspended.connect(voice.set_suspended)
        window.dialogue_visibility_controller.feedback_changed.connect(self._feedback)
        window.dialogue_visibility_controller.line_changed.connect(self._subtitle)

    def show_initial(self):
        # The entrypoint owns first presentation. A queued constructor callback
        # can run inside native startup and then be undone by window.show().
        if self._initially_enabled and self.window.agent is not None:
            self.set_enabled(True, persist=False)
        else:
            self.window.show()

    def _save(self, key, value):
        if not save_voice_ui_preference(key, value):
            self.notify('设置未能保存，本次会话仍按当前选择运行。')



    def set_enabled(self, enabled, *, persist=True):
        if self._closed or self.window.agent is None:
            return
        enabled = bool(enabled)
        temporary_settings = self._return_after_settings
        if persist:
            # A deliberate mode choice supersedes an in-flight settings close.
            self._return_after_settings = False
        if enabled == self.enabled:
            if persist and temporary_settings:
                self._save('floating_mode', enabled)
            return
        self.enabled = enabled
        if persist:
            self._save('floating_mode', enabled)
        if enabled:
            panel = getattr(self.window, 'settings_panel', None)
            if panel is not None and panel.isVisible():
                self.window._close_settings_panel()
                panel.stop_motion_for_layout()
            voice = self.window.voice_input_controller
            voice.use_short_conversation()
            self.window._bind_voice_reply(voice.response_reply_key)
            self.view.set_package(getattr(self.window.host, 'character_package', None), self.window.current_pixmap)
            self.view.move_visible(self.view.pos())
            self.view.show()
            self.window.hide()
            self._refresh.start()
            self.refresh()
            if voice.manual_muted:
                self.notify('麦克风已完全禁用，请右键恢复采音。')
            elif not voice.wake_enabled or not voice.wake_words:
                self.notify('可连续三击小人唤醒；语音唤醒尚未配置，可右键进入设置。')
        else:
            if not self.window.dialogue_visibility_controller.has_pending_notices:
                self._refresh.stop()
            self.bubble.clear()
            self.controls.hide()
            self._hover_since = None
            self._hover_until = 0.
            self._dozing = False
            self.view.hide()
            self.window.show()
            self.window.raise_()
            self.window._layout_overlay()
            self._refresh_bubble()

    def refresh(self):
        if self._closed:
            return
        if not self.enabled:
            if self.window.dialogue_visibility_controller.has_pending_notices:
                self._refresh.start()
            self._refresh_bubble()
            return
        voice = self.window.voice_input_controller
        package = getattr(self.window.host, 'character_package', None)
        key = (getattr(package, 'character_id', None), getattr(package, 'revision', None),
               getattr(package, 'package_root', None))
        if key != self._package_key:
            if self._package_key is not None:
                voice.end_daily_conversation(stop_reply=False)
            self._package_key = key
            self._dozing = False
            self.view.set_package(package, self.window.current_pixmap)
        playing = getattr(self.window.audio_controller, 'is_voice_playing', lambda: False)()
        chat = getattr(self.window, 'chat_stream_controller', None)
        busy = chat is not None and chat.is_busy()
        self.view.set_idle_actions_allowed(not voice.suspended and voice.backend_ready()
            and not voice.conversation_active and not busy)
        state = ('speaking' if playing else 'thinking' if busy or voice.preparing_response
                 else 'listening' if voice.capture_ready else 'idle')
        idle = self._input_activity.idle_seconds()
        was_dozing = self._dozing
        self._dozing = (state == 'idle' and not voice.conversation_active and not voice.suspended
                        and self.view.isVisible() and not self.view.isMinimized()
                        and not self.view.interaction_active and self.view.docked_side is None
                        and idle is not None and idle >= 300. and self.view.has_state('sleep'))
        self.view.set_state('sleep' if self._dozing else state)
        if (was_dozing and not self._dozing and idle is not None and idle < 2.
                and state == 'idle' and not voice.conversation_active and not self.view.interaction_active
                and not voice.suspended and not self._reduced_motion):
            self.view.play_interaction('welcome')
        self.view.setAccessibleDescription(self.microphone_status())
        self._refresh_bubble()
        self._refresh_controls(playing, busy)

    def _interact(self, action):
        voice = self.window.voice_input_controller
        chat = getattr(self.window, 'chat_stream_controller', None)
        if (self._closed or not self.enabled or voice.suspended or voice.conversation_active
                or chat is not None and chat.is_busy()):
            return
        self.view.play_interaction(action)

    def _status_info(self, playing, busy):
        voice = self.window.voice_input_controller
        if voice.suspended:
            return 'unavailable', '采音暂停'
        if voice.manual_muted:
            return 'muted', self.microphone_status()
        if playing:
            return 'speaking', '正在说话'
        if busy or voice.preparing_response:
            return 'thinking', '正在思考…'
        if voice.capture_error:
            return 'error', self.microphone_status()
        if not voice.backend_ready():
            return 'unavailable', '对话连接未就绪'
        if voice.capture_ready:
            return 'listening', '正在聆听'
        if not voice.capture_allowed:
            return 'unavailable', '采音暂停'
        if voice.wake_enabled:
            return 'waiting', '待唤醒 · 麦克风开启' if voice.wake_ready else '正在准备唤醒'
        return 'idle', '点击「说话」唤醒'

    def _refresh_controls(self, playing=False, busy=False):
        if self._closed:
            return
        voice = self.window.voice_input_controller
        if not self.enabled or not self.view.isVisible() or self.view.isMinimized() or voice.suspended:
            self.controls.hide()
            return
        if self.view._menu_open:
            return  # Keep tool windows stable while the native context menu is open.
        point, now = QCursor.pos(), time.monotonic()
        pet_rect = self.view.frameGeometry().adjusted(-8, -8, 8, 8)
        tools_rect = self.controls.frameGeometry().adjusted(-10, -10, 10, 10)
        over_pet = pet_rect.contains(point)
        over_tools = self.controls.isVisible() and tools_rect.contains(point)
        between = self.controls.toolbar.isVisible() and pet_rect.united(tools_rect).contains(point)
        if over_pet:
            if self._hover_since is None:
                self._hover_since = now
            if now - self._hover_since >= .25:
                self._hover_until = now + 1.2
        elif over_tools or between:
            self._hover_until = now + 1.2
        else:
            self._hover_since = None
        hovered = now < self._hover_until and not self.view._dragging
        mode, text = self._status_info(playing, busy)
        active = voice.conversation_active or busy or playing
        self.controls.present(text=text, mode=mode, hovered=hovered,
            notice_title=self._current_notice[1] if self._current_notice is not None and not active else '',
            interactive=not active and self.view.has_state('petting'),
            reduced_motion=self._reduced_motion,
            show_status=not self.bubble.isVisible() and (mode != 'idle' or hovered))
        if self.controls.isVisible():
            self.controls.place_near(self.view.bubble_anchor())

    def _dismiss_notice(self):
        if self._closed or not self.enabled or self._current_notice is None:
            return
        self.window.dialogue_visibility_controller.dismiss_notice(self._current_notice[0])
        self.refresh()

    def _set_reduced_motion(self, enabled):
        self._reduced_motion = bool(enabled)
        self.view.set_reduced_motion(self._reduced_motion)
        self._save('floating_reduced_motion', self._reduced_motion)
        self.refresh()

    def _rest_at_edge(self):
        screen = QGuiApplication.screenAt(self.view.frameGeometry().center()) or QGuiApplication.primaryScreen()
        if screen is not None:
            bounds = screen.availableGeometry().intersected(screen.geometry())
            self.view.set_docked('left' if self.view.frameGeometry().center().x() < bounds.center().x() else 'right')

    def _manual_wake(self):
        if self._closed or not self.enabled:
            return
        voice = self.window.voice_input_controller
        package = getattr(self.window.host, 'character_package', None)
        name = (getattr(package, 'char_name', '') or getattr(package, 'name', '') or 'Spica')
        if not voice.request_manual_wake(name):
            if voice.manual_muted:
                self.notify('麦克风已完全禁用，请先在右键菜单恢复采音。')
            elif not voice.backend_ready():
                self.notify('对话连接尚未就绪，请稍后再试。')
            elif not voice.capture_allowed:
                self.notify('当前桌面采音暂停，请先结束设备测试或恢复桌面。')
            else:
                self.notify('当前已有对话或接话窗口，不重复唤醒。')
        else:
            self.view.prepare_for_voice()
        self.refresh()


    def _bubble_context(self):
        voice = self.window.voice_input_controller
        dialogue = self.window.dialogue_visibility_controller
        chat = getattr(self.window, 'chat_stream_controller', None)
        if (self._closed or voice.suspended or not voice.backend_ready()):
            return None
        package = getattr(self.window.host, 'character_package', None)
        return (getattr(package, 'character_id', None), getattr(package, 'revision', None),
                getattr(package, 'package_root', None), getattr(chat, 'stream_session_id', None))

    def _refresh_bubble(self):
        if self._closed:
            return
        voice = self.window.voice_input_controller
        dialogue = self.window.dialogue_visibility_controller
        chat = getattr(self.window, 'chat_stream_controller', None)
        package = getattr(self.window.host, 'character_package', None)
        name = getattr(package, 'char_name', '') or getattr(package, 'name', '') or 'Spica'
        surface = self.view if self.enabled else self.window
        panel = getattr(self.window, "settings_panel", None)
        visible = surface.isVisible() and not surface.isMinimized() and not (panel is not None and panel.isVisible())
        available = not voice.suspended and voice.backend_ready()
        active = (voice.conversation_active or chat is not None and chat.is_busy())
        content = dialogue.floating_content(self._bubble_context(), active=active,
            available=available, visible=visible and not voice.suspended and not dialogue.system_reveal_active, speaker=name,
            input_idle_seconds=self._input_activity.idle_seconds() if dialogue.has_pending_notices else None)
        self._current_notice = None
        if self.enabled or content is None or content[0] != 'notice':
            dialogue.show_notice_message(None)
            self._full_notice = None
        if content is None:
            self._bubble_kind = None
            self.bubble.clear()
            self._full_notice = None
            if not self.enabled and not dialogue.has_pending_notices:
                self._refresh.stop()
            return
        kind, title, text, _identity = content
        if kind == 'notice':
            self._current_notice = (_identity, title, text)
            if self.enabled and self._notice_animation_id != _identity:
                self._notice_animation_id = _identity
                self.view.play_interaction('notice')
        self._bubble_kind = kind
        if self.enabled:
            self.bubble.set_dialogue(title, text)
            self.bubble.place_near(self.view.bubble_anchor())
            self.bubble.show()
        else:
            self.bubble.hide()
            if kind == 'notice' and self._full_notice != content:
                self._full_notice = content
                dialogue.show_notice_message(f'{title}：{text}')

    def show_notice(self, notice):
        if self._closed or not self.window.dialogue_visibility_controller.queue_notice(notice):
            return
        logger.info('event=desktop_notice_received id=%s', notice.notification_id)
        self._refresh.start()
        self._refresh_bubble()

    @property
    def reply_text_visible(self):
        return (not self._closed and self.enabled and self.view.isVisible()
                and not self.view.isMinimized() and self.bubble.isVisible()
                and self._bubble_kind == 'dialogue' )

    def _position_bubble(self):
        if not self._closed and self.bubble.isVisible():
            self.bubble.place_near(self.view.bubble_anchor())
        if not self._closed and self.controls.isVisible():
            self.controls.place_near(self.view.bubble_anchor())

    def microphone_status(self):
        voice = self.window.voice_input_controller
        if voice.manual_muted:
            return self._mute_feedback or '麦克风已完全禁用'
        if voice.capture_error:
            return voice.capture_error
        if not voice.capture_allowed:
            return '当前桌面采音暂停'
        if voice.capture_ready:
            return '正在聆听'
        if voice.preparing_response:
            return '正在准备本次接话'
        if voice.wake_enabled:
            return '等待唤醒（仍使用麦克风）' if voice.wake_ready else '正在准备唤醒监听'
        return '语音唤醒未启用'

    def _mute_changed(self, muted):
        if muted:
            self._mute_started_at = time.monotonic()
            self._mute_feedback = '正在关闭采音…'
            self._mute_timer.setInterval(100)
            self._mute_timer.start()
            self._check_mute_release()
        else:
            self._mute_timer.stop()
            self._mute_feedback = ''
            self.notify('采音许可已恢复；旧对话不会重新开启。')
        self.refresh()

    def _check_mute_release(self):
        voice = self.window.voice_input_controller
        devices = getattr(self.window, 'device_settings_controller', None)
        if voice.capture_released and (devices is None or devices.capture_released):
            self._mute_feedback = '麦克风已完全禁用'
            self._mute_timer.stop()
            self.notify(self._mute_feedback)
        elif self._mute_started_at is not None and time.monotonic() - self._mute_started_at >= 1.5:
            self._mute_feedback = '采音尚未释放；已禁止新录音，请检查设备连接'
            if self._mute_timer.interval() != 500:
                self.notify(self._mute_feedback)
                self._mute_timer.setInterval(500)

    def notify(self, message):
        if self.enabled and not self._closed:
            QToolTip.showText(self.view.mapToGlobal(QPoint(self.view.width() + 4, 0)),
                             html.escape(str(message)[:240]), self.view, msecShowTime=4000)

    def _subtitle(self, text):
        if self._closed:
            return
        context = self._bubble_context()
        if context is not None:
            self.window.dialogue_visibility_controller.remember_floating_line(text, context)
        self._refresh_bubble()

    def _feedback(self, message):
        if str(message).strip() not in {'等待说话中....', '...', '……'}:
            self.notify(message)

    def _open_settings(self):
        if not self._closed:
            self.window.open_settings_panel()

    def prepare_settings(self):
        """Reveal settings without changing the preferred startup surface."""
        if not self._closed and self.enabled:
            self._return_after_settings = True
            self.set_enabled(False, persist=False)

    def settings_closed(self):
        return_to_pet = self._return_after_settings
        self._return_after_settings = False
        if return_to_pet and not self._closed and not getattr(self.window, '_restart_requested', False):
            self.set_enabled(True, persist=False)

    def collapse(self):
        self.set_enabled(True)

    def show_menu(self, position):
        if self._closed:
            return
        menu = QMenu(self.view if self.enabled else self.window)
        try:
            alarm = getattr(self.window, 'alarm_controller', None)
            if alarm is not None:
                menu.addAction('闹钟', alarm.show)
            if self.enabled :
                menu.addAction('说话', lambda: QTimer.singleShot(0, self._manual_wake))
            menu.addAction('展开完整界面' if self.enabled else '收起为桌宠',
                           lambda: self.set_enabled(not self.enabled))
            if self.enabled:
                for state, label in (('petting', '摸摸头'), ('easter_egg', '角色彩蛋')):
                    action = menu.addAction(label, lambda _checked=False, state=state:
                        QTimer.singleShot(0, lambda: self._interact(state)))
                    action.setEnabled(self.view.has_state(state) )
                edge = menu.addAction('出来走走' if self.view.docked_side else '在屏幕边缘休息',
                    lambda: self.view.set_docked(None) if self.view.docked_side else self._rest_at_edge())
                edge.setEnabled(self.view.has_state('peek') )
                if self._current_notice is not None:
                    menu.addAction('看完了，收起纸条', self._dismiss_notice)
            motion = menu.addAction('少动一点')
            motion.setCheckable(True)
            motion.setChecked(self._reduced_motion)
            motion.toggled.connect(self._set_reduced_motion)
            menu.addSeparator()
            status = menu.addAction(self.microphone_status())
            status.setEnabled(False)
            menu.addSeparator()
            menu.addAction('结束日常对话', self.window.voice_input_controller.end_daily_conversation)
            muted = self.window.voice_input_controller.manual_muted
            menu.addAction('恢复采音' if muted else '完全禁麦',
                           lambda: self.window.voice_input_controller.set_manual_muted(not muted))
            menu.addAction('恢复语音监听', self.window._load_voice_wake_preferences)
            menu.addAction('设置', self._open_settings)
            size_menu = menu.addMenu('角色大小')
            for size, label in ((72, '小'), (96, '中'), (128, '大')):
                size_menu.addAction(label, lambda _checked=False, size=size: self._set_size(size))
            menu.addSeparator()
            menu.addAction('退出 Spica', self.window.close)
            screen = QGuiApplication.screenAt(position) or QGuiApplication.primaryScreen()
            if screen is not None:
                bounds = screen.availableGeometry().intersected(screen.geometry())
                size = menu.sizeHint()
                position = QPoint(max(bounds.left(), min(position.x(), bounds.right() + 1 - size.width())),
                                  max(bounds.top(), min(position.y(), bounds.bottom() + 1 - size.height())))
            menu.exec(position)
        finally:
            menu.deleteLater()

    def _set_size(self, size):
        self.view.set_display_size(size)
        self._save('floating_size', size)

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._return_after_settings = False
        self._refresh.stop()
        self._mute_timer.stop()
        self.window.dialogue_visibility_controller.close_floating()
        self.bubble.clear()
        self.controls.shutdown()
        self._power.shutdown()
        self._input_activity.shutdown()
        self.view.shutdown()
