"""Settings-only camera preview, borrowing Home's existing lease when active."""
from pathlib import Path
from PySide6.QtCore import QObject, Qt
from spica.config.home import HomeConfig
from spica.config.manager import ConfigManager
from spica.config.schema import fold_platform
from ui.workers.character_package_worker import CharacterPackageWorker

ROOT = Path(__file__).resolve().parents[2]


class HomeCameraController(QObject):
    def __init__(self, window, panel):
        super().__init__(window)
        self.window, self.panel = window, panel
        self.page = self.editor = panel.application_page
        self.calibration = self.worker = None
        self._closing = False
        self._camera_generation = 0
        self.page.preview_requested.connect(lambda: self.open_camera(preview=True))
        self.page.calibrate_requested.connect(lambda: self.open_camera(preview=False))

    def _run(self, operation, complete):
        if self._closing or self.worker is not None or self.panel.settings_busy:
            return
        self._complete_operation = complete
        self.panel.set_application_busy(True)
        self.worker = CharacterPackageWorker(operation, self)
        self.worker.completed.connect(self._complete, Qt.ConnectionType.QueuedConnection)
        self.worker.start()

    def _complete(self, event):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        self.panel.set_application_busy(False)
        if self._closing:
            return
        if event.data['ok']:
            try:
                self._complete_operation(event.data)
            except Exception as exc:
                self.page.status.setText('相机窗口未能打开：' + str(exc))
        else:
            self.page.status.setText(event.data['error'])

    def shutdown(self):
        self._closing = True
        self._camera_generation += 1
        if self.calibration is not None:
            self.calibration.close()
        return self.worker is None and self.calibration is None

    def open_camera(self, *, preview):
        if self._closing:
            return
        if self.calibration is not None:
            self.calibration.raise_()
            self.calibration.activateWindow()
            return
        live = getattr(self.window.host, "config", None)
        shared_camera = getattr(self.window.host, "home_runtime", None) is not None
        if not preview and self.editor.patch():
            self.page.status.setText("请先保存设备与应用设置，再进行区域校准，避免标定与设备配置不一致。")
            return
        patch = self.editor.patch() if preview else {}
        generation = self._camera_generation
        self.page.status.setText("正在检查相机配置…")
        def read():
            import sys
            from spica.adapters.home_camera import camera_device_present
            config = self.window.host.management_surface.read_config()
            effective_platform = fold_platform(config["platform"]["os"], sys.platform)
            if effective_platform not in {'linux', 'windows'}:
                raise ValueError("Home 相机与区域校准支持 Linux 和 Windows。")
            home = HomeConfig.model_validate(ConfigManager.merge(config["home"], patch.get("home", {})))
            if shared_camera:
                # Model/data locations belong to the running core. Only the
                # selected camera's device and format are temporary preview inputs.
                home = HomeConfig.model_validate({**live.home.model_dump(), **{
                    key: getattr(home, key) for key in ('camera_device', 'camera_width', 'camera_height', 'camera_fps')}})
            if not camera_device_present(home.camera_device, effective_platform):
                raise ValueError("所选相机不存在，请刷新设备列表或检查连接。")
            home = home.model_copy(update={"data_directory": str(ROOT / home.data_directory), "model_directory": str(ROOT / home.model_directory)})
            return {"home": home}
        def open_window(data):
            if generation != self._camera_generation:
                return
            from ui.home_calibration import HomeCalibration
            camera = None
            if shared_camera:
                from ui.workers.home_preview_camera import HomePreviewCamera
                camera = HomePreviewCamera(self.window.host, data['home'], raw_preview=preview, parent=self)
            try:
                self.calibration = HomeCalibration(data["home"], raw_preview=preview, parent=self.window, camera=camera)
            except Exception:
                if camera is not None:
                    camera.close()
                    camera.deleteLater()
                raise
            self.calibration.saved.connect(lambda: self.page.camera_status.setText(
                "桌区／床区已保存，关闭校准后 Home 会重新读取；设备与分辨率的配置变更仍需重启。"
                if shared_camera else "桌区／床区已保存，启用 Home 并重启后生效。"))
            self.calibration.closed.connect(self._camera_closed)
            self.calibration.show()
            self.page.status.setText("已打开相机预览。" if preview else "拖框设置桌区／床区，观察人体圆点与实时区域判断后保存。")
        self._run(read, open_window)

    def _camera_closed(self):
        if self.calibration is not None:
            camera = self.calibration.camera
            if getattr(camera, 'detail', ''):
                self.page.status.setText(camera.detail)
            if isinstance(camera, QObject):
                camera.deleteLater()
            self.calibration.deleteLater()
            self.calibration = None
