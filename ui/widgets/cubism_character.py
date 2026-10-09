"""Optional native Cubism surface. Imported only for a Cubism character pack."""

from __future__ import annotations

import logging
import math
import time
from pathlib import Path

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QImage, QPixmap, QRegion, QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QLabel

from spica.core.cubism import CubismModel, load_bindings, load_director, read_json
from spica.core.cubism_motion import CubismPlayback, ParameterEffect
from spica.core.character_manifest import package_file
from ui.widgets.character_sprite import CharacterSpriteView

logger = logging.getLogger(__name__)
_native_initialized = False


class CubismCanvas(QOpenGLWidget):
    ready = Signal()
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        fmt = QSurfaceFormat()
        fmt.setAlphaBufferSize(8)
        fmt.setDepthBufferSize(24)
        fmt.setStencilBufferSize(8)
        fmt.setSwapInterval(1)
        self.setFormat(fmt)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._native = None
        self.model = None
        self.spec: CubismModel | None = None
        self._root: Path | None = None
        self._model_path: Path | None = None
        self._loaded_path: Path | None = None
        self._params: dict[str, int] = {}
        self._last_frame = 0.0
        self._gaze = [0.0, 0.0]
        self._mouth = 0.0
        self.mouth_level = 0.0
        self.mouth_provider = None
        self.hold_seconds = .8
        self.idle_return_seconds = 20
        self._playback = None
        self._effects = {}
        self._effect = None
        self._expression = None
        self._elapsed = 0.0
        self._motion_id: str | None = None
        self._idle_motion: str | None = None
        self._idle_return_at: float | None = None
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self.update)

    def set_model(self, root: Path, spec: CubismModel):
        path = package_file(root, spec.model)
        if path == self._model_path and spec == self.spec:
            return
        if root != self._root:
            self._playback = None
        self._root, self.spec, self._model_path = root, spec, path
        if self.isValid():
            self.makeCurrent()
            try:
                self._load_model()
            finally:
                self.doneCurrent()
        self.update()

    def initializeGL(self):  # noqa: N802
        global _native_initialized
        try:
            import live2d.v3 as native

            if not _native_initialized:
                native.init()
                native.enableLog(False)
                _native_initialized = True
            self._native = native
            native.glInit()
            self.context().aboutToBeDestroyed.connect(self.release)
            self._load_model()
        except Exception as exc:
            self._fail(exc)

    def _load_model(self):
        if self._native is None or self.spec is None or self._model_path is None:
            return
        new = None
        try:
            # Costumes may share assets while changing parameters and motions.
            new = self.model if self._loaded_path == self._model_path else None
            if new is None:
                new = self._native.Model()
                refs = read_json(self._model_path)["FileReferences"]
                moc = package_file(self._model_path.parent, refs["Moc"])
                if not new.HasMocConsistencyFromFile(str(moc)):
                    raise ValueError("模型 MOC 完整性检查失败")
                new.LoadModelJson(str(self._model_path))
                new.CreateRenderer(2)
                new.SetAutoBlink(False)
                new.SetAutoBreath(False)
            new.Resize(max(1, self.width()), max(1, self.height()))
            new.SetScale(self.spec.scale)
            new.SetOffset(*self.spec.offset)
            ids = new.GetParameterIds()
            for name in (list(self.spec.parameters.fixed) + self.spec.parameters.eyes + self.spec.parameters.mouth
                         + [drive.id for drive in self.spec.parameters.gaze_x + self.spec.parameters.gaze_y]):
                if name not in ids:
                    raise ValueError(f"模型不存在指定参数：{name}")
            effects = {key: ParameterEffect(read_json(package_file(self._root, path)))
                       for key, path in self.spec.effects.items()}
            if self.model is not None and self.model is not new:
                self.model.DestroyRenderer()
            self.model, new = new, None
            self._params = {key: index for index, key in enumerate(self.model.GetParameterIds())}
            self._effects = effects
            if self._playback is None:
                self._playback = CubismPlayback(self.spec, self.hold_seconds)
            else:
                self._playback.set_model(self.spec)
            self._loaded_path = self._model_path
            self._gaze[:] = [0, 0]
            self._last_frame = time.monotonic()
            self.reset_performance()
            self._timer.start()
            self.ready.emit()
        except Exception as exc:
            if new is not None and new is not self.model:
                new.DestroyRenderer()
            self._fail(exc)

    def _fail(self, exc):
        self._timer.stop()
        if self.model is not None:
            self.model.DestroyRenderer()
            self.model = None
        self._loaded_path = None
        logger.warning("event=cubism_load_failed error=%s", exc)
        self.failed.emit(f"Live2D 暂不可用，已显示备用立绘：{exc}")

    def resizeGL(self, width, height):  # noqa: N802
        if self.model is not None:
            self.model.Resize(max(1, width), max(1, height))

    def start_motion(self, motion_id: str):
        if self.model is None or self.spec is None:
            return
        motion = self.spec.motions.get(motion_id)
        if motion is None:
            return
        if self._motion_id == motion_id and not self.model.IsMotionFinished():
            return
        self._motion_id = motion_id
        self.model.StartMotion(motion.group, motion.index, 3)

    def reset_performance(self, *, preserve_expression=False):
        self.mouth_level = self._mouth = 0
        self._motion_id = None
        self._effect = None
        self._elapsed = 0
        self._idle_return_at = None
        if not preserve_expression:
            self._expression = None
            self._idle_motion = self.spec.idle if self.spec else None
        if self._playback is not None:
            self._playback.reset()
        if self.model is None or self.spec is None:
            return
        self.model.StopAllMotions()
        self.model.ResetAllParameters()
        if not preserve_expression:
            self.model.ResetExpressions()
            if self.spec.neutral_expression:
                self.model.SetExpression(self.spec.neutral_expression)
                self._expression = self.spec.neutral_expression
        self.start_motion(self._idle_motion or self.spec.idle)

    def finish_performance(self):
        """End a spoken turn without cutting its gesture or erasing its mood."""
        self.mouth_level = 0
        self._effect = None
        if self._playback is not None:
            expression_id = self._playback.finish()
            if expression_id is not None and self.model is not None:
                self._set_expression(expression_id)
                if self._motion_id in self.spec.motions and self.spec.motions[self._motion_id].kind == "idle":
                    self.start_motion(self._idle_motion or self.spec.idle)
        if self._idle_return_at is None:
            self._idle_return_at = time.monotonic() + self.idle_return_seconds

    def _advance_idle(self, now):
        if self._idle_return_at is not None and now >= self._idle_return_at:
            self._idle_return_at = None
            self._idle_motion = self.spec.idle
            if self.spec.neutral_expression:
                self.model.SetExpression(self.spec.neutral_expression)
            else:
                self.model.ResetExpressions()
            self._expression = self.spec.neutral_expression
            self.start_motion(self._idle_motion)
        if self.model.IsMotionFinished():
            self.start_motion(self._idle_motion or self.spec.idle)

    def apply_cue(self, cue):
        self._idle_return_at = None
        if self._playback is not None:
            self._playback.queue(cue)

    def _set_expression(self, expression_id):
        expression = self.spec.expressions.get(expression_id, self._expression)
        if expression != self._expression:
            if expression:
                self.model.SetExpression(expression)
            else:
                self.model.ResetExpressions()
            self._expression = expression
        if expression_id in self.spec.expressions:
            self._idle_motion = self.spec.idle_motions.get(expression_id, self.spec.idle)

    def _advance_director(self, now):
        cue = self._playback.advance(now) if self._playback else None
        if cue is None:
            return
        self._set_expression(cue.get("expression"))
        if cue.get("motion"):
            self.start_motion(cue["motion"])
        elif self._motion_id in self.spec.motions and self.spec.motions[self._motion_id].kind == "idle":
            self.start_motion(self._idle_motion or self.spec.idle)
        effect = self._effects.get(cue.get("effect"))
        self._effect = (effect, now) if effect else None

    def paintGL(self):  # noqa: N802
        if self._native is None:
            return
        self._native.clearBuffer()
        if self.model is None or self.spec is None:
            return
        now = time.monotonic()
        dt = max(0, min(.05, now - self._last_frame))
        self._last_frame = now
        self._elapsed += dt
        model = self.model
        self._advance_director(now)
        self._advance_idle(now)
        model.LoadParameters()
        model.UpdateMotion(dt)
        model.SaveParameters()
        model.UpdateExpression(dt)
        if self.spec.parameters.blink == "auto":
            phase = self._elapsed % 4.3
            closure = max(0, 1 - abs(phase - .14) / .14) if phase < .28 else 0
            for key in self.spec.parameters.eyes:
                index = self._params[key]
                model.SetParameterValue(index, model.GetParameterValue(index) * (1 - closure))
        point = self.mapFromGlobal(QCursor.pos())
        target = [max(-1, min(1, (point.x() / max(1, self.width()) - .5) * 2)),
                  max(-1, min(1, (point.y() / max(1, self.height()) - .3) * 2))]
        blend = 1 - math.exp(-dt * 7)
        for axis in (0, 1):
            self._gaze[axis] += (target[axis] - self._gaze[axis]) * blend
        for axis, drives in enumerate((self.spec.parameters.gaze_x, self.spec.parameters.gaze_y)):
            for drive in drives:
                if drive.id in self._params:
                    model.AddParameterValue(self._params[drive.id], self._gaze[axis] * drive.scale)
        level = self.mouth_provider() if self.mouth_provider is not None else self.mouth_level
        self._mouth += (max(0, min(1, level)) - self._mouth) * (1 - math.exp(-dt * 32))
        for key in self.spec.parameters.mouth:
            if key in self._params:
                model.SetParameterValue(self._params[key], self._mouth)
        if self.spec.physics:
            model.UpdatePhysics(dt)
        model.UpdatePose(dt)
        if self._effect is not None:
            effect, started = self._effect
            weight = effect.weight(now - started)
            for key, value in effect.values(now - started).items():
                if key in self._params:
                    index = self._params[key]
                    model.SetParameterValue(index, model.GetParameterValue(index) * (1 - weight) + value)
            if now - started >= effect.duration:
                self._effect = None
        for key, value in self.spec.parameters.fixed.items():
            model.SetParameterValue(self._params[key], value)
        model.Draw()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._last_frame = time.monotonic()
        if self.model is not None:
            self._timer.start()
        QTimer.singleShot(0, self._check_context)

    def _check_context(self):
        if self.isVisible() and not self.isValid():
            self._fail(RuntimeError("无法创建 OpenGL 上下文，请检查显卡驱动"))

    def hideEvent(self, event):  # noqa: N802
        self._timer.stop()
        super().hideEvent(event)

    def release(self):
        self._timer.stop()
        if self.model is not None:
            self.makeCurrent()
            self.model.DestroyRenderer()
            self.model = None
            self.doneCurrent()
        self._loaded_path = None


class CubismCharacterView(CharacterSpriteView):
    """Keep the sprite display contract while drawing only Cubism in steady state.

    A failed or missing native runtime displays the pack's static preview.
    No graphics effect captures the GL child on each animation tick.
    """

    status_changed = Signal(str)
    shape_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.canvas = CubismCanvas(self)
        self.canvas.ready.connect(self._show_native)
        self.canvas.failed.connect(self._show_fallback)
        self._bindings = None
        self._root = None
        self._costumes = {}
        self._native_ready = False
        self._shape = QRegion()
        self._shape_at = 0.0
        self._reading_shape = False
        self._pending_cue = None
        self.canvas.frameSwapped.connect(self._refresh_shape)

    def configure(self, config):
        self._root = Path(config["package_root"])
        relative = Path(config["cubism"]).relative_to(self._root).as_posix()
        self._bindings = load_bindings(self._root, relative)
        director = load_director(self._root, self._bindings)
        self.canvas.hold_seconds = director.hold_seconds
        self.canvas.idle_return_seconds = director.idle_return_seconds
        self._costumes = {str(Path(image).absolute()): costume
                          for costume, expressions in config["sprite_map"].items()
                          for images in expressions.values() for image in images}

    def set_sprite(self, pixmap, identity):
        self._identity = identity
        QLabel.setPixmap(self, pixmap)
        costume = self._costumes.get(str(Path(identity).absolute())) if identity else None
        if costume and self._bindings is not None:
            self.canvas.set_model(self._root, self._bindings.models[costume])
            self._apply_pending_cue()

    def apply_cue(self, cue):
        self._pending_cue = dict(cue)
        self._apply_pending_cue()

    def _apply_pending_cue(self):
        if self._pending_cue is None or self.canvas.model is None:
            return
        costume = self._costumes.get(str(Path(self._identity).absolute())) if self._identity else None
        if self._pending_cue.get("costume") == costume:
            self.canvas.apply_cue(self._pending_cue)
            self._pending_cue = None

    def reset_performance(self):
        self._pending_cue = None
        self.canvas.reset_performance()

    def interrupt_performance(self):
        self._pending_cue = None
        self.canvas.reset_performance(preserve_expression=True)

    def finish_performance(self):
        self._pending_cue = None
        self.canvas.finish_performance()

    def native_hit_region(self):
        if not self._native_ready:
            return None
        return self._shape if not self._shape.isEmpty() else QRegion(self.rect())

    def _refresh_shape(self):
        now = time.monotonic()
        motion = self.canvas.spec.motions.get(self.canvas._motion_id) if self.canvas.spec else None
        interval = .5 if motion is not None and motion.kind == "idle" else .2
        if not self._native_ready or self._reading_shape or now - self._shape_at < interval:
            return
        self._shape_at, self._reading_shape = now, True
        try:
            # GPU readback can wait for a frame. Quiet idle reuses its padded
            # silhouette for longer; gestures keep the 5 Hz refresh rate.
            image = self.canvas.grabFramebuffer().scaled(max(1, self.width() // 8), max(1, self.height() // 8))
            if image.isNull():
                return
            image = image.convertToFormat(QImage.Format.Format_RGBA8888)
            alpha = bytes(image.constBits())[3::4].translate(bytes(int(value > 12) for value in range(256)))
            region = QRegion()
            sx, sy = self.width() / image.width(), self.height() / image.height()
            margin = max(24, round(self.width() * .055))
            for y in range(image.height()):
                occupied = alpha[y * image.width():(y + 1) * image.width()]
                first, last = occupied.find(b"\x01"), occupied.rfind(b"\x01")
                if first >= 0:
                    row = QRect(int(first * sx), int(y * sy), int((last - first + 1) * sx) + 1, int(sy) + 1)
                    region |= QRegion(row.adjusted(-margin, -margin, margin, margin))
            self._shape = region.intersected(QRegion(self.rect()))
        finally:
            self._reading_shape = False
        self.shape_changed.emit()




    def _show_native(self):
        self._native_ready = True
        self._shape_at = 0
        self._apply_pending_cue()
        self.canvas.show()
        self.status_changed.emit("")
        self.update()

    def _show_fallback(self, message):
        self._native_ready = False
        self.canvas.hide()
        self.status_changed.emit(message)
        self.update()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self.canvas.setGeometry(self.rect())
        self._shape = QRegion()
        self._shape_at = 0

    def paintEvent(self, event):  # noqa: N802
        if self._native_ready:
            return
        super().paintEvent(event)


    def close_renderer(self):
        self.canvas.release()
