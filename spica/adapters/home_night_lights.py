"""Optional bedtime LEDs, limited to the installation's identified controllers."""
import ctypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


CASE_CONTROLLER = 'Lian Li Uni Hub - SL Infinity'
GPU_CONTROLLER = 'ZOTAC GAMING GeForce RTX 4090 AMP Extreme AIRO'
MOTHERBOARD_CONTROLLER = 'ASUS ROG STRIX Z790-H GAMING WIFI'
RAM_CONTROLLER = 'ENE DRAM'
GPU_PCI_ID = dict(vendor='0x10de', device='0x2684', subsystem_vendor='0x19da', subsystem_device='0x4675')


def _pci_matches(device, expected):
    return all((device/name).read_text().strip().lower() == value for name, value in expected.items())


def _gpu_i2c_transfer(descriptor, data=None):
    """The same single-message I2C transfer used by OpenRGB on Linux."""
    class Message(ctypes.Structure):
        _fields_ = [('address', ctypes.c_ushort), ('flags', ctypes.c_ushort),
                    ('length', ctypes.c_ushort), ('buffer', ctypes.POINTER(ctypes.c_ubyte))]

    class Transfer(ctypes.Structure):
        _fields_ = [('messages', ctypes.POINTER(Message)), ('count', ctypes.c_uint)]

    size = 32 if data is None else len(data)
    buffer = (ctypes.c_ubyte * size)(*([0] * size if data is None else data))
    message = Message(0x49, 1 if data is None else 0, size, buffer)
    request = Transfer(ctypes.pointer(message), 1)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
    libc.ioctl.restype = ctypes.c_int
    if libc.ioctl(descriptor, 0x0707, ctypes.byref(request)) != 1:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return bytes(buffer)


def _gpu_master_off(device, *, sysfs=Path('/sys/class/i2c-dev'), transfer=_gpu_i2c_transfer):
    if not re.fullmatch(r'/dev/i2c-\d+', device):
        raise RuntimeError('显卡灯控总线无效')
    parent = (sysfs/Path(device).name/'device').resolve(strict=True).parent
    if not _pci_matches(parent, GPU_PCI_ID):
        raise RuntimeError('显卡灯控总线不属于已绑定的索泰 RTX 4090')
    descriptor = os.open(device, os.O_RDWR | os.O_CLOEXEC)
    try:
        transfer(descriptor, bytes((0xA0, 0xF1, 0, 0, 0, 0, 0, 0)))
        if transfer(descriptor).split(b'\0')[0] != b'N675A-1062':
            raise RuntimeError('显卡灯控固件身份不符')
        # OpenRGB ZotacV2GPUController::TurnOnOff(false). Static black/zero
        # brightness was accepted by this firmware but left its LEDs lit.
        # This frame only disables LEDs: reset=false, no firmware reset.
        transfer(descriptor, bytes((0xA0, *([0] * 20))))
        for active in (0, 1):
            transfer(descriptor, bytes((0xA0, 0xF0, 0, 0, 0, active, 6, 0)))
            state = transfer(descriptor)
            if len(state) != 32 or state[0] != 0 or state[4:6] != bytes((active, 6)):
                raise RuntimeError('显卡灯效总开关关闭未获确认')
    finally:
        os.close(descriptor)


def _case_sync_off(*, sysfs=Path('/sys/class/hidraw'), devices=Path('/dev')):
    matches = [path for path in sysfs.glob('hidraw*')
               if 'HID_ID=0003:00000CF2:0000A102' in (path/'device/uevent').read_text().splitlines()]
    if len(matches) != 1:
        raise RuntimeError('无法唯一确认已安装的 SL-Infinity 控制器')
    descriptor = os.open(devices/matches[0].name, os.O_WRONLY | os.O_CLOEXEC)
    try:
        # Only RGB-header sync (0x61); never the distinct PWM sync command (0x62).
        if os.write(descriptor, bytes((0xE0, 0x10, 0x61, 0, 0, 0, 0))) != 7:
            raise RuntimeError('SL-Infinity RGB 同步关闭结果未确认')
    finally:
        os.close(descriptor)
    time.sleep(.2)


def _gpu_present(*, sysfs=Path('/sys/bus/pci/devices')):
    return any(_pci_matches(device, GPU_PCI_ID)
               for device in sysfs.iterdir() if (device/'vendor').is_file())


class HomeNightLights:
    def __init__(self, config, *, run=subprocess.run, case_sync_off=_case_sync_off, gpu_present=_gpu_present,
                 gpu_off=_gpu_master_off, effective_platform=None):
        self.config, self._run = config, run
        self._case_sync_off, self._gpu_present = case_sync_off, gpu_present
        self._gpu_off = gpu_off
        self._platform = effective_platform or ('windows' if sys.platform == 'win32' else 'linux')
        self._windows = None
        if self._platform == 'windows':
            from spica.adapters.windows_home_lights import WindowsHomeLights
            self._windows = WindowsHomeLights(config.lights.openrgb_executable)
            self._case_sync_off = self._windows.case_sync_off

    def off(self, cancelled=None):
        return self._set(False, cancelled)

    def on(self, cancelled=None):
        return self._set(True, cancelled)

    def cleanup_settled(self, pending):
        if self._windows is None or set(pending) != {'ram'}:
            return False
        return self._windows.ram_cleanup_settled(pending['ram']['owner'])

    def _set(self, enabled, cancelled=None):
        results = {}
        cleanup_pending = False
        operations = (
            ('respeaker', self.config.lights.bedtime_respeaker_leds_off,
             lambda: self._windows.respeaker_set(enabled, cancelled) if self._windows is not None else self._respeaker_set(enabled)),
            ('case', self.config.lights.bedtime_case_leds_off, lambda: self._case_set(enabled, cancelled)),
            ('gpu', self.config.lights.bedtime_gpu_leds_off, lambda: self._gpu_set(enabled, cancelled)),
            ('ram', self.config.lights.bedtime_ram_leds_off, lambda: self._ram_set(enabled, cancelled)),
            ('motherboard', self.config.lights.bedtime_motherboard_leds_off, lambda: self._motherboard_set(enabled, cancelled)),
        )
        if self._windows is not None:
            # Its protected RAM helper first releases the competing Aura LED
            # service. The other Windows controllers then have one owner.
            operations = sorted(operations, key=lambda item: item[0] != 'ram')
        for name, configured, operation in operations:
            if not configured:
                continue
            if cleanup_pending or cancelled is not None and cancelled.is_set():
                results[name] = {'status': 'cancelled'}
                continue
            try:
                completed = operation()
                if isinstance(completed, dict) and completed.get('status') == 'cleanup_pending':
                    results[name] = completed
                    cleanup_pending = True
                else:
                    results[name] = {'status': 'cancelled' if completed is False else 'requested'}
            except Exception as exc:
                # An optional LED failure must not cancel the room light or alarm.
                results[name] = {'status': 'unconfirmed', 'reason': str(exc)}
        return results

    @staticmethod
    def _respeaker_set(enabled):
        from hardware.respeaker.control import ReSpeakerControl
        control = ReSpeakerControl()
        try:
            if enabled:
                control.turn_leds_on()
            else:
                control.turn_leds_off()
        finally:
            control.close()

    def _run_openrgb(self, command):
        result = (self._windows.run_cli(command, self._run) if self._windows is not None else
                  self._run(command, capture_output=True, text=True, timeout=3, check=True))
        # OpenRGB 1.0rc2 can exit zero even after rejecting CLI settings.
        if any(error in result.stdout for error in ('Error:', 'Wrong number of colors specified')):
            raise RuntimeError('OpenRGB 未接受灯效命令：'+result.stdout[-500:])
        return result.stdout

    def _openrgb_command(self, directory, controller, *, detector=None, count=1):
        if self._windows is not None:
            directory += '-windows'
        settings = Path(self.config.data_directory) / directory
        if self._windows is not None:
            settings = settings.resolve()
        if self._windows is not None:
            self._windows.validate_profile(settings, detector or controller)
        document = json.loads((settings / 'OpenRGB.json').read_text(encoding='utf-8'))
        detectors = document.get('Detectors', {}).get('detectors', {})
        if {name for name, enabled in detectors.items() if enabled} != {detector or controller}:
            raise RuntimeError('OpenRGB 必须只启用指定控制器：'+(detector or controller))
        command = [self.config.lights.openrgb_executable, '--noautoconnect', '--config', str(settings)]
        listed = self._run_openrgb([*command, '--list-devices'])
        if re.findall(r'^\d+: ([^\r\n]+)', listed, re.M) != [controller] * count:
            raise RuntimeError('灯效控制器身份或数量不符：'+controller)
        return command, listed

    def _case_set(self, enabled, cancelled=None):
        command, _ = self._openrgb_command('openrgb', CASE_CONTROLLER)
        if cancelled is not None and cancelled.is_set():
            return False
        if self._windows is not None:
            if self._windows.case_sync_off(cancelled) is False:
                return False
        else:
            self._case_sync_off()
        if cancelled is not None and cancelled.is_set():
            return False
        # This driver starts every channel at zero LEDs and then skips all
        # hardware mode writes. Cover its eight channels up to the documented
        # 96-LED limit; unused LEDs receive black too. These are RGB sizes only.
        channels = [part for zone in range(8) for part in (
            '--device', CASE_CONTROLLER, '--zone', str(zone), '--size', '96',
            '--mode', 'Rainbow Wave' if enabled else 'Static', '--brightness', '100' if enabled else '0',
            *(() if enabled else ('--color', '000000')))]
        self._run_openrgb([*command, *channels])

    def _gpu_set(self, enabled, cancelled=None):
        if self._windows is not None:
            return self._windows.gpu_set(enabled, cancelled)
        if not self._gpu_present():
            raise RuntimeError('未检测到已绑定的索泰 RTX 4090（19da:4675）')
        command, listed = self._openrgb_command('openrgb-gpu', GPU_CONTROLLER)
        if cancelled is not None and cancelled.is_set():
            return False
        if enabled:
            # The driver first enables the master LED switch, then sets both
            # idle and active effects. This restoration passed the site test.
            self._run_openrgb([*command, '--device', GPU_CONTROLLER, '--mode', 'Rainbow', '--brightness', '100'])
        else:
            location = re.search(r'^\s*Location:\s+I2C: (/dev/i2c-\d+), address 0x49\s*$', listed, re.M)
            if location is None:
                raise RuntimeError('显卡灯控总线地址未确认')
            self._gpu_off(location[1])

    def _ram_set(self, enabled, cancelled=None):
        if self._windows is not None:
            return self._windows.ram_set(enabled, cancelled)
        # ENE detection remaps LED addresses. Keep its explicit opt-in and
        # single-detector profile; this is not a read-only SMBus scan.
        command, _ = self._openrgb_command('openrgb-ram', RAM_CONTROLLER, detector='ENE SMBus DRAM', count=2)
        if cancelled is not None and cancelled.is_set():
            return False
        self._run_openrgb([*command, '--device', RAM_CONTROLLER, '--mode', 'Rainbow' if enabled else 'Off'])

    def _motherboard_set(self, enabled, cancelled=None):
        command, _ = self._openrgb_command('openrgb-motherboard', MOTHERBOARD_CONTROLLER, detector='ASUS Aura Motherboard')
        if cancelled is not None and cancelled.is_set():
            return False
        # Zero-sized ARGB zones are skipped by OpenRGB. A size of one enables
        # the whole-channel hardware effect; no per-LED color writes are used.
        channels = [part for zone in (1, 2, 3) for part in (
            '--device', MOTHERBOARD_CONTROLLER, '--zone', str(zone), '--size', '1',
            '--mode', 'Rainbow' if enabled else 'Off')]
        self._run_openrgb([*command, *channels])
