"""Draw calibrated eye patches inside the existing transparent sprite widget."""

from __future__ import annotations

import math
import random
import time
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QPainterPath, QPixmap, QRegion

from spica.core.character_manifest import package_file
from spica.core.eye_rig import EyeRegion, load_eye_rig


def _smooth(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3 - 2 * value)


def _contour(points, x: float, component: int) -> float:
    index = 0
    while index < len(points) - 2 and x > points[index + 1][0]:
        index += 1
    a, b = points[index], points[index + 1]
    before, after = points[max(0, index - 1)], points[min(len(points) - 1, index + 2)]
    t = max(0.0, min(1.0, (x - a[0]) / (b[0] - a[0])))
    m0 = (b[component] - before[component]) / (b[0] - before[0]) * (b[0] - a[0])
    m1 = (after[component] - a[component]) / (after[0] - a[0]) * (b[0] - a[0])
    return ((2*t**3 - 3*t*t + 1)*a[component] + (t**3 - 2*t*t + t)*m0
            + (-2*t**3 + 3*t*t)*b[component] + (t**3 - t*t)*m1)


def _surface(width: int, height: int) -> QImage:
    image = QImage(width * 2, height * 2, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    return image


def _painter(image: QImage) -> QPainter:
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.scale(2, 2)
    return painter


class _Eye:
    def __init__(self, spec: EyeRegion, opened: QImage, closed: QImage):
        self.spec = spec
        x, y, w, h = spec.bounds
        self.source, self.closed = _surface(w, h), _surface(w, h)
        for source, target in ((opened, self.source), (closed, self.closed)):
            painter = _painter(target)
            painter.drawImage(QRectF(0, 0, w, h), source, QRectF(x, y, w, h))
            painter.end()
        self.horizontal, self.ball, self.output = (_surface(w, h) for _ in range(3))
        self.left, self.right = spec.contour[0][0] - x, spec.contour[-1][0] - x
        self.iris_left, self.iris_right = (v - x for v in spec.iris)
        self.columns = []
        for index in range(w * 2):
            px = index / 2
            top = _contour(spec.contour, x + px, 1) - y
            bottom = _contour(spec.contour, x + px, 2) - y
            inside = self.left < px < self.right
            resting = _contour(spec.closed_line, x + px, 1) - y
            lash = top - (_contour(spec.contour, x + px, 3) - y)
            self.columns.append((px, top, bottom, resting, lash, inside))
        interior = [column for column in self.columns if column[-1]]
        self.aperture = QPainterPath()
        self.aperture.moveTo(interior[0][0], interior[0][1])
        for column in interior:
            self.aperture.lineTo(column[0], column[1])
        for column in reversed(interior):
            self.aperture.lineTo(column[0], column[2])
        self.aperture.closeSubpath()
        self.last = None

    def render(self, openness: float, dx: float, dy: float) -> QImage:
        key = (round(openness * 500), round(dx * 25), round(dy * 25))
        if key == self.last:
            return self.output
        self.last = key
        _, _, w, h = self.spec.bounds
        full = QRectF(0, 0, w, h)
        left, right = self.left, self.right
        iris_left, iris_right = self.iris_left, self.iris_right
        painter = _painter(self.horizontal)
        painter.drawImage(full, self.source)
        # Translate every row of the iris equally. Only the surrounding white
        # stretches; varying dx with lid height bends the upper iris.
        for sx, sw, target_x, target_w in (
            (left, iris_left - left, left, iris_left - left + dx),
            (iris_left, iris_right - iris_left, iris_left + dx, iris_right - iris_left),
            (iris_right, right - iris_right, iris_right + dx, right - iris_right - dx),
        ):
            painter.drawImage(QRectF(target_x, 0, target_w, h), self.source,
                              QRectF(sx * 2, 0, sw * 2, h * 2))
        painter.end()
        painter = _painter(self.ball)
        painter.drawImage(full, self.horizontal)
        for px, top, bottom, _, _, inside in self.columns:
            if not inside or bottom - top < 4:
                continue
            middle = (top + bottom) / 2
            shift = dy * _smooth((px - left) / 22) * _smooth((right - px) / 22)
            shift = max(top - middle + 0.01, min(bottom - middle - 0.01, shift))
            painter.drawImage(QRectF(px, top, .5, middle - top + shift), self.horizontal,
                              QRectF(px * 2, top * 2, 1, (middle - top) * 2))
            painter.drawImage(QRectF(px, middle + shift, .5, bottom - middle - shift), self.horizontal,
                              QRectF(px * 2, middle * 2, 1, (bottom - middle) * 2))
        painter.end()
        painter = _painter(self.output)
        painter.drawImage(full, self.source)
        if openness > .9999:
            painter.setClipPath(self.aperture)
            painter.drawImage(full, self.ball)
        else:
            painter.drawImage(full, self.closed)
            for px, top, bottom, resting, lash, inside in self.columns:
                if not inside:
                    continue
                upper = resting + (top - resting) * openness
                lower = resting + (bottom - resting) * openness
                lash_height = lash * (.55 + .45 * openness)
                trim = 2.5 * (1 - openness)
                painter.setOpacity(_smooth(openness / .18))
                painter.drawImage(QRectF(px, resting - 10, .5, 19), self.closed,
                                  QRectF(px * 2, (resting + 10) * 2, 1, 4))
                if lash > trim:
                    painter.drawImage(QRectF(px, upper - lash_height, .5, lash_height), self.source,
                                      QRectF(px * 2, (top - lash) * 2, 1, (lash - trim) * 2))
                painter.setOpacity(1)
                if openness > .035 and lower - upper > .01:
                    painter.drawImage(QRectF(px, upper, .5, lower - upper), self.ball,
                                      QRectF(px * 2, upper * 2, 1, (lower - upper) * 2))
        painter.end()
        return self.output


class EyeMotion:
    def __init__(self, path: Path):
        self.rig = load_eye_rig(path)
        opened_path = package_file(path.parent, self.rig.textures.open)
        opened = QImage(str(opened_path))
        closed = QImage(str(package_file(path.parent, self.rig.textures.closed)))
        expected = (self.rig.canvas.width, self.rig.canvas.height)
        if any(image.isNull() or (image.width(), image.height()) != expected for image in (opened, closed)):
            raise ValueError("eye animation artwork is missing or has changed size")
        # Match OverlayWindow's alpha crop exactly, before any display scaling.
        self.crop = QRegion(QPixmap.fromImage(opened).mask()).boundingRect()
        if self.crop.isEmpty():
            raise ValueError("eye animation artwork is empty")
        self.eyes = [_Eye(eye, opened, closed) for eye in self.rig.eyes]
        self.gaze = [0.0, 0.0]
        self.openness = 1.0
        self.last_time = time.monotonic() * 1000
        self.next_blink = self.last_time + self.rig.blink.initial_delay_ms
        self.blink_start = -math.inf
        self._last_frame = None

    def advance(self, now: float, cursor: QPoint, placement: QRect, viewport: QRect) -> bool:
        rig = self.rig
        eye_x = placement.x() + (rig.gaze.origin[0] - self.crop.x()) * placement.width() / self.crop.width()
        eye_y = placement.y() + (rig.gaze.origin[1] - self.crop.y()) * placement.height() / self.crop.height()
        tx = (cursor.x() - eye_x) / max(140, viewport.width() * .48)
        ty = (cursor.y() - eye_y) / max(120, viewport.height() * .32)
        length = max(1, math.hypot(tx, ty))
        easing = 1 - math.exp(-max(0, min(64, now - self.last_time)) / rig.gaze.smoothing_ms)
        self.last_time = now
        self.gaze[0] += (tx / length - self.gaze[0]) * easing
        self.gaze[1] += (ty / length - self.gaze[1]) * easing
        if now >= self.next_blink:
            self.blink_start = now
            self.next_blink = now + rig.blink.duration_ms + random.uniform(*rig.blink.interval_ms)
        progress = (now - self.blink_start) / rig.blink.duration_ms
        if not 0 <= progress < 1:
            self.openness = 1.0
        elif progress < .33:
            self.openness = 1 - _smooth(progress / .33)
        elif progress < .45:
            self.openness = 0.0
        else:
            self.openness = _smooth((progress - .45) / .55)
        dx, dy = self.offset
        frame = (round(dx * 25), round(dy * 25), round(self.openness * 500))
        changed, self._last_frame = frame != self._last_frame, frame
        return changed

    @property
    def offset(self) -> tuple[float, float]:
        x, y = self.rig.gaze.maximum_offset
        strength = self.rig.gaze.strength
        return self.gaze[0] * x * strength, self.gaze[1] * y * strength

    def paint(self, painter: QPainter, placement: QRect) -> None:
        dx, dy = self.offset
        if self.openness > .9999 and abs(dx) + abs(dy) <= .005:
            return
        sx, sy = placement.width() / self.crop.width(), placement.height() / self.crop.height()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        for eye in self.eyes:
            x, y, w, h = eye.spec.bounds
            target = QRectF(placement.x() + (x - self.crop.x()) * sx,
                            placement.y() + (y - self.crop.y()) * sy, w * sx, h * sy)
            painter.drawImage(target, eye.render(self.openness, dx, dy))
