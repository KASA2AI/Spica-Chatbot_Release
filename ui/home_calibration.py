"""Local Home calibration UI. Preview bytes stay in memory; face enrollment is optional."""
import time

from PySide6.QtCore import Qt, QTimer, QRectF, QSize, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QWidget, QLabel, QPushButton, QVBoxLayout,
                              QHBoxLayout, QMessageBox)

from spica.adapters.home_camera import HomeCamera
from spica.home.profile import HomeProfile, load_profile, save_profile


class RegionPreview(QLabel):
    def __init__(self):
        super().__init__()
        self.setFixedSize(960, 540)
        self.setText("正在打开房间摄像头……")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.regions = {}
        self.selection = "desk"
        self._origin = None
        self._drag = None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position()

    def mouseMoveEvent(self, event):
        if self._origin is not None:
            self._drag = QRectF(self._origin, event.position()).normalized().intersected(QRectF(self.rect()))
            self.update()

    def mouseReleaseEvent(self, event):
        if self._origin is None:
            return
        rect = QRectF(self._origin, event.position()).normalized().intersected(QRectF(self.rect()))
        self._origin = None
        self._drag = None
        if rect.width() >= 15 and rect.height() >= 15:
            self.regions[self.selection] = (rect.left()/self.width(), rect.top()/self.height(),
                                            rect.right()/self.width(), rect.bottom()/self.height())
            self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        if self._drag is not None:
            painter.setPen(QColor("#35d07f" if self.selection == "desk" else "#65b8ff"))
            painter.drawRect(self._drag)
        for name, (x0, y0, x1, y1) in self.regions.items():
            painter.setPen(QColor("#35d07f" if name == "desk" else "#65b8ff"))
            rect = QRectF(x0*self.width(), y0*self.height(), (x1-x0)*self.width(), (y1-y0)*self.height())
            painter.drawRect(rect)
            painter.drawText(rect.topLeft(), "桌区" if name == "desk" else "床区")


class HomeCalibration(QWidget):
    saved = Signal()
    closed = Signal()

    def __init__(self, config, *, raw_preview=False, parent=None, camera=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.config = config
        self.raw_preview = raw_preview
        self.setWindowTitle("Spica Home · 房间区域标定")
        self.preview = RegionPreview()
        self.hint = QLabel("选择桌区或床区后拖框；重新拖框可调整该区域，人体圆点表示判断位置。\n"
                           "这里预览区域位置；正式亮屏和离床结束仍需持续的新画面确认。\n"
                           "人脸登记可选，不影响仅保存区域；预览画面不保存。")
        self.status = QLabel("等待相机新画面")
        self.samples = []
        self._pending_samples = []
        self._registering = False
        self._last_sample = 0
        self._last_frame = float('-inf')
        self._size = None
        try:
            previous = load_profile(config.data_directory, config.camera_device)
            if previous.image_size == (config.camera_width, config.camera_height):
                self.preview.regions = dict(desk=previous.desk, bed=previous.bed)
                self.samples = previous.embeddings
            else:
                self.status.setText("分辨率已改变，请重新绘制桌区与床区。")
        except FileNotFoundError:
            pass
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))
        self.camera = camera if camera is not None else (HomeCamera(config, None, preview=False, raw_preview=True)
                                                       if raw_preview else HomeCamera(config, None, preview=True))
        self._closed = False
        if getattr(self.camera, 'finished', None) is not None:
            self.camera.finished.connect(self.close)
            self.camera.profile_saved.connect(self._saved)
            self.camera.save_failed.connect(self._save_failed)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(100)
        desk, bed = QPushButton("标定桌区"), QPushButton("标定床区")
        desk.clicked.connect(lambda: setattr(self.preview, "selection", "desk"))
        bed.clicked.connect(lambda: setattr(self.preview, "selection", "bed"))
        self.register_button = QPushButton("可选：登记本人脸部")
        self.register_button.clicked.connect(self.register)
        save = self.save_button = QPushButton("保存区域与可选登记")
        save.clicked.connect(self.save)
        buttons = QHBoxLayout()
        for button in (desk, bed, self.register_button, save):
            buttons.addWidget(button)
            button.setEnabled(not raw_preview)
        reset = QPushButton("清空区域重画")
        reset.clicked.connect(self.reset_regions)
        reset.setEnabled(not raw_preview)
        buttons.addWidget(reset)
        if raw_preview:
            self.hint.setText("相机预览：不加载识别模型、不保存图像。需要人体框及桌区／床区判断时，请打开区域校准。")
        layout = QVBoxLayout(self)
        layout.addWidget(self.hint)
        layout.addWidget(self.preview)
        layout.addWidget(self.status)
        layout.addLayout(buttons)
        self.camera.require("calibration", True)

    def reset_regions(self):
        self.preview.regions.clear()
        self.preview.update()
        self.status.setText("请选择桌区或床区，在画面上重新拖框。")

    def region_status(self, observation):
        if not {"desk", "bed"} <= self.preview.regions.keys() or self._size is None:
            return "区域未设置完整"
        profile = HomeProfile(camera_device=self.config.camera_device, image_size=self._size,
                              desk=self.preview.regions["desk"], bed=self.preview.regions["bed"])
        labels = {"desk": "桌区有人", "bed": "床区有人", "outside": "区域外有人", "unknown": "无法判断"}
        positions = sorted({labels[profile.region(person.box)] for person in observation.people})
        desk, bed = profile.desk, profile.bed
        overlap = max(desk[0], bed[0]) < min(desk[2], bed[2]) and max(desk[1], bed[1]) < min(desk[3], bed[3])
        return " / ".join(positions or ["未检测到人"]) + (" · 桌床区域重叠，请调整" if overlap else "")

    def register(self):
        self._registering = not self._registering
        self._pending_samples.clear()
        self.register_button.setText("取消本次人脸登记" if self._registering else "可选：登记本人脸部")
        self.status.setText("可选登记中：独自在画面内面朝镜头并轻微转头。" if self._registering else
                            "已取消本次登记，原有可选特征保留，可直接保存区域。")

    def tick(self):
        try:
            packet = self.camera.poll()
            if packet is None:
                if self._last_frame != float('-inf') and time.monotonic() - self._last_frame > self.config.frame_ttl_seconds:
                    self.status.setText("等待相机有效新画面，无法判断区域。")
                return
            observation, image, embedding, size = packet
            if image:
                pixmap = QPixmap()
                pixmap.loadFromData(image, "JPG")
                pixmap = pixmap.scaled(QSize(960, 640), Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation)
                self.preview.setFixedSize(pixmap.size())
                self.preview.setPixmap(pixmap)
            self._size = size
            if observation.error:
                self._last_frame = float("-inf")
                self.status.setText(observation.error)
                return
            self._last_frame = observation.captured_mono
            fresh = 0 <= time.monotonic()-observation.captured_mono <= self.config.frame_ttl_seconds
            if self.raw_preview:
                self.status.setText(f"相机画面 {size[0]} × {size[1]} · 未启用人体识别")
                return
            if not fresh:
                self.status.setText("画面已过期，无法判断")
                return
            if (self._registering and embedding is not None and fresh
                    and observation.captured_mono-self._last_sample >= .5):
                if all(sum(a*b for a, b in zip(embedding, sample)) >= self.config.face_similarity
                       for sample in self._pending_samples):
                    self._pending_samples.append(embedding)
                self._last_sample = observation.captured_mono
                if len(self._pending_samples) >= 6:
                    self.samples = list(self._pending_samples)
                    self._registering = False
                    self.register_button.setText("可选：登记本人脸部")
            self.status.setText(self.region_status(observation) + f" · 人数 {len(observation.people)} · 可选登记 {len(self._pending_samples) if self._registering else len(self.samples)} 个 · "
                                f"推理 {observation.inference_seconds:.2f} 秒" +
                                (" · 看清人脸后继续采集，可取消后只保存区域" if self._registering else "") +
                                (" · 人脸辅助不可用，区域标定仍可保存" if observation.face_error else ""))
        except Exception as exc:
            self.status.setText(str(exc))

    def save(self):
        try:
            if self.raw_preview:
                raise ValueError("相机预览不能保存区域，请打开区域校准。")
            if self._registering:
                raise ValueError("可选人脸登记尚未完成；可取消登记后只保存区域。")
            if not {"desk", "bed"} <= self.preview.regions.keys():
                raise ValueError("请先分别绘制桌区和床区，再保存。")
            if self._size is None or not 0 <= time.monotonic()-self._last_frame <= self.config.frame_ttl_seconds:
                raise ValueError("请等待相机有效新画面后保存区域。")
            if self._size != (self.config.camera_width, self.config.camera_height):
                raise ValueError(f"相机实际返回 {self._size[0]}×{self._size[1]}，请先在设备设置保存这个分辨率再校准。")
            profile = HomeProfile(camera_device=self.config.camera_device, image_size=self._size,
                desk=self.preview.regions["desk"], bed=self.preview.regions["bed"], embeddings=self.samples)
            if getattr(self.camera, 'save_profile', None) is not None:
                self.camera.save_profile(profile)
                self.save_button.setEnabled(False)
                self.status.setText('正在保存区域…')
                return
            save_profile(self.config.data_directory, profile)
        except (KeyError, ValueError, OSError) as exc:
            QMessageBox.warning(self, "标定尚未保存", str(exc))
            return
        self._saved()

    def _save_failed(self, message):
        self.save_button.setEnabled(True)
        self.status.setText(f'区域保存未确认：{message}')
        QMessageBox.warning(self, '标定保存未确认', message)

    def _saved(self):
        self.status.setText("区域及可选人脸特征已保存到本机 Home 数据。")
        self.saved.emit()
        self.close()

    def closeEvent(self, event):
        self.timer.stop()
        try:
            released = self.camera.close()
        except Exception as exc:
            self.status.setText(f'相机尚未释放：{exc}')
            event.ignore()
            return
        if released is False:
            self.status.setText('正在结束预览并恢复 Home…')
            event.ignore()
            return  # The worker's finished signal retries close without blocking Qt.
        if not self._closed:
            self._closed = True
            self.closed.emit()
        super().closeEvent(event)


def run(config):
    app = QApplication.instance() or QApplication([])
    window = HomeCalibration(config)
    window.show()
    return app.exec()
