"""Native S3 capability and real resume evidence, without a Qt event loop."""
import ctypes
from ctypes import wintypes
import threading
import time
import xml.etree.ElementTree as ET


class _Guid(ctypes.Structure):
    _fields_ = [('bytes', ctypes.c_ubyte * 16)]

    @classmethod
    def parse(cls, text):
        import uuid
        return cls((ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(text).bytes_le))


class _BatteryScale(ctypes.Structure):
    _fields_ = [('Granularity', wintypes.ULONG), ('Capacity', wintypes.ULONG)]


class _Capabilities(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ubyte) for name in (
        'PowerButtonPresent', 'SleepButtonPresent', 'LidPresent', 'SystemS1', 'SystemS2',
        'SystemS3', 'SystemS4', 'SystemS5', 'HiberFilePresent', 'FullWake', 'VideoDimPresent',
        'ApmPresent', 'UpsPresent', 'ThermalControl', 'ProcessorThrottle', 'ProcessorMinThrottle',
        'ProcessorMaxThrottle', 'FastSystemS4', 'Hiberboot', 'WakeAlarmPresent', 'AoAc',
        'DiskSpinDown', 'HiberFileType', 'AoAcConnectivitySupported')]
    _fields_ += [('spare3', ctypes.c_ubyte * 6), ('SystemBatteriesPresent', ctypes.c_ubyte),
                 ('BatteriesAreShortTerm', ctypes.c_ubyte), ('BatteryScale', _BatteryScale * 3)]
    _fields_ += [(name, ctypes.c_int) for name in (
        'AcOnLineWake', 'SoftLidWake', 'RtcWake', 'MinDeviceWakeState', 'DefaultLowLatencyWake')]


class WindowsPowerState:
    def __init__(self):
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        wmi = None
        try:
            wmi = win32com.client.GetObject('winmgmts:root/cimv2')
            boots = [str(item.LastBootUpTime) for item in wmi.ExecQuery(
                'SELECT LastBootUpTime FROM Win32_OperatingSystem')]
        finally:
            wmi = None
            pythoncom.CoUninitialize()
        if len(boots) != 1 or not boots[0]:
            raise RuntimeError('Windows boot identity unavailable')
        self.boot_id = 'windows:' + boots[0]
        self._lock = threading.Lock()
        self._record = self._read_resume_record()
        self._resume_baseline = self._record
        self._checked = time.monotonic()
        self._pending = False
        self._sleeping = False
        self._resume_notified = False
        self._powr = ctypes.WinDLL('powrprof', use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p)

        class Subscription(ctypes.Structure):
            _fields_ = [('Callback', callback_type), ('Context', ctypes.c_void_p)]

        def notified(context, event, setting):
            self._on_power_notification(event)
            return 0

        self._callback = callback_type(notified)
        self._subscription = Subscription(self._callback, None)
        self._registration = wintypes.HANDLE()
        register = self._powr.PowerRegisterSuspendResumeNotification
        register.argtypes = (wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.HANDLE))
        register.restype = wintypes.DWORD
        error = register(2, ctypes.byref(self._subscription), ctypes.byref(self._registration))
        if error:
            raise ctypes.WinError(error)

    def _on_power_notification(self, event):
        with self._lock:
            if event == 4:  # PBT_APMSUSPEND
                self._sleeping = self._pending = True
                self._resume_notified = False
                # The polling cache can lag the previous resume's event log.
                # Snapshot the actual boundary so that event cannot later prove
                # recovery from this sleep. A failed sample leaves this cycle
                # unknown; a later read alone cannot reconstruct the boundary.
                self._resume_baseline = None
                try:
                    baseline = self._read_resume_record()
                    if baseline < self._record:
                        raise RuntimeError('Windows resume event history changed')
                    self._record = self._resume_baseline = baseline
                except Exception:
                    # Exceptions must never escape the native power callback.
                    # Keep the subscription and pending evidence for cleanup.
                    pass
            elif event in (7, 18):  # PBT_APMRESUMESUSPEND / AUTOMATIC
                self._sleeping = False
                if not self._resume_notified:
                    # The event-log record can arrive before this callback.
                    # Retain its proof against the pre-suspend baseline instead
                    # of waiting forever for a second, nonexistent resume.
                    self._pending = (self._resume_baseline is None
                                     or self._record <= self._resume_baseline)
                self._resume_notified = True
            self._checked = 0.

    @staticmethod
    def _read_resume_record():
        import win32evtlog
        query = "*[System[Provider[@Name='Microsoft-Windows-Kernel-Power'] and EventID=107]]"
        handle = win32evtlog.EvtQuery('System',
            win32evtlog.EvtQueryChannelPath | win32evtlog.EvtQueryReverseDirection, query)
        try:
            events = win32evtlog.EvtNext(handle, 1, 0, 0)
            if not events:
                return 0
            event = events[0]
            try:
                root = ET.fromstring(win32evtlog.EvtRender(event, win32evtlog.EvtRenderEventXml))
                value = root.findtext('{*}System/{*}EventRecordID')
                return int(value)
            finally:
                event.Close()
        finally:
            handle.Close()

    def resume_stamp(self):
        with self._lock:
            if time.monotonic()-self._checked >= 1:
                record = self._read_resume_record()
                if record < self._record:
                    raise RuntimeError('Windows resume event history changed; restart Home to establish a new baseline')
                if self._resume_baseline is not None and record > self._resume_baseline:
                    self._pending = False
                self._record, self._checked = record, time.monotonic()
            if self._sleeping or self._pending:
                raise RuntimeError('Windows resume confirmation pending')
            return dict(boot_id=self.boot_id, suspend_count=self._record)

    def resume_ready(self):
        self.resume_stamp()
        return True

    def can_suspend(self):
        caps = _Capabilities()
        call = self._powr.GetPwrCapabilities
        call.argtypes, call.restype = (ctypes.POINTER(_Capabilities),), ctypes.c_ubyte
        if not call(ctypes.byref(caps)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(caps.SystemS3)

    def wake_enabled(self):
        scheme = ctypes.POINTER(_Guid)()
        active = self._powr.PowerGetActiveScheme
        active.argtypes, active.restype = (wintypes.HKEY, ctypes.POINTER(ctypes.POINTER(_Guid))), wintypes.DWORD
        error = active(None, ctypes.byref(scheme))
        if error:
            raise ctypes.WinError(error)
        class PowerStatus(ctypes.Structure):
            _fields_ = [('ACLineStatus', ctypes.c_ubyte), ('BatteryFlag', ctypes.c_ubyte),
                        ('BatteryLifePercent', ctypes.c_ubyte), ('SystemStatusFlag', ctypes.c_ubyte),
                        ('BatteryLifeTime', wintypes.DWORD), ('BatteryFullLifeTime', wintypes.DWORD)]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        try:
            status = PowerStatus()
            get_status = kernel.GetSystemPowerStatus
            get_status.argtypes, get_status.restype = (ctypes.POINTER(PowerStatus),), wintypes.BOOL
            if not get_status(ctypes.byref(status)) or status.ACLineStatus not in (0, 1):
                raise RuntimeError('Windows power source unavailable')
            read = getattr(self._powr, 'PowerReadACValueIndex' if status.ACLineStatus else 'PowerReadDCValueIndex')
            read.argtypes = (wintypes.HKEY, ctypes.POINTER(_Guid), ctypes.POINTER(_Guid),
                             ctypes.POINTER(_Guid), ctypes.POINTER(wintypes.DWORD))
            read.restype = wintypes.DWORD
            group = _Guid.parse('238c9fa8-0aad-41ed-83f4-97be242c8f20')
            setting = _Guid.parse('bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d')
            value = wintypes.DWORD()
            error = read(None, scheme, ctypes.byref(group), ctypes.byref(setting), ctypes.byref(value))
            if error:
                raise ctypes.WinError(error)
            return value.value == 1  # 2 permits only Windows-designated important tasks.
        finally:
            kernel.LocalFree.argtypes, kernel.LocalFree.restype = (ctypes.c_void_p,), ctypes.c_void_p
            kernel.LocalFree(scheme)

    @staticmethod
    def shutting_down():
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.GetSystemMetrics.argtypes, user.GetSystemMetrics.restype = (ctypes.c_int,), ctypes.c_int
        return bool(user.GetSystemMetrics(0x2000))  # SM_SHUTTINGDOWN

    def suspend(self):
        import win32api
        import win32con
        import win32security
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(),
            win32con.TOKEN_ADJUST_PRIVILEGES | win32con.TOKEN_QUERY)
        previous = None
        try:
            privilege = win32security.LookupPrivilegeValue(None, 'SeShutdownPrivilege')
            win32api.SetLastError(0)
            previous = win32security.AdjustTokenPrivileges(token, False, [(privilege, win32con.SE_PRIVILEGE_ENABLED)])
            if win32api.GetLastError() == 1300:
                raise PermissionError('current Windows session lacks suspend privilege')
            call = self._powr.SetSuspendState
            call.argtypes, call.restype = (ctypes.c_ubyte,) * 3, ctypes.c_ubyte
            if not call(False, False, False):  # S3, no forced closing, wake events enabled.
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            if previous is not None:
                win32security.AdjustTokenPrivileges(token, False, previous)
            token.Close()

    def close(self):
        if self._registration:
            unregister = self._powr.PowerUnregisterSuspendResumeNotification
            unregister.argtypes, unregister.restype = (wintypes.HANDLE,), wintypes.DWORD
            error = unregister(self._registration)
            if error:
                raise ctypes.WinError(error)
            self._registration = wintypes.HANDLE()
