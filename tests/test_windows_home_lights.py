"""Native RGB protocol fixtures only: never enumerate or operate real hardware."""
import ctypes
import hashlib
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from spica.adapters.windows_home_lights import (
    WindowsHomeLights, _gpu_master_set, _HidApi, _HidInfo, _I2CInfo, _NvApi,
)


def _profile(tmp_path):
    executable = tmp_path / 'OpenRGB.exe'
    executable.write_bytes(b'synthetic executable fingerprint')
    directory = tmp_path / 'profile'
    directory.mkdir()
    document = {'Detectors': {'detectors': {'ENE SMBus DRAM': True, 'Other HID device': False}}}
    (directory / 'OpenRGB.json').write_text(json.dumps(document), encoding='utf-8')
    marker = dict(executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
                  detectors_sha256=hashlib.sha256(json.dumps(sorted(document['Detectors']['detectors']),
                      ensure_ascii=False, separators=(',', ':')).encode()).hexdigest())
    (directory / 'WindowsOpenRGB.json').write_text(json.dumps(marker), encoding='utf-8')
    return WindowsHomeLights(executable), directory, document


@pytest.mark.parametrize('change', ['binary', 'catalog', 'enabled', 'client', 'qmk', 'vial', 'missing_marker'])
def test_profile_requires_audited_binary_and_complete_single_detector_set(tmp_path, change):
    adapter, directory, document = _profile(tmp_path)
    adapter.validate_profile(directory, 'ENE SMBus DRAM')
    if change == 'binary':
        adapter.executable.write_bytes(b'new version may add enabled detectors')
    elif change == 'catalog':
        del document['Detectors']['detectors']['Other HID device']
    elif change == 'enabled':
        document['Detectors']['detectors']['Other HID device'] = True
    elif change == 'client':
        document['Client'] = {'clients': [{'ip': '127.0.0.1', 'port': 6742}]}
    elif change in ('qmk', 'vial'):
        document['QMKOpenRGBDevices' if change == 'qmk' else 'QMKVialRGBDevices'] = {'devices': [{'name': 'extra'}]}
    else:
        (directory / 'WindowsOpenRGB.json').unlink()
    (directory / 'OpenRGB.json').write_text(json.dumps(document), encoding='utf-8')
    with pytest.raises(RuntimeError):
        adapter.validate_profile(directory, 'ENE SMBus DRAM')


@pytest.mark.parametrize('write_count', [65, 353, 0, -1])
def test_case_hid_targets_only_rgb_sync_and_releases_handle(monkeypatch, write_count):
    from spica.adapters import windows_home_lights as module
    info = _HidInfo(path=b'bound device', vendor_id=0x0CF2, product_id=0xA102,
                    interface_number=1, usage_page=0xFF72, usage=0xA1)
    pointer = ctypes.pointer(info)
    packets = []

    def write(handle, buffer, size):
        assert handle == 123
        packets.append(ctypes.string_at(buffer, size))
        return write_count

    adapter = _HidApi.__new__(_HidApi)
    adapter.api = SimpleNamespace(hid_enumerate=Mock(return_value=pointer),
        hid_free_enumeration=Mock(), hid_open_path=Mock(return_value=123),
        hid_get_device_info=Mock(return_value=pointer), hid_write=write, hid_close=Mock())
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    if write_count < 65:
        with pytest.raises(RuntimeError, match='写入未完成'):
            adapter.sync_off()
    else:
        assert adapter.sync_off()
    assert packets == [bytes((0xE0, 0x10, 0x61)) + bytes(62)]
    adapter.api.hid_enumerate.assert_called_once_with(0x0CF2, 0xA102)
    adapter.api.hid_close.assert_called_once_with(123)
    adapter.api.hid_free_enumeration.assert_called_once_with(pointer)


@pytest.mark.parametrize('failure', ['wrong_interface', 'duplicate', 'cancelled', 'changed_after_open'])
def test_case_hid_refuses_ambiguous_changed_or_cancelled_target(failure):
    info = _HidInfo(path=b'bound', vendor_id=0x0CF2, product_id=0xA102,
                    interface_number=1, usage_page=0xFF72, usage=0xA1)
    second = _HidInfo(path=b'other', vendor_id=0x0CF2, product_id=0xA102,
                      interface_number=1, usage_page=0xFF72, usage=0xA1)
    if failure == 'wrong_interface':
        info.interface_number = 0
    if failure == 'duplicate':
        info.next = ctypes.pointer(second)
    stop = threading.Event()
    if failure == 'cancelled':
        stop.set()
    adapter = _HidApi.__new__(_HidApi)
    adapter.api = SimpleNamespace(hid_enumerate=Mock(return_value=ctypes.pointer(info)),
        hid_free_enumeration=Mock(), hid_open_path=Mock(return_value=123),
        hid_get_device_info=Mock(return_value=ctypes.pointer(second if failure == 'changed_after_open' else info)),
        hid_write=Mock(), hid_close=Mock())
    if failure == 'cancelled':
        assert adapter.sync_off(stop) is False
    else:
        with pytest.raises(RuntimeError):
            adapter.sync_off(stop)
    adapter.api.hid_write.assert_not_called()
    if failure == 'changed_after_open':
        adapter.api.hid_close.assert_called_once_with(123)
    else:
        adapter.api.hid_open_path.assert_not_called()


class _Gpu:
    def __init__(self, *, enabled=True, firmware=b'N675A-1062', refuse=False):
        self.enabled, self.firmware, self.refuse = enabled, firmware, refuse
        self.writes, self.closed = [], False

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self.closed = True

    def transfer(self, data=None):
        if data is not None:
            self.writes.append(data)
            if len(data) == 21 and not self.refuse:
                self.enabled = bool(data[1])
            return data
        if self.writes[-1][1] == 0xF1:
            return self.firmware.ljust(32, b'\0')
        state = bytearray(32)
        state[0], state[4], state[5] = int(self.enabled), self.writes[-1][5], 6
        return bytes(state)


@pytest.mark.parametrize('enabled', [False, True])
def test_gpu_master_uses_only_bound_firmware_led_flag_and_retains_effects(enabled):
    gpu = _Gpu(enabled=not enabled)
    assert _gpu_master_set(enabled, api_factory=lambda: gpu)
    commands = [frame for frame in gpu.writes if len(frame) == 21]
    assert commands == [bytes((0xA0, int(enabled))) + bytes(19)]
    assert all(frame[1] in (0xF0, 0xF1) for frame in gpu.writes if len(frame) != 21)
    assert gpu.closed


@pytest.mark.parametrize('failure', ['firmware', 'readback', 'cancelled', 'already_set'])
def test_gpu_identity_cancellation_readback_and_release(failure):
    gpu = _Gpu(enabled=failure != 'already_set',
               firmware=b'unknown' if failure == 'firmware' else b'N675A-1062',
               refuse=failure == 'readback')
    stop = threading.Event()
    if failure == 'cancelled':
        stop.set()
    if failure in ('firmware', 'readback'):
        with pytest.raises(RuntimeError):
            _gpu_master_set(False, stop, api_factory=lambda: gpu)
    else:
        assert _gpu_master_set(False, stop, api_factory=lambda: gpu) is (failure == 'already_set')
    if failure != 'readback':
        assert not any(len(frame) == 21 for frame in gpu.writes)
    assert gpu.closed


def test_nvapi_i2c_frame_has_only_the_dedicated_rgb_port_and_address():
    adapter = _NvApi.__new__(_NvApi)
    adapter.gpu = 123
    calls = []

    def transfer(handle, value, unknown):
        info = ctypes.cast(value, ctypes.POINTER(_I2CInfo)).contents
        assert handle == 123
        assert info.version == ctypes.sizeof(_I2CInfo) | (3 << 16)
        assert (info.device_address, info.port_id, info.port_id_set, info.is_ddc_port) == (0x92, 1, 1, 0)
        assert not info.register and info.register_size == 0 and info.display_mask == 0
        assert info.speed == 0xFFFF and info.speed_khz == 0
        calls.append(bytes(info.data[:info.size]))
        return 0

    adapter.read = adapter.write = transfer
    assert adapter.transfer(bytes((0xA0, 0xF1))) == bytes((0xA0, 0xF1))
    assert adapter.transfer() == bytes(32)
    assert calls == [bytes((0xA0, 0xF1)), bytes(32)]
