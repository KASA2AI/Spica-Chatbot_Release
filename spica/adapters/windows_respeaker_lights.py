"""RGB-only ReSpeaker control through an already-bound Windows WinUSB interface.

This does not install drivers, select configurations, claim audio, tune DSP, or
enter DFU. Seeed's three pixel-ring commands are sent to endpoint zero through
the unique MI_03 function of the same physical parent as the working UAC device.
The Linux PyUSB/tuning implementation remains separate.
"""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import sys
import uuid


_CONTROL = r'USB\VID_2886&PID_0018&MI_03'
_AUDIO = r'USB\VID_2886&PID_0018&MI_00'


class _Guid(c.Structure):
    _fields_ = [('value', c.c_ubyte * 16)]


class _Interface(c.Structure):
    _fields_ = [('size', w.DWORD), ('guid', _Guid), ('flags', w.DWORD), ('reserved', c.c_void_p)]


class _DeviceInfo(c.Structure):
    _fields_ = [('size', w.DWORD), ('guid', _Guid), ('instance', w.DWORD), ('reserved', c.c_void_p)]


class _Descriptor(c.Structure):
    _pack_ = 1
    _fields_ = [(name, c.c_ubyte) for name in (
        'length', 'type', 'number', 'alternate', 'endpoints', 'kind', 'subclass', 'protocol', 'string')]


class _Setup(c.Structure):
    _pack_ = 1
    _fields_ = [('request_type', c.c_ubyte), ('request', c.c_ubyte), ('value', c.c_ushort),
                ('index', c.c_ushort), ('length', c.c_ushort)]


def _function(library, name, result, arguments):
    function = getattr(library, name)
    function.restype, function.argtypes = result, arguments
    return function


def _check(value):
    if not value:
        raise c.WinError(c.get_last_error())


def _choose_binding(controls, audio):
    if len(controls) != 1 or len(audio) != 1:
        raise RuntimeError('无法唯一确认 ReSpeaker 的控制与音频接口')
    control, microphone = controls[0], audio[0]
    if (control['service'].casefold() != 'winusb' or microphone['service'].casefold() != 'usbaudio'
            or control['parent'] != microphone['parent']
            or any(row['problem'] or not row['status'] & 8 for row in (control, microphone))):
        raise RuntimeError('ReSpeaker 控制驱动未就绪或不属于正在使用的音频设备')
    return control, microphone


def _binding():
    import winreg
    cm = c.WinDLL('cfgmgr32.dll', use_last_error=True, winmode=0x800)
    locate = _function(cm, 'CM_Locate_DevNodeW', w.ULONG, [c.POINTER(w.DWORD), w.LPWSTR, w.ULONG])
    parent = _function(cm, 'CM_Get_Parent', w.ULONG, [c.POINTER(w.DWORD), w.DWORD, w.ULONG])
    status = _function(cm, 'CM_Get_DevNode_Status', w.ULONG,
                       [c.POINTER(w.ULONG), c.POINTER(w.ULONG), w.DWORD, w.ULONG])

    def present(hardware):
        rows = []
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, 'SYSTEM\\CurrentControlSet\\Enum\\' + hardware)
        except FileNotFoundError:
            return rows
        with key:
            count = winreg.QueryInfoKey(key)[0]
            if count > 128:
                raise RuntimeError('ReSpeaker 历史设备实例数量异常')
            for index in range(count):
                name = winreg.EnumKey(key, index)
                instance = hardware + '\\' + name
                node = w.DWORD()
                # No PHANTOM flag: old disconnected instances cannot be selected.
                error = locate(c.byref(node), c.create_unicode_buffer(instance), 0)
                if error == 13:  # CR_NO_SUCH_DEVNODE
                    continue
                if error:
                    raise RuntimeError('ReSpeaker PnP 实例查询失败')
                ancestor, flags, problem = w.DWORD(), w.ULONG(), w.ULONG()
                if parent(c.byref(ancestor), node, 0) or status(c.byref(flags), c.byref(problem), node, 0):
                    raise RuntimeError('ReSpeaker PnP 状态查询失败')
                with winreg.OpenKey(key, name) as device:
                    try:
                        service = winreg.QueryValueEx(device, 'Service')[0]
                    except FileNotFoundError:
                        service = ''
                rows.append(dict(instance=instance, node=node.value, parent=ancestor.value,
                                 service=service, status=flags.value, problem=problem.value))
        return rows

    return _choose_binding(present(_CONTROL), present(_AUDIO))


def _device_path(binding):
    import winreg
    instance = binding['instance']
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
            'SYSTEM\\CurrentControlSet\\Enum\\' + instance + '\\Device Parameters') as key:
        guids = winreg.QueryValueEx(key, 'DeviceInterfaceGUIDs')[0]
    if not isinstance(guids, list) or len(guids) != 1:
        raise RuntimeError('ReSpeaker WinUSB 接口 GUID 不唯一')
    guid = _Guid((c.c_ubyte * 16).from_buffer_copy(uuid.UUID(guids[0]).bytes_le))
    api = c.WinDLL('setupapi.dll', use_last_error=True, winmode=0x800)
    get = _function(api, 'SetupDiGetClassDevsW', c.c_void_p, [c.POINTER(_Guid), w.LPCWSTR, w.HWND, w.DWORD])
    enum = _function(api, 'SetupDiEnumDeviceInterfaces', w.BOOL,
        [c.c_void_p, c.c_void_p, c.POINTER(_Guid), w.DWORD, c.POINTER(_Interface)])
    detail = _function(api, 'SetupDiGetDeviceInterfaceDetailW', w.BOOL,
        [c.c_void_p, c.POINTER(_Interface), c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.POINTER(_DeviceInfo)])
    identify = _function(api, 'SetupDiGetDeviceInstanceIdW', w.BOOL,
        [c.c_void_p, c.POINTER(_DeviceInfo), w.LPWSTR, w.DWORD, c.POINTER(w.DWORD)])
    destroy = _function(api, 'SetupDiDestroyDeviceInfoList', w.BOOL, [c.c_void_p])
    devices = get(c.byref(guid), instance, None, 0x12)
    if devices == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    try:
        interface = _Interface()
        interface.size = c.sizeof(interface)
        _check(enum(devices, None, c.byref(guid), 0, c.byref(interface)))
        needed = w.DWORD()
        detail(devices, c.byref(interface), None, 0, c.byref(needed), None)
        if c.get_last_error() != 122 or not 8 < needed.value < 32768:
            raise RuntimeError('ReSpeaker 接口路径长度无效')
        buffer = c.create_string_buffer(needed.value)
        c.cast(buffer, c.POINTER(w.DWORD))[0] = 8 if c.sizeof(c.c_void_p) == 8 else 6
        info = _DeviceInfo()
        info.size = c.sizeof(info)
        _check(detail(devices, c.byref(interface), buffer, len(buffer), c.byref(needed), c.byref(info)))
        name = c.create_unicode_buffer(1024)
        _check(identify(devices, c.byref(info), name, len(name), None))
        if name.value.casefold() != instance.casefold() or info.instance != binding['node']:
            raise RuntimeError('ReSpeaker 打开前的接口身份已改变')
        other = _Interface()
        other.size = c.sizeof(other)
        if enum(devices, None, c.byref(guid), 1, c.byref(other)) or c.get_last_error() != 259:
            raise RuntimeError('ReSpeaker WinUSB 路径不唯一')
        return c.wstring_at(c.addressof(buffer) + 4)
    finally:
        destroy(devices)


class _WinUsb:
    def __enter__(self):
        if sys.platform != 'win32':
            raise OSError('ReSpeaker WinUSB requires Windows')
        self.file, self.usb = None, c.c_void_p()
        kernel = c.WinDLL('kernel32.dll', use_last_error=True, winmode=0x800)
        api = c.WinDLL('winusb.dll', use_last_error=True, winmode=0x800)
        create = _function(kernel, 'CreateFileW', w.HANDLE,
                           [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE])
        self.close_file = _function(kernel, 'CloseHandle', w.BOOL, [w.HANDLE])
        self.free = _function(api, 'WinUsb_Free', w.BOOL, [c.c_void_p])
        initialize = _function(api, 'WinUsb_Initialize', w.BOOL, [w.HANDLE, c.POINTER(c.c_void_p)])
        query = _function(api, 'WinUsb_QueryInterfaceSettings', w.BOOL,
                          [c.c_void_p, c.c_ubyte, c.POINTER(_Descriptor)])
        get_descriptor = _function(api, 'WinUsb_GetDescriptor', w.BOOL,
            [c.c_void_p, c.c_ubyte, c.c_ubyte, c.c_ushort, c.c_void_p, w.ULONG, c.POINTER(w.ULONG)])
        policy = _function(api, 'WinUsb_SetPipePolicy', w.BOOL,
                           [c.c_void_p, c.c_ubyte, w.ULONG, w.ULONG, c.c_void_p])
        self.transfer = _function(api, 'WinUsb_ControlTransfer', w.BOOL,
            [c.c_void_p, _Setup, c.c_void_p, w.ULONG, c.POINTER(w.ULONG), c.c_void_p])
        self.binding = _binding()
        try:
            self.file = create(_device_path(self.binding[0]), 0xC0000000, 3, None, 3, 0x40000000, None)
            if self.file == w.HANDLE(-1).value:
                self.file = None
                raise c.WinError(c.get_last_error())
            _check(initialize(self.file, c.byref(self.usb)))
            descriptor = _Descriptor()
            _check(query(self.usb, 0, c.byref(descriptor)))
            if (descriptor.length, descriptor.type, descriptor.number, descriptor.alternate,
                    descriptor.endpoints, descriptor.kind, descriptor.subclass, descriptor.protocol) != (9, 4, 3, 0, 0, 255, 255, 255):
                raise RuntimeError('ReSpeaker 打开后的描述符不是独立 LED 控制接口')
            device, received = (c.c_ubyte * 18)(), w.ULONG()
            _check(get_descriptor(self.usb, 1, 0, 0, device, len(device), c.byref(received)))
            if received.value != 18 or bytes(device)[8:12] != bytes.fromhex('86281800'):
                raise RuntimeError('ReSpeaker 打开后的设备 ID 不符')
            timeout = w.ULONG(1000)
            _check(policy(self.usb, 0, 3, c.sizeof(timeout), c.byref(timeout)))
            if _binding() != self.binding:
                raise RuntimeError('ReSpeaker 控制或音频接口已改变')
            return self
        except BaseException:
            self.__exit__()
            raise

    def write(self, command, payload):
        if (command, payload) not in (
            (0x20, bytes([0])), (0x20, bytes([20])), (0x22, bytes([0])), (0x22, bytes([1])),
            (1, bytes([0, 0, 0, 0])), (1, bytes([0, 128, 255, 0]))):
            raise ValueError('Only the fixed ReSpeaker LED frames are supported')
        if _binding() != self.binding:
            raise RuntimeError('ReSpeaker 写入前的接口身份已改变')
        data = (c.c_ubyte * len(payload)).from_buffer_copy(payload)
        packet = _Setup(0x40, 0, command, 0x1C, len(payload))
        count = w.ULONG()
        _check(self.transfer(self.usb, packet, data, len(data), c.byref(count), None))
        if count.value != len(payload):
            raise RuntimeError('ReSpeaker 灯控写入长度不符')

    def __exit__(self, *error):
        try:
            if self.usb.value:
                handle, self.usb = self.usb, c.c_void_p()
                _check(self.free(handle))
        finally:
            if self.file is not None:
                handle, self.file = self.file, None
                _check(self.close_file(handle))


def set_respeaker_lights(enabled, cancelled=None, *, transport=_WinUsb):
    if type(enabled) is not bool:
        raise ValueError('LED state must be boolean')
    if cancelled is not None and cancelled.is_set():
        return False
    frames = ((0x20, bytes([20 if enabled else 0])), (0x22, bytes([int(enabled)])),
              (1, bytes([0, 128, 255, 0]) if enabled else bytes(4)))
    with transport() as device:
        for command, payload in frames:
            if cancelled is not None and cancelled.is_set():
                return False
            device.write(command, payload)
    return True  # Transfer acknowledgement; physical darkness is separate evidence.
