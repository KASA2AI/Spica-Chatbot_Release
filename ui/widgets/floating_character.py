"""A small transparent character surface; it owns no conversation or microphone."""

from collections import deque
import math
from pathlib import Path
import random
import time

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QBitmap, QGuiApplication, QMovie, QPainter, QPixmap, QRegion, QTransform
from PySide6.QtWidgets import QApplication, QWidget

from spica.core.character_manifest import package_file
from spica.core.floating_character import load_floating_character

# Preserve existing preference values, now interpreted as screen-relative tiers.
_SIZE_RATIOS = {72: .10, 96: .15, 128: .20}
_DOCK_WIDTH = .6875
_DOCK_HEIGHT = .9375


class FloatingCharacterWindow(QWidget):
    menu_requested = Signal(QPoint)
    position_changed = Signal(QPoint)
    quit_requested = Signal()
    resource_error = Signal(str)
    wake_requested = Signal()
    geometry_changed = Signal()

    def __init__(self):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle('Spica · 悬浮语音')
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.resize(96, 96)
        self._spec = None
        self._root = None
        self._movie = None
        self._frame = QPixmap()
        self._poster = QPixmap()
        self._state = 'idle'
        self._closed = False
        self._size_choice = 96
        self._base_size = 96
        self._docked_side = None
        self._dock_from = QPixmap()
        self._dock_progress = 1.
        self._dock_animation = QVariantAnimation(self)
        self._dock_animation.setDuration(480)
        self._dock_animation.setStartValue(0.)
        self._dock_animation.setEndValue(1.)
        self._dock_animation.valueChanged.connect(self._animate_dock)
        self._dock_animation.finished.connect(self._finish_dock_transition)
        self._reduced_motion = False
        self._last_easter_at = time.monotonic()
        self._sizing_screen = None
        self._screen_handle_connected = False
        self._rendered_state = None
        self._press = None
        self._offset = QPoint()
        self._dragging = False
        self._shake_anchor = None
        self._shake_direction = None
        self._shake_turns = deque(maxlen=4)
        self._shake_level = 0
        self._shaken = False
        self._reaction = None
        self._reaction_timer = QTimer(self)
        self._reaction_timer.setSingleShot(True)
        self._reaction_timer.timeout.connect(self._finish_reaction)
        self._idle_actions_allowed = True
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._play_idle_action)
        self._menu_open = False
        self._reported = set()
        self._click_count = 0
        self._last_click_at = 0.
        self._click_origin = None

    def set_package(self, package, fallback: QPixmap | None = None):
        if self._docked_side is not None:
            self.set_docked(None)
        self._last_easter_at = time.monotonic()
        self._idle_timer.stop()
        self._reset_clicks()
        self._reset_gesture()
        self._cancel_reaction()
        self._release_movie()
        self._spec = self._root = None
        self._reported.clear()
        self._poster = (fallback.scaled(256, 256, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
                        if fallback is not None and not fallback.isNull() else QPixmap())
        configured = getattr(package, 'floating_config_path', None)
        if configured:
            try:
                path = Path(configured)
                self._spec, self._root = load_floating_character(path), path.parent
                poster = QPixmap(str(package_file(self._root, self._spec.poster)))
                if not poster.isNull():
                    self._poster = poster.scaled(256, 256, Qt.AspectRatioMode.KeepAspectRatio,
                                                 Qt.TransformationMode.SmoothTransformation)
            except (OSError, ValueError) as exc:
                self.resource_error.emit(f'悬浮素材不可用，使用当前角色静态图：{exc}')
        self._frame = self._poster
        self._rendered_state = None
        self._render_state()

    def set_display_size(self, size: int):
        self._reset_clicks()
        self._reset_gesture()
        self._cancel_reaction()
        self._size_choice = min(_SIZE_RATIOS, key=lambda choice: abs(choice - int(size)))
        self.move_visible(self.pos())
        self._render_state()

    def _set_sizing_screen(self, screen):
        if screen is self._sizing_screen:
            return
        if self._sizing_screen is not None:
            try:
                for signal in (self._sizing_screen.availableGeometryChanged,
                               self._sizing_screen.geometryChanged,
                               self._sizing_screen.logicalDotsPerInchChanged):
                    signal.disconnect(self._screen_metrics_changed)
            except RuntimeError:  # QScreen may already be destroyed after unplugging it.
                pass
        self._sizing_screen = screen
        if screen is not None:
            for signal in (screen.availableGeometryChanged, screen.geometryChanged,
                           screen.logicalDotsPerInchChanged):
                signal.connect(self._screen_metrics_changed)

    def _screen_metrics_changed(self, *_args):
        self.move_visible(self.pos())

    def _screen_changed(self, screen):
        self._set_sizing_screen(screen)
        self.move_visible(self.pos())

    def _fit_screen_size(self, screen):
        rect = screen.availableGeometry().intersected(screen.geometry())
        size = max(48, min(round(screen.geometry().height() * _SIZE_RATIOS[self._size_choice]),
                           rect.width(), rect.height()))
        previous = self._base_size
        self._base_size = size
        compact = self._docked_side is not None and self._dock_from.isNull()
        target_size = QSize(round(size * _DOCK_WIDTH), round(size * _DOCK_HEIGHT)) if compact else QSize(size, size)
        if target_size != self.size():
            if self._dragging:
                self._offset = self._offset * (size / previous)
            self.resize(target_size)
        if self._movie is not None:
            target = QSize(size, size) * screen.devicePixelRatio()
            if self._movie.scaledSize() != target:
                self._movie.setScaledSize(target)
        self._update_mask()


    def bubble_anchor(self):
        return self.frameGeometry()



    def set_state(self, state: str):
        if state not in ('idle', 'listening', 'speaking', 'thinking', 'sleep'):
            return
        self._state = state
        if state in ('listening', 'speaking', 'thinking'):
            if self._reaction in {'petting', 'welcome', 'notice', 'easter_egg'}:
                self._cancel_reaction()
            if self._docked_side is not None:
                self.set_docked(None)
        self._render_state()

    @property
    def docked_side(self):
        return self._docked_side

    @property
    def interaction_active(self):
        return self._press is not None or self._menu_open or self._reaction is not None

    def has_state(self, state):
        return self._spec is not None and state in self._spec.states and state not in self._reported

    def set_reduced_motion(self, enabled):
        enabled = bool(enabled)
        if enabled == self._reduced_motion:
            return
        self._reduced_motion = enabled
        if enabled and not self._dock_from.isNull():
            self._finish_dock_transition()
        self._release_movie()
        self._render_state()

    def play_interaction(self, state):
        if (state not in {'petting', 'welcome', 'notice', 'easter_egg'} or not self.has_state(state)
                or self._closed or not self.isVisible()
                or self._state not in {'idle', 'sleep'} or self._press is not None or self._menu_open):
            return False
        if self._docked_side is not None:
            self.set_docked(None)
        self._state = 'idle'
        if state == 'easter_egg':
            self._last_easter_at = time.monotonic()
        self._play_reaction(state)
        return self._reaction == state

    def prepare_for_voice(self):
        if self._closed:
            return
        self._cancel_reaction()
        if self._state == 'sleep':
            self._state = 'idle'
        if self._docked_side is not None:
            self.set_docked(None)
        else:
            self._render_state()

    def set_docked(self, side):
        if side not in (None, 'left', 'right') or side == self._docked_side:
            return
        if side is not None and (not self.has_state('peek')
                                 or self._state not in {'idle', 'sleep'}):
            return
        old_rect, old_side = self.frameGeometry(), self._docked_side
        screen = QGuiApplication.screenAt(old_rect.center()) or QGuiApplication.primaryScreen()
        previous = self._frame
        animate = (side is not None and old_side is None and self.isVisible()
                   and not self._reduced_motion and not previous.isNull())
        self._stop_dock_transition()
        self._docked_side = side
        if animate:
            self._dock_from, self._dock_progress = previous, 0.
        self._cancel_reaction()
        self._release_movie()
        self._reset_clicks()
        if screen is not None:
            self._fit_screen_size(screen)
            bounds = screen.availableGeometry().intersected(screen.geometry())
            x = bounds.left() if side == 'left' else bounds.right() + 1 - self.width() if side == 'right' else (
                old_rect.right() + 1 - self.width() if old_side == 'right' else old_rect.left())
            self.move_visible(QPoint(x, old_rect.top()))
        self._render_state()
        if animate:
            self._dock_animation.start()
        self.geometry_changed.emit()

    def _animate_dock(self, progress):
        self._dock_progress = float(progress)
        self._update_mask()
        self.update()

    def _stop_dock_transition(self):
        self._dock_animation.stop()
        self._dock_from = QPixmap()
        self._dock_progress = 1.

    def _finish_dock_transition(self):
        self._stop_dock_transition()
        if self._closed or self._docked_side is None:
            return
        screen = QGuiApplication.screenAt(self.frameGeometry().center()) or QGuiApplication.primaryScreen()
        if screen is not None:
            self._fit_screen_size(screen)
            bounds = screen.availableGeometry().intersected(screen.geometry())
            x = bounds.left() if self._docked_side == 'left' else bounds.right() + 1 - self.width()
            self.move_visible(QPoint(x, self.y()))
        self._update_mask()
        self.update()
        self.geometry_changed.emit()

    def _dock_near_edge(self):
        screen = QGuiApplication.screenAt(self.frameGeometry().center()) or QGuiApplication.primaryScreen()
        if screen is not None:
            rect = screen.availableGeometry().intersected(screen.geometry())
            if self.x() - rect.left() <= 12:
                self.set_docked('left')
            elif rect.right() + 1 - self.x() - self.width() <= 12:
                self.set_docked('right')

    def set_idle_actions_allowed(self, allowed: bool):
        if bool(allowed) != self._idle_actions_allowed:
            self._idle_actions_allowed = bool(allowed)
            if not allowed and self._reaction in {'petting', 'welcome', 'notice', 'easter_egg'}:
                self._cancel_reaction()
            self._render_state()

    def _idle_candidates(self):
        return ({name: action for name, action in self._spec.idle_actions.items()
                 if 'idle:' + name not in self._reported} if self._spec is not None else {})

    def _idle_eligible(self):
        return (not self._closed and self.isVisible() and not self.isMinimized() and self._idle_actions_allowed
                and self._state == 'idle'
                and self._press is None and not self._menu_open
                and self._docked_side is None and not self._reduced_motion)

    def _sync_idle_actions(self):
        if not self._idle_eligible():
            self._idle_timer.stop()
            if self._reaction is not None and self._reaction.startswith('idle:'):
                self._cancel_reaction()
        elif self._reaction is not None or not self._idle_candidates():
            self._idle_timer.stop()
        elif not self._idle_timer.isActive():
            self._idle_timer.start(random.randint(*self._spec.idle_interval_ms))

    def _play_idle_action(self):
        if not self._idle_eligible() or self._reaction is not None:
            return
        if (self.has_state('easter_egg') and time.monotonic() - self._last_easter_at >= 300.
                and random.random() < .12):
            self.play_interaction('easter_egg')
            return
        candidates = self._idle_candidates()
        if candidates:
            name = random.choices(list(candidates), weights=[action.weight for action in candidates.values()], k=1)[0]
            self._play_reaction('idle:' + name)

    def _animation(self, state):
        if self._spec is None or state is None:
            return None
        return (self._spec.idle_actions.get(state[5:]) if state.startswith('idle:')
                else self._spec.states.get(state))

    def _release_movie(self):
        self._reaction_timer.stop()
        movie, self._movie = self._movie, None
        if movie is not None:
            movie.stop()
            movie.frameChanged.disconnect(self._frame_changed)
            movie.deleteLater()
        self._rendered_state = None

    def _render_state(self):
        if self._closed:
            return
        self._sync_idle_actions()
        state = self._reaction or self._state
        if self._docked_side is not None:
            state = 'peek'
        if self._dragging:
            state = ('dizzy_held' if self._shaken and self._spec is not None and self._spec.dizzy_held
                     else 'dragged')
        if state == self._rendered_state or not self.isVisible():
            return
        self._release_movie()
        self._rendered_state = state
        # Retain the previous pose until the next state has an actual frame.
        # This avoids flashing the idle poster between held and recovery poses.
        static = getattr(self._spec, state, None) if state == 'dizzy_held' else None
        if static:
            try:
                frame = QPixmap(str(package_file(self._root, static)))
                if frame.isNull():
                    raise ValueError('无法解码静态表情')
                self._frame = frame
            except (OSError, ValueError) as exc:
                self._frame = self._poster
                if state not in self._reported:
                    self._reported.add(state)
                    self.resource_error.emit(f'{state} 表情不可用，使用当前角色静态图：{exc}')
        animation = self._animation(state)
        if self._reduced_motion:
            frame = self._frame if static else self._poster
            if animation is not None and animation.keyframes:
                try:
                    index = 0 if state == 'peek' else min(1, len(animation.keyframes) - 1)
                    path = animation.keyframes[index]
                    candidate = QPixmap(str(package_file(self._root, path)))
                    if not candidate.isNull():
                        frame = candidate
                except (OSError, ValueError):
                    pass
            self._frame = frame
            if self._reaction is not None:
                self._reaction_timer.start(900)
            self._update_mask()
            self.update()
            return
        if animation is not None:
            try:
                path = package_file(self._root, animation.file)
                movie = QMovie(str(path), parent=self)
                movie.setCacheMode(QMovie.CacheMode.CacheNone)
                movie.setScaledSize(QSize(self._base_size, self._base_size) * self.devicePixelRatioF())
                if not movie.isValid():
                    movie.deleteLater()
                    raise ValueError('无法解码动画')
                self._movie = movie
                movie.frameChanged.connect(self._frame_changed)
                movie.error.connect(lambda _error, current=movie: QTimer.singleShot(0,
                    lambda: self._decode_failed(current)))
                movie.finished.connect(lambda current=movie: self._animation_finished(current))
                movie.start()
                if animation.start_frame:
                    # WebP cannot seek with CacheNone. Advance sequentially in
                    # this event turn, suppressing intro frames in the slot.
                    movie.setPaused(True)
                    for _ in range(animation.start_frame):
                        if not movie.jumpToNextFrame():
                            raise ValueError('无法定位动画起始帧')
                    movie.setPaused(False)
                if movie.currentFrameNumber() == movie.frameCount() - 1 and self._reaction is not None:
                    movie.setPaused(True)
                    self._reaction_timer.start(max(1, movie.nextFrameDelay()))
            except (OSError, ValueError) as exc:
                self._release_movie()
                self._frame = self._poster
                if state not in self._reported:
                    self._reported.add(state)
                    self.resource_error.emit(f'{state} 动画不可用，使用当前角色静态图：{exc}')
                if self._reaction == state:
                    self._cancel_reaction()
                    self._render_state()
                    return
        elif not static:
            self._frame = self._poster
        self._update_mask()
        self.update()

    def _frame_changed(self, _index):
        if self.sender() is not self._movie or self._movie is None:
            return
        animation = self._animation(self._rendered_state)
        if animation is not None and _index < animation.start_frame:
            return
        frame = self._movie.currentPixmap()
        if not frame.isNull():
            self._frame = frame
            self._update_mask()
            self.update()
        if _index == self._movie.frameCount() - 1:
            if self._reaction is not None:
                # Reactions play once even if an authored file loops forever.
                # Hold the final frame for its real delay before restoring the
                # current voice state; do not restore the state at click time.
                self._movie.setPaused(True)
                self._reaction_timer.start(max(1, self._movie.nextFrameDelay()))
            elif self._spec is not None and not self._spec.loop:
                self._movie.setPaused(True)

    def _animation_finished(self, movie):
        if movie is not self._movie:
            return
        if self._reaction is not None:
            self._finish_reaction()
        elif self._spec is not None and self._spec.loop:
            movie.start()

    def _cancel_reaction(self):
        self._reaction_timer.stop()
        self._reaction = None

    def _finish_reaction(self):
        if self._reaction is not None:
            self._cancel_reaction()
            self._render_state()

    def _play_reaction(self, state):
        if (self._spec is None
                or self._animation(state) is None or not self.isVisible()):
            self._render_state()
            return
        self._cancel_reaction()
        self._reaction = state
        self._release_movie()  # Repeated clicks restart this one animation.
        self._render_state()

    def _decode_failed(self, movie):
        if movie is not self._movie:
            return
        state = self._rendered_state
        self._release_movie()
        self._rendered_state = state
        self._frame = self._poster
        self._update_mask()
        self.update()
        if state not in self._reported:
            self._reported.add(state)
            self.resource_error.emit('悬浮动画解码失败，已回退当前角色静态图。')
        if self._reaction == state:
            self._cancel_reaction()
            self._render_state()

    def _draw_rect(self, frame=None):
        if self._docked_side is not None:
            width = round(self._base_size * _DOCK_WIDTH)
            extent = max(0., min(1., (self._dock_progress - .45) / .55))
            extent = extent * extent * (3 - 2 * extent)
            hidden = round(width * (1 - extent))
            offset = (-(self._base_size - width) - hidden if self._docked_side == 'left'
                      else self.width() - width + hidden)
            return QRect(offset, 0, self._base_size, self._base_size)
        frame = self._frame if frame is None else frame
        if frame.isNull():
            return self.rect().adjusted(4, 4, -4, -4)
        size = frame.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRect((self.width() - size.width()) // 2, (self.height() - size.height()) // 2,
                     size.width(), size.height())

    def _display_frame(self, frame):
        if self._docked_side != 'right' or frame.isNull():
            return frame
        # The artwork already contains the natural half-hidden pose. Keep its
        # shoulder, grip and downward hair intact; only mirror for the right edge.
        return frame.transformed(QTransform().scale(-1, 1))

    def _dock_body_rect(self):
        progress = min(1., self._dock_progress / .55)
        progress = progress * progress * (3 - 2 * progress)
        offset = round(self._base_size * progress) * (-1 if self._docked_side == 'left' else 1)
        return QRect(offset, 0, self._base_size, self._base_size)

    def _update_mask(self):
        # Keep the live silhouette while dragging too. Clearing a shaped X11
        # window can leave its previous clip in the compositor. Qt's mouse
        # grab already carries the drag beyond the visible character pixels.
        if self._frame.isNull():
            self.clearMask()
            return
        def frame_region(frame, rect):
            image = frame.toImage().scaled(rect.size(), Qt.AspectRatioMode.IgnoreAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation)
            # Opaque posters/decoder frames have no alpha channel. Qt returns
            # a null alpha mask for them, not a full one. Keep an explicit
            # rectangle while waiting for the next animated silhouette.
            if not image.hasAlphaChannel():
                return QRegion(rect)
            return QRegion(QBitmap.fromImage(image.createAlphaMask())).translated(rect.topLeft())

        region = frame_region(self._display_frame(self._frame), self._draw_rect())
        if not self._dock_from.isNull():
            region |= frame_region(self._dock_from, self._dock_body_rect())
        if region.isEmpty():
            self.clearMask()
        else:
            self.setMask(region.intersected(QRegion(self.rect())))

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if not self._dock_from.isNull():
            painter.drawPixmap(self._dock_body_rect(), self._dock_from)
        if self._frame.isNull():
            # Even an entirely missing character image must leave a reachable menu.
            painter.setBrush(Qt.GlobalColor.darkCyan)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(self._draw_rect())
        else:
            painter.drawPixmap(self._draw_rect(), self._display_frame(self._frame))

    def showEvent(self, event):
        super().showEvent(event)
        if not self._screen_handle_connected and self.windowHandle() is not None:
            self.windowHandle().screenChanged.connect(self._screen_changed)
            self._screen_handle_connected = True
        self.move_visible(self.pos())
        self._render_state()

    def hideEvent(self, event):
        self._idle_timer.stop()
        self._stop_dock_transition()
        self._reset_clicks()
        self._reset_gesture()
        self._cancel_reaction()
        self._release_movie()
        self._frame = self._poster
        super().hideEvent(event)

    def contextMenuEvent(self, event):
        self._reset_clicks()
        self._menu_open = True
        self._render_state()
        try:
            self.menu_requested.emit(event.globalPos())
        finally:
            self._menu_open = False
            self._render_state()

    def moveEvent(self, event):
        super().moveEvent(event)
        self.geometry_changed.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.geometry_changed.emit()

    def mouseDoubleClickEvent(self, event):
        # Qt replaces the second press with this event. Count releases once.
        self.mousePressEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._reset_gesture()
            self._press = event.globalPosition().toPoint()
            self._offset = self._press - self.pos()
            self._shake_anchor = (self._press, time.monotonic())
            self._render_state()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._press is None:
            return
        point = event.globalPosition().toPoint()
        if not self._dragging:
            delta = point - self._press
            if math.hypot(delta.x(), delta.y()) < 4:
                return
            if self._docked_side is not None:
                self.set_docked(None)
            self._reset_clicks()
            self._cancel_reaction()
            self._dragging = True
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            animation = self._spec.states.get('dragged') if self._spec else None
            if animation is not None and animation.pivot_px is not None:
                self._offset = QPoint(round(animation.pivot_px[0] * self.width() / self._spec.canvas[0]),
                                      round(animation.pivot_px[1] * self.height() / self._spec.canvas[1]))
        self._note_shake(point, time.monotonic())
        self._render_state()
        self.move_visible(point - self._offset, point)

    def _note_shake(self, point, now):
        if (self._spec is None
                or 'dizzy' not in self._spec.states or self._shake_anchor is None):
            return
        anchor, started = self._shake_anchor
        dx, dy = point.x() - anchor.x(), point.y() - anchor.y()
        distance = math.hypot(dx, dy)
        if distance < max(28., self.width() * .38):
            return
        direction = (dx / distance, dy / distance)
        while self._shake_turns and now - self._shake_turns[0] > 1.8:
            self._shake_turns.popleft()
        if distance / max(.001, now - started) < 140.:
            self._shake_turns.clear()
        elif self._shake_direction is not None and sum(a * b for a, b in zip(direction, self._shake_direction)) < -.55:
            self._shake_turns.append(now)
        self._shake_direction = direction
        self._shake_anchor = (point, now)
        self._shake_level = len(self._shake_turns)
        self._shaken = self._shaken or self._shake_level >= 4

    def _reset_gesture(self):
        self._press = self._shake_anchor = self._shake_direction = None
        self._dragging = self._shaken = False
        self._shake_turns.clear()
        self._shake_level = 0
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._press is None:
            return
        moved, shaken = self._dragging, self._shaken
        pressed_at = self._shake_anchor[1]
        self._reset_gesture()
        if moved:
            if shaken:
                self._play_reaction('dizzy')
            else:
                self._render_state()
                self._dock_near_edge()
            self.position_changed.emit(self.pos())
        else:
            if self._docked_side is not None:
                self.set_docked(None)
                self.play_interaction('welcome')
            elif self._count_click(event.globalPosition().toPoint(), pressed_at):
                self._cancel_reaction()
                self._render_state()
                self.wake_requested.emit()
            else:
                self._play_reaction('click')

    def _reset_clicks(self):
        self._click_count = 0
        self._last_click_at = 0.
        self._click_origin = None

    def _count_click(self, point, pressed_at):
        now = time.monotonic()
        interval = QApplication.doubleClickInterval() / 1000
        if now - pressed_at > interval:
            self._reset_clicks()
            return False
        if (now - self._last_click_at > interval or self._click_origin is None
                or (point - self._click_origin).manhattanLength() > QApplication.startDragDistance()):
            self._click_count = 0
            self._click_origin = point
        self._click_count += 1
        self._last_click_at = now
        if self._click_count == 3:
            self._reset_clicks()
            return True
        return False

    def move_visible(self, position: QPoint, pointer: QPoint | None = None):
        screen = QGuiApplication.screenAt(pointer or position) or QGuiApplication.primaryScreen()
        if screen is not None:
            self._set_sizing_screen(screen)
            self._fit_screen_size(screen)
            if pointer is not None and self._dragging:
                position = pointer - self._offset
            rect = screen.availableGeometry().intersected(screen.geometry())
            position = QPoint(max(rect.left(), min(position.x(), rect.right() + 1 - self.width())),
                              max(rect.top(), min(position.y(), rect.bottom() + 1 - self.height())))
            if self._docked_side is not None and not self._dragging:
                position.setX(rect.left() if self._docked_side == 'left' else rect.right() + 1 - self.width())
        self.move(position)

    def closeEvent(self, event):
        event.ignore()
        self.quit_requested.emit()

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self.hide()
        self._set_sizing_screen(None)
        self._release_movie()
        self._frame = self._poster = QPixmap()
        self.deleteLater()
