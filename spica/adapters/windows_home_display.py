"""Session display power, guarded by one active monitor's native identity."""
from dataclasses import dataclass

from spica.home.models import DisplayResult


@dataclass(frozen=True)
class WindowsMonitor:
    device_id: str
    name: str


def active_monitors():
    import win32api
    import winreg

    result = []
    for handle, _, _ in win32api.EnumDisplayMonitors():
        output = win32api.GetMonitorInfo(handle)['Device']
        index = 0
        while True:
            try:
                device = win32api.EnumDisplayDevices(output, index, 1)
            except win32api.error:
                break
            index += 1
            if not device.StateFlags & 1:  # DISPLAY_DEVICE_ACTIVE (monitor flags)
                continue
            parts = device.DeviceID.split('#')
            if len(parts) != 4 or parts[0].upper() != '\\\\?\\DISPLAY':
                raise RuntimeError('Windows monitor identity unavailable')
            key = 'SYSTEM\\CurrentControlSet\\Enum\\DISPLAY\\' + '\\'.join(parts[1:3]) + '\\Device Parameters'
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as entry:
                raw, _ = winreg.QueryValueEx(entry, 'EDID')
            names = [raw[start+5:start+18].decode('ascii', errors='replace').strip()
                     for start in (54, 72, 90, 108) if raw[start:start+5] == b'\0\0\0\xfc\0']
            if len(names) != 1:
                raise RuntimeError('Windows monitor EDID name unavailable')
            result.append(WindowsMonitor(device.DeviceID, names[0]))
    return result


def request_display_power(enabled):
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    send = user32.SendNotifyMessageW
    send.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    send.restype = wintypes.BOOL
    # Async to other windows: an unresponsive application cannot block Home.
    if not send(0xffff, 0x0112, 0xf170, -1 if enabled else 2):
        raise ctypes.WinError(ctypes.get_last_error())


class WindowsHomeDisplay:
    def __init__(self, config, *, enumerate_monitors=active_monitors, request=request_display_power):
        self.config, self._enumerate, self._request = config, enumerate_monitors, request

    def check_target(self):
        monitors = self._enumerate()
        if len(monitors) != 1:
            raise RuntimeError('configured monitor must be the only active Windows output')
        target = monitors[0]
        if not self.config.monitor_name or target.name != self.config.monitor_name:
            raise RuntimeError('monitor identity differs from Home calibration')
        # Linux connector names stay intact when moving the configuration.
        # A Windows device interface is a separate, optional stricter binding.
        if self.config.windows_monitor_id and target.device_id != self.config.windows_monitor_id:
            raise RuntimeError('Windows monitor device identity differs from Home calibration')

    def wake(self):
        return self._set_power(True)

    def blank(self):
        return self._set_power(False)

    def _set_power(self, enabled):
        try:
            self.check_target()
            self._request(enabled)
            return DisplayResult('wake_requested' if enabled else 'blank_requested',
                                 'physical light still requires observation')
        except Exception as exc:
            return DisplayResult('unavailable', str(exc))
