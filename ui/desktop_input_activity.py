"""Read input idleness, never key contents; owned and closed by the desktop UI."""

import ctypes
import sys
import time

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication


class _WindowsIdle:
    class Info(ctypes.Structure):
        _fields_ = [('cbSize', ctypes.c_uint), ('dwTime', ctypes.c_uint32)]

    def __init__(self):
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.user.GetLastInputInfo.argtypes = [ctypes.POINTER(self.Info)]
        self.user.GetLastInputInfo.restype = ctypes.c_int
        self.kernel.GetTickCount.argtypes = []
        self.kernel.GetTickCount.restype = ctypes.c_uint32

    def seconds(self):
        info = self.Info(ctypes.sizeof(self.Info), 0)
        if not self.user.GetLastInputInfo(ctypes.byref(info)):
            return None
        return ((self.kernel.GetTickCount() - info.dwTime) & 0xffffffff) / 1000.

    def close(self):
        pass


class _X11Idle:
    class Info(ctypes.Structure):
        _fields_ = [('window', ctypes.c_ulong), ('state', ctypes.c_int), ('kind', ctypes.c_int),
                    ('since', ctypes.c_ulong), ('idle', ctypes.c_ulong), ('event_mask', ctypes.c_ulong)]

    def __init__(self):
        self.display = self.info = None
        self.x11 = ctypes.CDLL('libX11.so.6')
        self.xss = ctypes.CDLL('libXss.so.1')
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XOpenDisplay.restype = ctypes.c_void_p
        self.x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.x11.XDefaultRootWindow.restype = ctypes.c_ulong
        self.x11.XFree.argtypes = [ctypes.c_void_p]
        self.x11.XFree.restype = ctypes.c_int
        self.x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.x11.XCloseDisplay.restype = ctypes.c_int
        self.xss.XScreenSaverAllocInfo.argtypes = []
        self.xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(self.Info)
        self.xss.XScreenSaverQueryInfo.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(self.Info)]
        self.xss.XScreenSaverQueryInfo.restype = ctypes.c_int
        self.display = self.x11.XOpenDisplay(None)
        if not self.display:
            raise OSError('No X11 display')
        self.info = self.xss.XScreenSaverAllocInfo()
        if not self.info:
            self.close()
            raise OSError('No screensaver info')
        self.root = self.x11.XDefaultRootWindow(self.display)

    def seconds(self):
        if not self.xss.XScreenSaverQueryInfo(self.display, self.root, self.info):
            return None
        return self.info.contents.idle / 1000.

    def close(self):
        if self.info:
            self.x11.XFree(self.info)
            self.info = None
        if self.display:
            self.x11.XCloseDisplay(self.display)
            self.display = None


class DesktopInputActivity(QObject):
    """Lazy native sampling, at most twice a second; local input is a fallback."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._reader = None
        self._initialized = self._closed = False
        self._sample_at = None
        self._sample_idle = None
        self._local_input_at = None
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.spontaneous() and event.type() in (
            QEvent.Type.KeyPress, QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove,
            QEvent.Type.Wheel, QEvent.Type.TouchBegin,
        ):
            self._local_input_at = time.monotonic()
        return False

    def idle_seconds(self):
        if self._closed:
            return None
        now = time.monotonic()
        if not self._initialized:
            self._initialized = True
            try:
                if sys.platform == 'win32':
                    self._reader = _WindowsIdle()
                elif sys.platform.startswith('linux') and QGuiApplication.platformName() == 'xcb':
                    self._reader = _X11Idle()
            except (OSError, AttributeError):
                self._reader = None
        if self._sample_at is None or now - self._sample_at >= .5:
            self._sample_at = now
            self._sample_idle = self._reader.seconds() if self._reader is not None else None
        idle = None if self._sample_idle is None else self._sample_idle + now - self._sample_at
        if self._local_input_at is not None:
            local = max(0., now - self._local_input_at)
            idle = local if idle is None else min(idle, local)
        return idle

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        QApplication.instance().removeEventFilter(self)
        if self._reader is not None:
            self._reader.close()
            self._reader = None
