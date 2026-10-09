"""Poll Home only while its status page is visible; no device actions."""
from PySide6.QtCore import QObject, QTimer, Slot, Qt

from ui.workers.character_package_worker import CharacterPackageWorker


class HomeStatusController(QObject):
    def __init__(self, window, page):
        super().__init__(window)
        self.window, self.page = window, page
        self._worker = None
        self._generation = 0
        self._request_generation = 0
        self._closed = False
        self._snapshot = None
        self._audio = {}
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._tick)
        page.visibility_changed.connect(self._visibility_changed)
        page.refresh_requested.connect(self.refresh)

    def _visibility_changed(self, visible):
        self._generation += 1
        self._timer.stop()
        self._snapshot = None
        if visible and not self._closed:
            self.page.unavailable('正在读取 Home 状态…')
            self._timer.start()
            self.refresh()

    def _tick(self):
        if self._closed or not self.page.isVisible():
            return
        if self._snapshot is not None:
            self.page.load_snapshot(self._snapshot, self._audio)
        self.refresh()

    def refresh(self):
        if self._closed or not self.page.isVisible() or self._worker is not None:
            return
        if getattr(self.window, '_setup_only', False):
            self.page.unavailable('配置模式尚未连接核心，保存并重启后可查看运行状态。')
            return
        host = self.window.host
        self._request_generation = self._generation
        self.page.refresh_button.setEnabled(False)
        self._worker = CharacterPackageWorker(lambda: {'snapshot': host.home_status()}, self)
        self._worker.completed.connect(self._complete, Qt.ConnectionType.QueuedConnection)
        self._worker.start()

    @Slot(object)
    def _complete(self, event):
        if self.sender() is not self._worker:
            return
        worker, self._worker = self._worker, None
        worker.deleteLater()
        if self._closed:
            return
        self.page.refresh_button.setEnabled(True)
        if not self.page.isVisible():
            return
        if self._request_generation != self._generation:
            self.refresh()
            return
        result = event.data
        if not result['ok']:
            self._snapshot = None
            self.page.unavailable('状态读取失败：' + result['error'])
        elif result['snapshot'] is None:
            self._snapshot = None
            self.page.unavailable(('Home 未启动：' + self.window.host.plugin_host.errors().get('builtin:home', '请核对配置')) if self.window.host.config.home.enabled else 'Home 尚未启用。')
        else:
            self._snapshot = result['snapshot']
            self._audio = self.window.audio_controller.output_status('home')
            self.page.load_snapshot(self._snapshot, self._audio)

    def shutdown(self):
        self._closed = True
        self._generation += 1
        self._timer.stop()
