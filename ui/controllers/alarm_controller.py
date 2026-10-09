"""Asynchronous desktop alarm management over the existing Home interface."""
import threading
import time
import uuid

from PySide6.QtCore import QObject, QTimer, Signal, Slot, Qt

from ui.widgets.alarm_panel import AlarmEditor, AlarmPanel


class _AlarmRequest(QObject):
    completed = Signal(object)

    def __init__(self, operation, parent):
        super().__init__(parent)
        self.operation = operation

    def start(self):
        def run():
            try:
                result = dict(ok=True, value=self.operation())
            except Exception as exc:
                result = dict(ok=False, error=str(exc), unknown=isinstance(exc, (TimeoutError, ConnectionError, OSError, EOFError)))
            try:
                self.completed.emit(result)
            except RuntimeError:
                pass  # The window was destroyed; the core still owns the command.
        threading.Thread(target=run, name='home-alarm-request', daemon=True).start()


class AlarmController(QObject):
    control_feedback = Signal(str)

    def __init__(self, parent, *, surface_provider):
        super().__init__(parent)
        self.surface_provider = surface_provider
        self.panel = AlarmPanel(parent)
        self.editor = None
        self._snapshot = None
        self._worker = None
        self._stop_worker = None
        self._writing = False
        self._pending = None
        self._command = self._retry_write = None
        self._closed = False
        self._notice = ''
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        self.panel.hidden.connect(self._timer.stop)
        self.panel.fixed_requested.connect(self.edit_fixed)
        self.panel.temporary_requested.connect(lambda: self.edit_temporary())
        self.panel.edit_requested.connect(self.edit_temporary)
        self.panel.operation_requested.connect(self.submit)

    def show(self):
        if self._closed:
            return
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()
        self._timer.start()
        self.refresh()

    def refresh(self):
        if self._closed or self._worker is not None or not self.panel.isVisible():
            return
        self._start(lambda: self.surface_provider().home_alarm_plan(), writing=False)

    def stop_wake(self):
        if self._closed or self._stop_worker is not None:
            return
        surface = self.surface_provider()
        config = getattr(surface, 'config', None)
        if config is not None and (not config.home.enabled or not config.home.wake.enabled):
            return
        requested_at, requested_mono, request_id = time.time(), time.monotonic(), uuid.uuid4().hex
        # STOP must not queue behind a panel refresh or block local audio stop.
        self._stop_worker = _AlarmRequest(lambda: surface.home_alarm_manage(
            'stop_current', {'requested_at': requested_at, 'requested_mono': requested_mono}, request_id=request_id), self)
        self._stop_worker.completed.connect(self._stop_complete, Qt.ConnectionType.QueuedConnection)
        self._stop_worker.start()

    @Slot(object)
    def _stop_complete(self, result):
        worker, self._stop_worker = self._stop_worker, None
        if worker:
            worker.deleteLater()
        if self._closed:
            return
        if not result['ok']:
            self.control_feedback.emit('当前语音已停止，但整轮叫醒停止结果未确认：'+result['error'])
        elif result['value'].get('persistence_failed'):
            self.control_feedback.emit('本轮叫醒已停止，但保存失败，后台正在重试；重启后请核对闹钟状态。')
        elif result['value']['stopped']:
            self.control_feedback.emit('本轮叫醒已停止，之后的闹钟安排保留。')

    def _start(self, operation, *, writing):
        self._writing = writing
        if writing:
            self.panel.set_busy(True)
            self.panel.status.setText('正在保存…')
            if self.editor:
                self.editor.save_button.setEnabled(False)
        self._worker = _AlarmRequest(operation, self)
        self._worker.completed.connect(self._complete, Qt.ConnectionType.QueuedConnection)
        self._worker.start()

    @Slot(object)
    def _complete(self, result):
        worker, self._worker = self._worker, None
        if worker:
            worker.deleteLater()
        if self._closed:
            return
        if result['ok']:
            self._snapshot = result['value']
            self.panel.load_snapshot(self._snapshot)
            if self._writing:
                self._retry_write = None
                self._notice = '已保存。'
                if self.editor:
                    self.editor.accept()
                    self.editor = None
            if not self._snapshot.get('execution_enabled'):
                self._notice = 'Home 叫醒功能当前关闭，已保存的安排暂不执行。'
            elif self._snapshot.get('error'):
                self._notice = 'Home 当前不可执行：' + self._snapshot['error']
            self.panel.status.setText(self._notice or '北京时间 · 语音可调整未来 24 小时内的这一次')
        else:
            self._notice = result['error']
            if self._writing and result.get('unknown'):
                self._retry_write = self._command
                self._notice += '；保存结果尚未确认，可再次保存核对原请求。'
            self.panel.status.setText(self._notice)
            self.panel.set_busy(not self._writing)
            if self.editor:
                self.editor.save_button.setEnabled(True)
                self.editor.status.setText(self._notice)
        self._writing = False
        if self._pending is not None:
            operation, self._pending = self._pending, None
            self._start(operation, writing=True)

    def submit(self, action, arguments, *, revision=None):
        if self._closed or self._snapshot is None:
            return
        if self._writing or self._pending is not None:
            return
        revision = self._snapshot['revision'] if revision is None else revision
        arguments = dict(arguments)
        if self._retry_write is not None and self._retry_write[:2] == (action, arguments):
            _, _, request_id, revision = self._retry_write
        else:
            request_id = uuid.uuid4().hex
        self._command = action, arguments, request_id, revision
        operation = lambda: self.surface_provider().home_alarm_manage(action, arguments,
            expected_revision=revision, request_id=request_id)
        if self._worker is not None:
            self._pending = operation
            self.panel.set_busy(True)
            if self.editor:
                self.editor.save_button.setEnabled(False)
        else:
            self._start(operation, writing=True)

    def _open_editor(self, kind, value, action, identity_arguments=None):
        if self.editor:
            self.editor.raise_()
            return
        editor = self.editor = AlarmEditor(self.panel, kind=kind, now=self._snapshot['now'], value=value)
        revision = self._snapshot['revision']
        def save(values):
            if action == 'move':
                values.pop('label', None)
            self.submit(action, dict(values, **(identity_arguments or {})), revision=revision)
        editor.submitted.connect(save)
        editor.finished.connect(lambda _: self._editor_closed(editor))
        editor.show()

    def _editor_closed(self, editor):
        if self.editor is editor:
            self.editor = None
        editor.deleteLater()

    def edit_fixed(self):
        if self._snapshot:
            self._open_editor('fixed', self._snapshot['fixed'], 'save_fixed')

    def edit_temporary(self, value=None):
        if not self._snapshot:
            return
        if value and value.get('editor_kind') == 'move':
            self._open_editor('move', value, 'move', {'instance_id': value['id']})
        elif value:
            self._open_editor('temporary', value, 'edit_temporary' if value['next'] or value.get('can_enable') else 'rearm', {'schedule_id': value['id']})
        else:
            self._open_editor('temporary', None, 'add_temporary')

    def shutdown(self):
        self._closed = True
        self._pending = None
        self._timer.stop()
        if self.editor:
            self.editor.close()
        self.panel.close()

    @property
    def is_saving(self):
        return self._writing or self._pending is not None or self._stop_worker is not None
