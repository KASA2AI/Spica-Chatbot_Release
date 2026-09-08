"""Shared sprite renderer, used locally for crossfades and eye animation.

Drawing stays inside paintEvent, below the desktop's existing drop shadow.
Static sprites at rest defer to QLabel; calibrated sprites add eye patches in
the same layer. The shared frame primitives are inert unless explicitly driven;
this desktop build has no endpoint transition controller or remote transport.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from PySide6.QtCore import QEasingCurve, QPointF, QRect, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QBrush, QColor, QCursor, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QLabel

from ui.widgets.eye_motion import EyeMotion

logger = logging.getLogger(__name__)

# Shared motion language (2026-07-23 OWNER revision: presence reads as cyber
# TRANSIT -- she leaves for another endpoint, she does not dissolve). These are
# aesthetic constants, deliberately NOT configuration.
CROSSFADE_MS = 120
RGB_GHOST_CYAN = QColor(70, 224, 255)
RGB_GHOST_MAGENTA = QColor(255, 79, 216)
RGB_GHOST_OPACITY = 0.55
LINE_CORE_COLOR = QColor(210, 245, 255)
BODY_GLOW_COLOR = QColor(220, 244, 255)

_SLOW_FRAME_MS = 25.0
_TINT_CACHE_CAP = 8
_SLICE_BANDS = 9
_SPARK_COUNT = 22
_STREAK_COUNT = 7
_BURST_COUNT = 18
_HAND_POSE_FOLDERS = frozenset({"普通动作", "抱肩", "竖食指"})


def _hash01(value: float) -> float:
    """Deterministic pseudo-random in [0,1) -- the GLSL fract-sin idiom."""
    raw = math.sin(value * 12.9898) * 43758.5453
    return raw - math.floor(raw)


def _clampf(value: float) -> float:
    return max(0.0, min(1.0, value))


def _is_hand_pose_change(previous: str | None, current: str | None) -> bool:
    if not previous or not current:
        return False
    old_path = PurePosixPath(previous.replace("\\", "/"))
    new_path = PurePosixPath(current.replace("\\", "/"))
    old_pose = old_path.parent.name
    new_pose = new_path.parent.name
    return bool(
        old_pose in _HAND_POSE_FOLDERS
        and new_pose in _HAND_POSE_FOLDERS
        and old_pose != new_pose
    )


@dataclass(frozen=True)
class PresenceFrame:
    """One rendered instant of the transit choreography.

    sx           horizontal squash toward the light line (1 = full body)
    rgb_dx       chromatic ghost offset in px (0 = aligned, no ghosts)
    sprite_alpha body opacity (0 during the line-only phases)
    line_alpha   additive light-line brightness
    line_y_frac  line top offset as a fraction of sprite height (negative = up)
    line_len_frac line length as a fraction of sprite height
    slice_amp    glitch band horizontal displacement amplitude in px
    slice_seed   deterministic seed for which bands shift (jumps mid-phase)
    spark_alpha  brightness of the rising spark particles
    spark_phase  spark travel progress (particles derive position statelessly)
    flare_alpha  horizontal light-burst brightness (body <-> line moments)
    flare_width_frac  flare width as a fraction of sprite width (shockwave grows)
    body_glow    silhouette tint: subtle bloom or full white-hot flash
    scan_alpha/scan_pos    bright scan band sweeping the silhouette (pos 0..1)
    ring_alpha/ring_radius ground energy ring at her feet (radius 0..~1.1)
    streak_alpha/streak_phase  vertical energy streams rising around the body
    burst_alpha/burst_phase    radial spark burst from the line's waist
    echo_alpha   ghost silhouette: residual signal after depart, hologram
                 preview before arrive (renders even with sprite_alpha 0)
    """

    sx: float = 1.0
    rgb_dx: float = 0.0
    sprite_alpha: float = 1.0
    line_alpha: float = 0.0
    line_y_frac: float = 0.0
    line_len_frac: float = 1.0
    slice_amp: float = 0.0
    slice_seed: int = 0
    spark_alpha: float = 0.0
    spark_phase: float = 0.0
    flare_alpha: float = 0.0
    flare_width_frac: float = 0.7
    body_glow: float = 0.0
    scan_alpha: float = 0.0
    scan_pos: float = 0.0
    ring_alpha: float = 0.0
    ring_radius: float = 0.0
    streak_alpha: float = 0.0
    streak_phase: float = 0.0
    burst_alpha: float = 0.0
    burst_phase: float = 0.0
    echo_alpha: float = 0.0


REST_FRAME = PresenceFrame()


class CharacterSpriteView(QLabel):
    """QLabel whose content layer can dissolve between sprites and transit."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._identity: str | None = None
        self._crossfade_old: QPixmap | None = None
        self._presence_frame = REST_FRAME
        self._tint_cache: dict[tuple[int, int], QPixmap] = {}
        self._last_motion_paint_ms: float | None = None
        self._slow_motion_frames = 0
        self._eye_rigs: dict[str, str] = {}
        self._eye_key = None
        self._eye_motion: EyeMotion | None = None
        self._eye_timer = QTimer(self)
        self._eye_timer.setInterval(33)
        self._eye_timer.timeout.connect(self._advance_eyes)
        self._crossfade = QVariantAnimation(self)
        self._crossfade.setStartValue(0.0)
        self._crossfade.setEndValue(1.0)
        self._crossfade.setDuration(CROSSFADE_MS)
        # Linear opacity is the cross-dissolve convention; eased curves belong
        # to the motion channels, not the blend.
        self._crossfade.setEasingCurve(QEasingCurve.Type.Linear)
        self._crossfade.valueChanged.connect(lambda _value: self.update())
        self._crossfade.finished.connect(self._finish_crossfade)

    @property
    def crossfade_animation(self) -> QVariantAnimation:
        return self._crossfade

    def setPixmap(self, pixmap) -> None:  # noqa: N802 - Qt override
        # Direct calls keep plain-QLabel semantics: any in-flight dissolve is
        # dropped so a stale layer can never outlive the caller's intent.
        self._crossfade.stop()
        self._crossfade_old = None
        self._identity = None
        self._select_eye_rig(None)
        super().setPixmap(pixmap)

    def set_eye_rigs(self, rigs: dict[str, str]) -> None:
        self._eye_rigs = {str(Path(key).absolute()): value for key, value in rigs.items()}
        self._eye_key = None
        self._select_eye_rig(self._identity)

    def _select_eye_rig(self, identity: str | None) -> None:
        path = self._eye_rigs.get(identity) if identity else None
        key = (identity, path)
        if key == self._eye_key:
            return
        self._eye_key = key
        self._eye_motion = None
        if path and identity:
            try:
                self._eye_motion = EyeMotion(Path(path))
            except (OSError, ValueError) as exc:
                logger.warning("event=eye_rig_load_failed path=%r error=%s", path, exc)
        self._sync_eye_timer()
        self.update()

    def _sync_eye_timer(self) -> None:
        if self._eye_motion is not None and self.isVisible() and not self._motion_active():
            self._eye_timer.start()
        else:
            self._eye_timer.stop()

    def _advance_eyes(self) -> None:
        pixmap = self.pixmap()
        if self._eye_motion is None or pixmap is None or pixmap.isNull():
            return
        if self._eye_motion.advance(time.monotonic() * 1000, self.mapFromGlobal(QCursor.pos()),
                                    self._placement_rect(pixmap), self.contentsRect()):
            self.update()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._sync_eye_timer()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._eye_timer.stop()
        super().hideEvent(event)

    def set_sprite(self, pixmap: QPixmap | None, identity: str | None) -> None:
        """Animated entry: dissolve on identity change, swap instantly otherwise.

        A rescale of the same image (window resize) keeps ``identity`` and must
        never dissolve; ``pixmap()`` returns the new target immediately either
        way, so the click-through mask tracks the latest sprite from frame one.
        """
        previous = self.pixmap()
        previous_identity = self._identity
        same_identity = identity is not None and identity == self._identity
        self._identity = identity
        self._select_eye_rig(identity)
        if same_identity:
            # Same image (rescale/relayout): never dissolve, and never snap an
            # in-flight dissolve either -- only the target pixmap is refreshed.
            super().setPixmap(pixmap)
            return
        preempted = self._crossfade.state() != QVariantAnimation.State.Stopped
        self._crossfade.stop()
        if _is_hand_pose_change(previous_identity, identity):
            # A hand-pose variant changes the whole silhouette. Dissolving two
            # incompatible arm shapes reads as a double body, so cut on the beat.
            self._crossfade_old = None
            super().setPixmap(pixmap)
            self._sync_eye_timer()
            self.update()
            return
        if (
            previous is None
            or previous.isNull()
            or pixmap is None
            or pixmap.isNull()
        ):
            self._crossfade_old = None
            super().setPixmap(pixmap)
            self._sync_eye_timer()
            return
        # Latest-wins: a half-faded previous layer is dropped outright -- the
        # prior target becomes the fading layer. One animation, never a queue.
        if preempted:
            logger.debug("event=sprite_crossfade_preempted")
        self._crossfade_old = previous
        super().setPixmap(pixmap)
        self._last_motion_paint_ms = None
        self._slow_motion_frames = 0
        self._crossfade.start()
        self._sync_eye_timer()

    def finish_sprite_transition(self) -> None:
        """Snap the current target before a higher-priority presence transit."""
        self._crossfade.stop()
        self._crossfade_old = None
        self._sync_eye_timer()
        self.update()

    def _finish_crossfade(self) -> None:
        self._crossfade_old = None
        self._sync_eye_timer()
        if self._slow_motion_frames:
            logger.debug(
                "event=sprite_crossfade_done slow_frames=%s",
                self._slow_motion_frames,
            )
        self.update()

    # -- presence transit channel (driven by CharacterPresenceAnimator) ------

    def set_presence_frame(self, frame: PresenceFrame) -> None:
        if frame == self._presence_frame:
            return
        self._presence_frame = frame
        self._sync_eye_timer()
        self.update()

    def _motion_active(self) -> bool:
        return (
            self._crossfade_old is not None
            or self._presence_frame != REST_FRAME
        )

    def _tinted(self, pixmap: QPixmap, color: QColor) -> QPixmap:
        """Flat-colour silhouette of ``pixmap`` for chromatic ghosting."""
        key = (pixmap.cacheKey(), color.rgba())
        cached = self._tint_cache.get(key)
        if cached is not None:
            return cached
        tinted = QPixmap(pixmap.size())
        tinted.setDevicePixelRatio(pixmap.devicePixelRatio())
        tinted.fill(Qt.GlobalColor.transparent)
        painter = QPainter(tinted)
        painter.drawPixmap(0, 0, pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(tinted.rect(), color)
        painter.end()
        if len(self._tint_cache) >= _TINT_CACHE_CAP:
            self._tint_cache.clear()
        self._tint_cache[key] = tinted
        return tinted

    def _placement_rect(self, pixmap: QPixmap) -> QRect:
        """QLabel's aligned pixmap rect (DPR-aware), for identical placement."""
        rect = self.contentsRect()
        size = pixmap.deviceIndependentSize().toSize()
        align = self.alignment()
        x = rect.x()
        if align & Qt.AlignmentFlag.AlignHCenter:
            x += (rect.width() - size.width()) // 2
        elif align & Qt.AlignmentFlag.AlignRight:
            x += rect.width() - size.width()
        y = rect.y()
        if align & Qt.AlignmentFlag.AlignVCenter:
            y += (rect.height() - size.height()) // 2
        elif align & Qt.AlignmentFlag.AlignBottom:
            y += rect.height() - size.height()
        return QRect(x, y, size.width(), size.height())

    def _draw_sliced_sprite(
        self,
        painter: QPainter,
        rect: QRect,
        pixmap: QPixmap,
        frame: PresenceFrame,
    ) -> None:
        """Glitch bands: a minority of horizontal strips shift sideways."""
        ratio = pixmap.devicePixelRatio()
        band_height = max(1, rect.height() // _SLICE_BANDS)
        y = rect.y()
        band = 0
        while y < rect.y() + rect.height():
            height = min(band_height, rect.y() + rect.height() - y)
            roll = _hash01(band * 7.13 + frame.slice_seed * 3.71 + 1.7)
            dx = round((roll * 2.0 - 1.0) * frame.slice_amp) if roll > 0.55 else 0
            source_y = (y - rect.y()) * ratio
            painter.drawPixmap(
                QRectF(rect.x() + dx, y, rect.width(), height),
                pixmap,
                QRectF(0.0, source_y, rect.width() * ratio, height * ratio),
            )
            y += height
            band += 1

    def _draw_sparks(
        self, painter: QPainter, rect: QRect, frame: PresenceFrame
    ) -> None:
        """Rising ice-cyan motes around the light line, stateless per frame."""
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        center_x = rect.x() + rect.width() / 2.0
        base_y = rect.y() + rect.height() * 0.92
        span = rect.height() * 0.55
        for index in range(_SPARK_COUNT):
            speed = _hash01(index * 1.31 + 0.7)
            drift = _hash01(index * 2.17 + 1.3)
            side = _hash01(index * 3.71 + 2.9)
            travel = (frame.spark_phase * (0.6 + 0.8 * speed) + drift) % 1.0
            alpha = frame.spark_alpha * (1.0 - travel) * (0.4 + 0.6 * speed)
            if alpha <= 0.01:
                continue
            color = QColor(LINE_CORE_COLOR)
            color.setAlphaF(min(1.0, alpha))
            size = 1.5 + 2.5 * speed
            painter.fillRect(
                QRectF(
                    center_x + (side * 2.0 - 1.0) * (10.0 + 18.0 * drift),
                    base_y - travel * span,
                    size,
                    size,
                ),
                color,
            )
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceOver
        )

    def _draw_flare(
        self, painter: QPainter, rect: QRect, frame: PresenceFrame
    ) -> None:
        """Horizontal light burst at the line's waist (body <-> line moments)."""
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        center_x = rect.x() + rect.width() / 2.0
        center_y = rect.y() + rect.height() * 0.5
        width = rect.width() * max(0.05, frame.flare_width_frac)
        for height, alpha_mul in ((9.0, 0.18), (3.0, 0.6)):
            alpha = min(1.0, frame.flare_alpha * alpha_mul)
            solid = QColor(LINE_CORE_COLOR)
            solid.setAlphaF(alpha)
            clear = QColor(LINE_CORE_COLOR)
            clear.setAlphaF(0.0)
            gradient = QLinearGradient(
                center_x - width / 2.0, 0.0, center_x + width / 2.0, 0.0
            )
            gradient.setColorAt(0.0, clear)
            gradient.setColorAt(0.5, solid)
            gradient.setColorAt(1.0, clear)
            painter.fillRect(
                QRectF(center_x - width / 2.0, center_y - height / 2.0, width, height),
                QBrush(gradient),
            )
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceOver
        )

    def _draw_scan_band(
        self, painter: QPainter, rect: QRect, frame: PresenceFrame
    ) -> None:
        """Bright horizontal band sweeping the body, confined to its alpha."""
        band_height = rect.height() * 0.10
        center_y = rect.y() + rect.height() * _clampf(frame.scan_pos)
        solid = QColor(LINE_CORE_COLOR)
        solid.setAlphaF(min(1.0, frame.scan_alpha))
        clear = QColor(LINE_CORE_COLOR)
        clear.setAlphaF(0.0)
        gradient = QLinearGradient(
            0.0, center_y - band_height / 2.0, 0.0, center_y + band_height / 2.0
        )
        gradient.setColorAt(0.0, clear)
        gradient.setColorAt(0.5, solid)
        gradient.setColorAt(1.0, clear)
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceAtop
        )
        painter.fillRect(
            QRectF(rect.x(), center_y - band_height / 2.0, rect.width(), band_height),
            QBrush(gradient),
        )
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceOver
        )

    def _draw_ground_ring(
        self, painter: QPainter, rect: QRect, frame: PresenceFrame
    ) -> None:
        """Expanding energy ring on the ground plane at her feet."""
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        center = QPointF(
            rect.x() + rect.width() / 2.0,
            rect.y() + rect.height() * 0.97,
        )
        radius_x = max(2.0, frame.ring_radius * rect.width() * 0.55)
        radius_y = max(2.0, radius_x * 0.22)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for pen_width, alpha_mul in ((5.0, 0.25), (2.0, 1.0)):
            color = QColor(LINE_CORE_COLOR)
            color.setAlphaF(min(1.0, frame.ring_alpha * alpha_mul))
            painter.setPen(QPen(color, pen_width))
            painter.drawEllipse(center, radius_x, radius_y)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceOver
        )

    def _draw_streaks(
        self, painter: QPainter, rect: QRect, frame: PresenceFrame
    ) -> None:
        """Thin vertical energy streams rising around the body."""
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        center_x = rect.x() + rect.width() / 2.0
        for index in range(_STREAK_COUNT):
            lane = _hash01(index * 4.7 + 0.3)
            pace = _hash01(index * 1.9 + 2.2)
            travel = (frame.streak_phase * (0.5 + 0.7 * pace) + lane) % 1.0
            alpha = (
                frame.streak_alpha * (0.35 + 0.5 * pace) * (1.0 - travel * 0.6)
            )
            if alpha <= 0.01:
                continue
            segment = rect.height() * (0.12 + 0.15 * pace)
            bottom = rect.y() + rect.height() - travel * rect.height() * 0.8
            x = center_x + (lane * 2.0 - 1.0) * rect.width() * 0.38
            solid = QColor(LINE_CORE_COLOR)
            solid.setAlphaF(min(1.0, alpha))
            clear = QColor(LINE_CORE_COLOR)
            clear.setAlphaF(0.0)
            gradient = QLinearGradient(0.0, bottom - segment, 0.0, bottom)
            gradient.setColorAt(0.0, clear)
            gradient.setColorAt(0.55, solid)
            gradient.setColorAt(1.0, clear)
            painter.fillRect(
                QRectF(x, bottom - segment, 1.5, segment), QBrush(gradient)
            )
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceOver
        )

    def _draw_burst(
        self, painter: QPainter, rect: QRect, frame: PresenceFrame
    ) -> None:
        """Radial spark burst from the line's waist (the snap moment)."""
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        center_x = rect.x() + rect.width() / 2.0
        waist_y = rect.y() + rect.height() * 0.5
        scale = rect.width() / 300.0
        for index in range(_BURST_COUNT):
            angle = _hash01(index * 2.39 + 0.5) * math.tau
            reach = (24.0 + 56.0 * _hash01(index * 3.1 + 1.1)) * scale
            pace = 0.6 + 0.4 * _hash01(index * 5.3 + 0.9)
            travel = _clampf(frame.burst_phase * pace)
            alpha = frame.burst_alpha * (1.0 - travel) * (0.4 + 0.6 * pace)
            if alpha <= 0.01:
                continue
            color = QColor(LINE_CORE_COLOR)
            color.setAlphaF(min(1.0, alpha))
            size = 1.5 + 2.0 * pace
            painter.fillRect(
                QRectF(
                    center_x + math.cos(angle) * reach * travel,
                    waist_y + math.sin(angle) * reach * travel * 0.45,
                    size,
                    size,
                ),
                color,
            )
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceOver
        )

    def _draw_transit_line(
        self, painter: QPainter, rect: QRect, frame: PresenceFrame
    ) -> None:
        """Additive ice-cyan light line: soft halo around a bright core."""
        center_x = rect.x() + rect.width() / 2.0
        height = rect.height() * frame.line_len_frac
        top = rect.y() + rect.height() * frame.line_y_frac
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        for width, alpha_mul in ((12.0, 0.10), (6.0, 0.30), (2.5, 1.0)):
            alpha = min(1.0, frame.line_alpha * alpha_mul)
            solid = QColor(LINE_CORE_COLOR)
            solid.setAlphaF(alpha)
            clear = QColor(LINE_CORE_COLOR)
            clear.setAlphaF(0.0)
            gradient = QLinearGradient(0.0, top, 0.0, top + height)
            gradient.setColorAt(0.0, clear)
            gradient.setColorAt(0.12, solid)
            gradient.setColorAt(0.88, solid)
            gradient.setColorAt(1.0, clear)
            painter.fillRect(
                QRectF(center_x - width / 2.0, top, width, height),
                QBrush(gradient),
            )
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceOver
        )

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        pixmap = self.pixmap()
        if not self._motion_active() or pixmap is None or pixmap.isNull():
            super().paintEvent(event)
            if self._eye_motion is not None and pixmap is not None and not pixmap.isNull():
                painter = QPainter(self)
                try:
                    self._eye_motion.paint(painter, self._placement_rect(pixmap))
                finally:
                    painter.end()
            return
        now_ms = time.monotonic() * 1000.0
        if (
            self._last_motion_paint_ms is not None
            and now_ms - self._last_motion_paint_ms > _SLOW_FRAME_MS
        ):
            self._slow_motion_frames += 1
        self._last_motion_paint_ms = now_ms

        frame = self._presence_frame
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            rect = self._placement_rect(pixmap)
            if frame.sprite_alpha > 0.0:
                painter.save()
                if frame.sx != 1.0:
                    # Squash toward the body's vertical centre line.
                    center_x = rect.x() + rect.width() / 2.0
                    painter.translate(center_x, 0.0)
                    painter.scale(frame.sx, 1.0)
                    painter.translate(-center_x, 0.0)
                if frame.rgb_dx > 0.25:
                    ghost_dx = round(frame.rgb_dx)
                    painter.setOpacity(RGB_GHOST_OPACITY * frame.sprite_alpha)
                    painter.drawPixmap(
                        rect.translated(-ghost_dx, 0),
                        self._tinted(pixmap, RGB_GHOST_CYAN),
                    )
                    painter.drawPixmap(
                        rect.translated(ghost_dx, 0),
                        self._tinted(pixmap, RGB_GHOST_MAGENTA),
                    )
                # New target below at FULL opacity, fading old layer above:
                # overlap alpha stays 1.0 for the whole dissolve, so the
                # transparent desktop never shows through her mid-fade.
                painter.setOpacity(frame.sprite_alpha)
                if frame.slice_amp > 0.5:
                    self._draw_sliced_sprite(painter, rect, pixmap, frame)
                else:
                    painter.drawPixmap(rect, pixmap)
                old = self._crossfade_old
                if old is not None and not old.isNull():
                    progress = float(self._crossfade.currentValue() or 0.0)
                    painter.setOpacity(
                        max(0.0, 1.0 - progress) * frame.sprite_alpha
                    )
                    painter.drawPixmap(self._placement_rect(old), old)
                if frame.scan_alpha > 0.0:
                    # Bright scan band sweeping the silhouette (SourceAtop).
                    painter.setOpacity(1.0)
                    self._draw_scan_band(painter, rect, frame)
                if frame.body_glow > 0.0:
                    # Silhouette tint: docking bloom or snap white-out.
                    painter.setOpacity(1.0)
                    glow = QColor(BODY_GLOW_COLOR)
                    glow.setAlphaF(min(1.0, frame.body_glow))
                    painter.setCompositionMode(
                        QPainter.CompositionMode.CompositionMode_SourceAtop
                    )
                    painter.fillRect(self.rect(), glow)
                    painter.setCompositionMode(
                        QPainter.CompositionMode.CompositionMode_SourceOver
                    )
                painter.restore()
            if frame.echo_alpha > 0.0:
                # Ghost silhouette: residual signal / hologram preview.
                painter.setOpacity(min(1.0, frame.echo_alpha))
                echo_dx = 2 if frame.slice_seed % 2 == 0 else -2
                painter.drawPixmap(
                    rect.translated(echo_dx, 0),
                    self._tinted(pixmap, RGB_GHOST_CYAN),
                )
                painter.setOpacity(1.0)
            if frame.ring_alpha > 0.0:
                self._draw_ground_ring(painter, rect, frame)
            if frame.streak_alpha > 0.0:
                self._draw_streaks(painter, rect, frame)
            if frame.line_alpha > 0.0:
                self._draw_transit_line(painter, rect, frame)
            if frame.flare_alpha > 0.0:
                self._draw_flare(painter, rect, frame)
            if frame.burst_alpha > 0.0:
                self._draw_burst(painter, rect, frame)
            if frame.spark_alpha > 0.0:
                self._draw_sparks(painter, rect, frame)
        finally:
            painter.end()


__all__ = [
    "CROSSFADE_MS",
    "REST_FRAME",
    "CharacterSpriteView",
    "PresenceFrame",
]
