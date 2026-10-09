"""Windows transports for the installation's exact Home RGB controllers.

Protocol references: OpenRGB release_1.0, ZotacV2GPUController,
i2c_smbus/Windows/i2c_smbus_nvapi.cpp, and LianLiControllerDetect.cpp.
HID uses the HIDAPI DLL shipped with that OpenRGB build, not a USB driver swap.
No transport is opened during module import or adapter construction.
"""
import ctypes
import base64
import hashlib
import json
import re
from pathlib import Path
import sys
import time


_U8 = ctypes.c_uint8
_U32 = ctypes.c_uint32
_P8 = ctypes.POINTER(_U8)


class _HidInfo(ctypes.Structure):
    pass


_HidInfo._fields_ = [
    ('path', ctypes.c_char_p), ('vendor_id', ctypes.c_uint16), ('product_id', ctypes.c_uint16),
    ('serial_number', ctypes.c_wchar_p), ('release_number', ctypes.c_uint16),
    ('manufacturer_string', ctypes.c_wchar_p), ('product_string', ctypes.c_wchar_p),
    ('usage_page', ctypes.c_uint16), ('usage', ctypes.c_uint16),
    ('interface_number', ctypes.c_int), ('next', ctypes.POINTER(_HidInfo)), ('bus_type', ctypes.c_int),
]


def _sl_infinity(info):
    return (info.vendor_id, info.product_id, info.interface_number, info.usage_page, info.usage) == (
        0x0CF2, 0xA102, 1, 0xFF72, 0xA1)


def _cancelled(cancelled):
    return cancelled is not None and cancelled.is_set()


class _HidApi:
    def __init__(self, executable):
        dll = Path(executable).resolve().parent / 'hidapi-hotplug.dll'
        if not dll.is_file():
            raise RuntimeError('OpenRGB 同目录缺少官方 hidapi-hotplug.dll；未替换 USB 驱动')
        self.api = ctypes.CDLL(str(dll), winmode=0x1100)
        for name, result, arguments in (
            ('hid_enumerate', ctypes.POINTER(_HidInfo), (ctypes.c_ushort, ctypes.c_ushort)),
            ('hid_free_enumeration', None, (ctypes.POINTER(_HidInfo),)),
            ('hid_open_path', ctypes.c_void_p, (ctypes.c_char_p,)),
            ('hid_get_device_info', ctypes.POINTER(_HidInfo), (ctypes.c_void_p,)),
            ('hid_write', ctypes.c_int, (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t)),
            ('hid_close', None, (ctypes.c_void_p,)),
        ):
            function = getattr(self.api, name)
            function.restype, function.argtypes = result, arguments

    def sync_off(self, cancelled=None):
        api = self.api
        first = api.hid_enumerate(0x0CF2, 0xA102)
        paths = []
        try:
            current = first
            while current:
                info = current.contents
                if _sl_infinity(info) and info.path:
                    paths.append(bytes(info.path))
                current = info.next
        finally:
            api.hid_free_enumeration(first)
        if len(paths) != 1:
            raise RuntimeError('无法唯一确认 Windows SL-Infinity HID 接口（0cf2:a102 / MI01 / FF72:A1）')
        if _cancelled(cancelled):
            return False
        handle = api.hid_open_path(paths[0])
        if not handle:
            raise RuntimeError('Windows SL-Infinity HID 无法打开；请检查设备占用或驱动')
        try:
            info = api.hid_get_device_info(handle)
            if not info or not _sl_infinity(info.contents) or info.contents.path != paths[0]:
                raise RuntimeError('Windows SL-Infinity 打开后的 HID 身份不符')
            if _cancelled(cancelled):
                return False
            # Fixed RGB-header sync command 0x61; never PWM sync 0x62. HIDAPI
            # pads the report to the native output report length when required.
            packet = bytes((0xE0, 0x10, 0x61)) + bytes(62)
            buffer = ctypes.create_string_buffer(packet)
            if api.hid_write(handle, buffer, len(packet)) < len(packet):
                raise RuntimeError('SL-Infinity RGB 同步关闭写入未完成')
        finally:
            api.hid_close(handle)
        time.sleep(.2)
        return True


class _I2CInfo(ctypes.Structure):
    _fields_ = [
        ('version', _U32), ('display_mask', _U32), ('is_ddc_port', _U8), ('device_address', _U8),
        ('register', _P8), ('register_size', _U32), ('data', _P8), ('size', _U32),
        ('speed', _U32), ('speed_khz', _U32), ('port_id', _U8), ('port_id_set', _U32),
    ]


class _NvApi:
    """Only the existing NVIDIA driver's private RGB I2C port is used."""
    def __enter__(self):
        if sys.platform != 'win32' or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise RuntimeError('NVIDIA 原生 RGB 需要 64 位 Windows')
        # Restrict DLL lookup to System32, never the working directory or PATH.
        self.api = ctypes.CDLL('nvapi64.dll', winmode=0x800)
        self.api.nvapi_QueryInterface.argtypes = (_U32,)
        self.api.nvapi_QueryInterface.restype = ctypes.c_void_p
        self.initialize = self._function(0x0150E828)
        self.unload = self._function(0xD22BDD7E)
        self.enumerate = self._function(0xE5AC921F, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(_U32))
        self.pci = self._function(0x2DDFB66E, ctypes.c_void_p, *([ctypes.POINTER(_U32)] * 4))
        args = (ctypes.c_void_p, ctypes.POINTER(_I2CInfo), ctypes.POINTER(_U32))
        self.write = self._function(0x283AC65A, *args)
        self.read = self._function(0x4D7B0709, *args)
        self._check(self.initialize(), '初始化')
        try:
            handles, count = (ctypes.c_void_p * 64)(), _U32()
            self._check(self.enumerate(handles, ctypes.byref(count)), '枚举 GPU 身份')
            if count.value > 64:
                raise RuntimeError('NVIDIA GPU 数量无效')
            matches = []
            for handle in handles[:count.value]:
                device, subsystem, revision, extended = (_U32() for _ in range(4))
                self._check(self.pci(handle, ctypes.byref(device), ctypes.byref(subsystem),
                                    ctypes.byref(revision), ctypes.byref(extended)), '读取 PCI 身份')
                if (device.value, subsystem.value) == (0x268410DE, 0x467519DA):
                    matches.append(handle)
            if len(matches) != 1:
                raise RuntimeError('未唯一确认已绑定的索泰 RTX 4090（10de:2684 / 19da:4675）')
            self.gpu = matches[0]
            return self
        except BaseException:
            self.unload()
            raise

    def __exit__(self, *error):
        self.unload()

    def _function(self, identifier, *arguments):
        address = self.api.nvapi_QueryInterface(identifier)
        if not address:
            raise RuntimeError('当前 NVIDIA 驱动不提供所需 RGB I²C 接口')
        return ctypes.CFUNCTYPE(ctypes.c_int, *arguments)(address)

    @staticmethod
    def _check(result, operation):
        if result != 0:
            raise RuntimeError(f'NVIDIA RGB {operation}失败（NVAPI {result}）')

    def transfer(self, data=None):
        size = 32 if data is None else len(data)
        buffer = (_U8 * size)(*([0] * size if data is None else data))
        info = _I2CInfo()
        info.version = ctypes.sizeof(info) | (3 << 16)
        info.device_address, info.port_id, info.port_id_set = 0x49 << 1, 1, 1
        info.data, info.size, info.speed = buffer, size, 0xFFFF
        unknown = _U32()
        operation = self.read if data is None else self.write
        self._check(operation(self.gpu, ctypes.byref(info), ctypes.byref(unknown)), '读取' if data is None else '写入')
        if info.size != size:
            raise RuntimeError('NVIDIA RGB I²C 返回长度不符')
        return bytes(buffer)


def _gpu_master_set(enabled, cancelled=None, *, api_factory=_NvApi):
    with api_factory() as api:
        if _cancelled(cancelled):
            return False
        api.transfer(bytes((0xA0, 0xF1, 0, 0, 0, 0, 0, 0)))
        if api.transfer().split(b'\0')[0] != b'N675A-1062':
            raise RuntimeError('显卡灯控固件身份不符')

        def states():
            result = []
            for active in (0, 1):
                api.transfer(bytes((0xA0, 0xF0, 0, 0, 0, active, 6, 0)))
                state = api.transfer()
                if len(state) != 32 or state[0] not in (0, 1) or state[1] != 0 or state[4:6] != bytes((active, 6)):
                    raise RuntimeError('显卡 RGB 回读身份不符')
                result.append(bool(state[0]))
            return result

        before = states()
        if _cancelled(cancelled):
            return False
        if before != [enabled, enabled]:
            # Change only the master flag, reset=false. In the off -> on
            # transition the firmware restores its existing effect settings.
            api.transfer(bytes((0xA0, int(enabled))) + bytes(19))
        # The Windows driver can briefly return the previous response after a
        # successful master write. Retry only status queries, never the write.
        deadline = time.monotonic() + .25
        while True:
            if _cancelled(cancelled):
                return False
            try:
                if states() == [enabled, enabled]:
                    return True
            except RuntimeError:
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('显卡灯效总开关状态未获确认')
            pause = min(.02, remaining)
            if cancelled is not None:
                if cancelled.wait(pause):
                    return False
            else:
                time.sleep(pause)


class WindowsHomeLights:
    def __init__(self, executable):
        self.executable = Path(executable)

    def validate_profile(self, directory, detector):
        if self.executable.suffix.lower() != '.exe' or not self.executable.is_file():
            raise RuntimeError('未配置已审核的 Windows OpenRGB.exe')
        settings = json.loads((directory / 'OpenRGB.json').read_text(encoding='utf-8'))
        try:
            marker = json.loads((directory / 'WindowsOpenRGB.json').read_text(encoding='utf-8'))
        except FileNotFoundError as exc:
            raise RuntimeError('Windows OpenRGB detector 清单尚未审核；未扫描硬件') from exc
        detectors = settings.get('Detectors', {}).get('detectors', {})
        if (not isinstance(detectors, dict) or not detectors
                or any(type(value) is not bool for value in detectors.values())
                or {name for name, value in detectors.items() if value} != {detector}):
            raise RuntimeError('Windows OpenRGB 只能启用指定 detector：' + detector)
        digest = hashlib.sha256(json.dumps(sorted(detectors), ensure_ascii=False,
                                          separators=(',', ':')).encode('utf-8')).hexdigest()
        if (marker.get('detectors_sha256') != digest
                or marker.get('executable_sha256') != hashlib.sha256(self.executable.read_bytes()).hexdigest()):
            raise RuntimeError('Windows OpenRGB 版本或完整 detector 集合已改变；未扫描硬件')
        if settings.get('Client', {}).get('clients'):
            raise RuntimeError('Home OpenRGB 配置不能含额外网络客户端')
        for key in ('QMKOpenRGBDevices', 'QMKVialRGBDevices'):
            if settings.get(key, {}).get('devices'):
                raise RuntimeError('Home OpenRGB 配置不能含动态 QMK detector')

    @staticmethod
    def run_cli(command, run):
        import win32api
        if '--config' in command:
            directory = Path(command[command.index('--config')+1])
            if not directory.is_absolute() or not directory.is_dir():
                raise RuntimeError('Windows OpenRGB 必须使用已存在的绝对配置目录；未启动检测')
        powershell = str(Path(win32api.GetSystemDirectory()) / 'WindowsPowerShell/v1.0/powershell.exe')
        script = Path(__file__).resolve().parents[2] / 'scripts/windows/windows_rgb_cli.ps1'
        encoded = base64.b64encode(json.dumps(command[1:], ensure_ascii=False).encode('utf-8')).decode('ascii')
        result = run([powershell, '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
                      '-ExecutionPolicy', 'Bypass', '-File', str(script), '-Executable', command[0],
                      '-ArgumentsBase64', encoded, '-TimeoutMilliseconds', '4000'],
                     capture_output=True, text=True, encoding='utf-8', errors='replace',
                     timeout=7, check=True, creationflags=0x08000000)
        # ConPTY contributes terminal controls, never part of device identity.
        result.stdout = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', result.stdout)
        result.stdout = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', result.stdout)
        result.stdout = result.stdout.replace('\r\n', '\n').replace('\r', '\n')
        return result

    def case_sync_off(self, cancelled=None):
        return _HidApi(self.executable).sync_off(cancelled)

    def gpu_set(self, enabled, cancelled=None):
        return _gpu_master_set(enabled, cancelled)

    @staticmethod
    def respeaker_set(enabled, cancelled=None):
        from spica.adapters.windows_respeaker_lights import set_respeaker_lights
        return set_respeaker_lights(enabled, cancelled)

    def ram_set(self, enabled, cancelled=None):
        self.require_ram_driver()
        from spica.adapters.windows_ram_task import WindowsRamCleanupPending, WindowsRamTask
        try:
            return WindowsRamTask().set(enabled, cancelled)
        except WindowsRamCleanupPending as exc:
            return dict(status='cleanup_pending', reason=str(exc), owner=exc.owner)

    @staticmethod
    def ram_cleanup_settled(owner):
        from spica.adapters.windows_ram_task import WindowsRamTask
        return WindowsRamTask().cleanup_settled(owner)

    @staticmethod
    def require_ram_driver():
        import win32service
        manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
        service = None
        try:
            try:
                service = win32service.OpenService(manager, 'PawnIO', win32service.SERVICE_QUERY_STATUS)
            except Exception as exc:
                raise RuntimeError('ENE DRAM 需要已安装的官方 PawnIO 驱动；未扫描 SMBus') from exc
            if win32service.QueryServiceStatus(service)[1] != win32service.SERVICE_RUNNING:
                raise RuntimeError('PawnIO 驱动未运行；未扫描 SMBus')
        finally:
            if service is not None:
                win32service.CloseServiceHandle(service)
            win32service.CloseServiceHandle(manager)
