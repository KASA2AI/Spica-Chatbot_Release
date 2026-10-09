"""Desktop OS power notifications; the voice owner decides what may resume."""

import logging
import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QObject, Signal, Slot, SLOT


class _WindowsPowerFilter(QAbstractNativeEventFilter):
    def __init__(self, notify):
        super().__init__()
        self.notify = notify

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) in (b'windows_generic_MSG', b'windows_dispatcher_MSG'):
            import ctypes
            from ctypes.wintypes import MSG
            event = ctypes.cast(int(message), ctypes.POINTER(MSG)).contents
            if event.message == 0x0218:  # WM_POWERBROADCAST
                if event.wParam == 4:  # PBT_APMSUSPEND
                    self.notify(True)
                elif event.wParam in (7, 18):  # PBT_APMRESUMESUSPEND / AUTOMATIC
                    self.notify(False)
        return False, 0


class DesktopPowerEvents(QObject):
    suspended = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bus = self._filter = None
        if sys.platform == 'win32':
            self._filter = _WindowsPowerFilter(self.suspended.emit)
            QCoreApplication.instance().installNativeEventFilter(self._filter)
        elif sys.platform.startswith('linux'):
            try:
                from PySide6.QtDBus import QDBusConnection
                bus = QDBusConnection.systemBus()
                if bus.connect('org.freedesktop.login1', '/org/freedesktop/login1',
                               'org.freedesktop.login1.Manager', 'PrepareForSleep', self,
                               SLOT('_prepare_sleep(bool)')):
                    self._bus = bus
            except ImportError:
                logging.getLogger(__name__).warning('Desktop sleep notifications unavailable: QtDBus missing')

    @Slot(bool)
    def _prepare_sleep(self, sleeping):
        self.suspended.emit(sleeping)

    def shutdown(self):
        if self._bus is not None:
            self._bus.disconnect('org.freedesktop.login1', '/org/freedesktop/login1',
                                 'org.freedesktop.login1.Manager', 'PrepareForSleep', self,
                                 SLOT('_prepare_sleep(bool)'))
            self._bus = None
        if self._filter is not None:
            QCoreApplication.instance().removeNativeEventFilter(self._filter)
            self._filter = None
