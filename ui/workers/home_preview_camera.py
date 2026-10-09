"""Bounded UI mailbox for the core-owned Home camera preview session."""
import threading
import uuid

from PySide6.QtCore import QThread, Signal


class HomePreviewCamera(QThread):
    profile_saved = Signal()
    save_failed = Signal(str)

    def __init__(self, host, config, *, raw_preview=False, parent=None):
        super().__init__(parent)
        self.host, self.config, self.raw_preview = host, config, raw_preview
        self.session_id = uuid.uuid4().hex
        self._packet = None
        self._pending_profile = None
        self._packet_lock = threading.Lock()
        self._started = False
        self.detail = ''

    def require(self, _reason, needed):
        if needed and not self._started:
            self._started = True
            self.start()

    def poll(self):
        with self._packet_lock:
            packet, self._packet = self._packet, None
            return packet

    def close(self):
        self.requestInterruption()
        return not self.isRunning()

    def save_profile(self, profile):
        if not self.isRunning() or self.isInterruptionRequested():
            raise ValueError('校准会话已结束，未保存区域')
        with self._packet_lock:
            self._pending_profile = profile.model_dump()

    def run(self):
        operation = getattr(self.host, 'home_camera_preview', None)
        try:
            if operation is None:
                raise RuntimeError('请重启核心与桌面，加载相机自动交接功能')
            operation('begin', self.session_id, raw_preview=self.raw_preview,
                camera_settings={key: getattr(self.config, key) for key in
                    ('camera_device', 'camera_width', 'camera_height', 'camera_fps')})
            while not self.isInterruptionRequested():
                with self._packet_lock:
                    profile, self._pending_profile = self._pending_profile, None
                if profile is not None:
                    try:
                        result = operation('save', self.session_id, profile_data=profile)
                        if result.get('saved') is not True:
                            raise RuntimeError('后台尚未确认区域保存')
                    except Exception as exc:
                        self.save_failed.emit(str(exc))
                    else:
                        self.profile_saved.emit()
                result = operation('poll', self.session_id)
                if not result['active']:
                    self.detail = result.get('detail', 'Home 已收回相机。')
                    break
                if result.get('packet') is not None:
                    with self._packet_lock:
                        self._packet = result['packet']
                self.msleep(100)
        except Exception as exc:
            self.detail = f'相机预览未能继续：{exc}'
        finally:
            # End uses the same identity even if the begin reply was lost.
            # Detaching the desktop also revokes it on the server.
            try:
                result = operation('end', self.session_id) if operation is not None else {}
                if not self.detail:
                    self.detail = result.get('detail', '相机预览已关闭。')
            except Exception as exc:
                self.detail = f'无法确认 Home 相机恢复，请检查核心连接：{exc}'
            with self._packet_lock:
                self._packet = None
