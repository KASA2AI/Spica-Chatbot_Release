import json
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from hardware.respeaker.control import ReSpeakerControl
from spica.adapters.home_night_lights import (
    CASE_CONTROLLER, GPU_CONTROLLER, GPU_PCI_ID, MOTHERBOARD_CONTROLLER, RAM_CONTROLLER,
    HomeNightLights, _case_sync_off, _gpu_master_off, _gpu_present,
)
from spica.config.home import HomeConfig, HomeLightsConfig


def test_bedtime_leds_use_only_led_commands_and_the_named_case_controller(tmp_path, monkeypatch):
    control = object.__new__(ReSpeakerControl)
    control._dev, control.close = Mock(), Mock()
    monkeypatch.setattr('hardware.respeaker.control.ReSpeakerControl', lambda: control)
    directory = tmp_path / 'openrgb'
    directory.mkdir()
    (directory / 'OpenRGB.json').write_text(json.dumps({
        'Detectors': {'detectors': {CASE_CONTROLLER: True, 'Unrelated RGB device': False}},
    }))
    commands = []
    def run(args, **kwargs):
        assert kwargs == dict(capture_output=True, text=True, timeout=3, check=True)
        commands.append(args)
        return SimpleNamespace(stdout='0: '+CASE_CONTROLLER+'\n')
    config = HomeConfig(data_directory=str(tmp_path), lights=HomeLightsConfig(
        bedtime_respeaker_leds_off=True, bedtime_case_leds_off=True,
        openrgb_executable='/opt/openrgb/AppRun'))
    sync_off = Mock()
    assert HomeNightLights(config, run=run, case_sync_off=sync_off, effective_platform='linux').off() == {
        'respeaker': {'status': 'requested'}, 'case': {'status': 'requested'},
    }
    assert control._dev.ctrl_transfer.call_args_list == [
        call(0x40, 0, 0x20, 0x1C, [0], 1000),
        call(0x40, 0, 0x22, 0x1C, [0], 1000),
        call(0x40, 0, 1, 0x1C, [0, 0, 0, 0], 1000),
    ]
    control.close.assert_called_once()
    sync_off.assert_called_once_with()
    assert commands == [
        ['/opt/openrgb/AppRun', '--noautoconnect', '--config', str(directory), '--list-devices'],
        ['/opt/openrgb/AppRun', '--noautoconnect', '--config', str(directory),
         *[part for zone in range(8) for part in (
             '--device', CASE_CONTROLLER, '--zone', str(zone), '--size', '96',
             '--mode', 'Static', '--brightness', '0', '--color', '000000')]],
    ]


def test_unavailable_leds_release_usb_and_never_scan_unrelated_rgb_devices(tmp_path, monkeypatch):
    control = SimpleNamespace(turn_leds_off=Mock(side_effect=OSError('USB unavailable')), close=Mock())
    monkeypatch.setattr('hardware.respeaker.control.ReSpeakerControl', lambda: control)
    directory = tmp_path / 'openrgb'
    directory.mkdir()
    (directory / 'OpenRGB.json').write_text(json.dumps({
        'Detectors': {'detectors': {CASE_CONTROLLER: True, 'Unrelated RGB device': True}},
    }))
    run = Mock()
    config = HomeConfig(data_directory=str(tmp_path), lights=HomeLightsConfig(
        bedtime_respeaker_leds_off=True, bedtime_case_leds_off=True))
    results = HomeNightLights(config, run=run, effective_platform='linux').off()
    assert results['respeaker']['status'] == results['case']['status'] == 'unconfirmed'
    control.close.assert_called_once()
    run.assert_not_called()
    assert HomeNightLights(HomeConfig(), run=run).off() == {}


def test_daytime_restores_ring_and_case_lighting_without_audio_or_fan_commands(tmp_path, monkeypatch):
    control = object.__new__(ReSpeakerControl)
    control._dev, control.close = Mock(), Mock()
    monkeypatch.setattr('hardware.respeaker.control.ReSpeakerControl', lambda: control)
    directory = tmp_path/'openrgb'
    directory.mkdir()
    (directory/'OpenRGB.json').write_text(json.dumps({'Detectors': {'detectors': {CASE_CONTROLLER: True}}}))
    run = Mock(return_value=SimpleNamespace(stdout='0: '+CASE_CONTROLLER+'\n'))
    config = HomeConfig(data_directory=str(tmp_path), lights=HomeLightsConfig(
        bedtime_respeaker_leds_off=True, bedtime_case_leds_off=True, openrgb_executable='/opt/openrgb/AppRun'))
    result = HomeNightLights(config, run=run, case_sync_off=Mock(), effective_platform='linux').on()
    assert result == {'respeaker': {'status': 'requested'}, 'case': {'status': 'requested'}}
    assert control._dev.ctrl_transfer.call_args_list == [
        call(0x40, 0, 0x20, 0x1C, [20], 1000),
        call(0x40, 0, 0x22, 0x1C, [1], 1000),
        call(0x40, 0, 1, 0x1C, [0, 128, 255, 0], 1000),
    ]
    control.close.assert_called_once()
    command = run.call_args_list[-1].args[0]
    assert command.count('Rainbow Wave') == command.count('100') == 8
    assert '--speed' not in command and '--color' not in command


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux hidraw transport')
def test_case_sync_off_targets_one_hub_and_writes_only_the_rgb_command(tmp_path, monkeypatch):
    import pytest
    sysfs, devices = tmp_path/'sys', tmp_path/'dev'
    entry = sysfs/'hidraw6'/'device'
    entry.mkdir(parents=True)
    devices.mkdir()
    (entry/'uevent').write_text('HID_ID=0003:00000CF2:0000A102\n')
    output = devices/'hidraw6'
    output.touch()
    monkeypatch.setattr('spica.adapters.home_night_lights.time.sleep', lambda _: None)
    _case_sync_off(sysfs=sysfs, devices=devices)
    assert output.read_bytes() == bytes((0xE0, 0x10, 0x61, 0, 0, 0, 0))
    other = sysfs/'hidraw7'/'device'
    other.mkdir(parents=True)
    (other/'uevent').write_text((entry/'uevent').read_text())
    with pytest.raises(RuntimeError, match='唯一确认'):
        _case_sync_off(sysfs=sysfs, devices=devices)


def test_gpu_and_case_failures_are_independent_and_gpu_detection_is_scoped(tmp_path):
    for directory, controller in [('openrgb', CASE_CONTROLLER), ('openrgb-gpu', GPU_CONTROLLER)]:
        target = tmp_path/directory
        target.mkdir()
        (target/'OpenRGB.json').write_text(json.dumps({'Detectors': {'detectors': {controller: True}}}))
    commands = []
    def run(args, **kwargs):
        commands.append(args)
        return SimpleNamespace(stdout=('0: '+GPU_CONTROLLER+'\n  Location: I2C: /dev/i2c-8, address 0x49\n'
            if 'openrgb-gpu' in args[3] else '0: '+CASE_CONTROLLER+'\n'))
    config = HomeConfig(data_directory=str(tmp_path), lights=HomeLightsConfig(
        bedtime_case_leds_off=True, bedtime_gpu_leds_off=True, openrgb_executable='/opt/openrgb/AppRun'))
    gpu_off = Mock()
    lights = HomeNightLights(config, run=run, gpu_present=lambda: True, gpu_off=gpu_off, effective_platform='linux',
                            case_sync_off=Mock(side_effect=OSError('hub unavailable')))
    result = lights.off()
    assert result['case']['status'] == 'unconfirmed' and result['gpu']['status'] == 'requested'
    gpu_off.assert_called_once_with('/dev/i2c-8')
    assert commands[-1][-1] == '--list-devices'  # Black/zero was not sufficient on real hardware.
    assert lights.on()['gpu']['status'] == 'requested'
    assert commands[-1][-6:] == ['--device', GPU_CONTROLLER, '--mode', 'Rainbow', '--brightness', '100']
    assert not any('--speed' in command or '--profile' in command for command in commands)
    commands.clear()
    lights._gpu_present = lambda: False
    assert lights.off()['gpu']['status'] == 'unconfirmed'
    assert not any('openrgb-gpu' in part for command in commands for part in command)


def test_gpu_binding_requires_the_exact_installed_pci_identity(tmp_path):
    device = tmp_path/'bound-pci-device'
    device.mkdir()
    values = dict(vendor='0x10de', device='0x2684', subsystem_vendor='0x19da', subsystem_device='0x4675')
    for name, value in values.items():
        (device/name).write_text(value+'\n')
    assert _gpu_present(sysfs=tmp_path)
    (device/'subsystem_device').write_text('0x3675\n')
    assert not _gpu_present(sysfs=tmp_path)


@pytest.mark.parametrize('enabled', [False, True])
def test_close_between_detection_and_led_output_does_not_relight_the_case(tmp_path, enabled):
    import threading
    cancelled = threading.Event()
    directory = tmp_path/'openrgb'
    directory.mkdir()
    (directory/'OpenRGB.json').write_text(json.dumps({'Detectors': {'detectors': {CASE_CONTROLLER: True}}}))
    commands = []
    def run(args, **kwargs):
        commands.append(args)
        cancelled.set()
        return SimpleNamespace(stdout='0: '+CASE_CONTROLLER+'\n')
    sync = Mock()
    config = HomeConfig(data_directory=str(tmp_path), lights=HomeLightsConfig(
        bedtime_case_leds_off=True, openrgb_executable='/opt/openrgb/AppRun'))
    lights = HomeNightLights(config, run=run, case_sync_off=sync, effective_platform='linux')
    assert (lights.on if enabled else lights.off)(cancelled)['case']['status'] == 'cancelled'
    assert len(commands) == 1 and commands[0][-1] == '--list-devices'
    sync.assert_not_called()


@pytest.mark.parametrize('firmware,still_on', [(b'N675A-1062', False), (b'unknown', False), (b'N675A-1062', True)])
@pytest.mark.skipif(sys.platform != 'linux', reason='Linux sysfs/I2C transport')
def test_gpu_master_switch_checks_identity_and_ack_and_always_closes_bus(tmp_path, monkeypatch, firmware, still_on):
    pci = tmp_path/'pci'
    (pci/'i2c-8').mkdir(parents=True)
    for name, value in GPU_PCI_ID.items():
        (pci/name).write_text(value)
    entry = tmp_path/'i2c-dev'/'i2c-8'
    entry.mkdir(parents=True)
    (entry/'device').symlink_to(pci/'i2c-8')
    opened, closed = Mock(return_value=123), Mock()
    monkeypatch.setattr('spica.adapters.home_night_lights.os.open', opened)
    monkeypatch.setattr('spica.adapters.home_night_lights.os.close', closed)
    writes = []
    def transfer(descriptor, data=None):
        assert descriptor == 123
        if data is not None:
            writes.append(data)
            return data
        if writes[-1][1] == 0xF1:
            return firmware.ljust(32, b'\0')
        state = bytearray(32)
        state[0], state[4], state[5] = int(still_on), writes[-1][5], 6
        return bytes(state)
    if firmware != b'N675A-1062' or still_on:
        with pytest.raises(RuntimeError, match='固件身份|关闭未获确认'):
            _gpu_master_off('/dev/i2c-8', sysfs=entry.parent, transfer=transfer)
    else:
        _gpu_master_off('/dev/i2c-8', sysfs=entry.parent, transfer=transfer)
        assert [packet[5] for packet in writes if packet[1] == 0xF0] == [0, 1]
    mutations = [packet for packet in writes if packet[1] not in {0xF0, 0xF1}]
    assert mutations == ([bytes([0xA0]+[0]*20)] if firmware == b'N675A-1062' else [])
    closed.assert_called_once_with(123)
    (pci/'subsystem_device').write_text('0x3675')
    opened.reset_mock()
    with pytest.raises(RuntimeError, match='不属于'):
        _gpu_master_off('/dev/i2c-8', sysfs=entry.parent, transfer=transfer)
    opened.assert_not_called()


def test_ram_and_argb_use_scoped_detectors_and_restore_whole_channel_effects(tmp_path):
    from spica.config.manager import ConfigManager
    settings = tmp_path/'app.yaml'
    settings.write_text('home:\n  lights:\n    bedtime_ram_leds_off: true\n    bedtime_motherboard_leds_off: true\n')
    config = ConfigManager(config_path=str(settings)).load().home.model_copy(update={'data_directory': str(tmp_path)})
    for directory, detector in [('openrgb-ram', 'ENE SMBus DRAM'), ('openrgb-motherboard', 'ASUS Aura Motherboard')]:
        target = tmp_path/directory
        target.mkdir()
        (target/'OpenRGB.json').write_text(json.dumps({'Detectors': {'detectors': {detector: True}}}))
    commands = []
    def run(args, **kwargs):
        commands.append(args)
        return SimpleNamespace(stdout=('0: ENE DRAM\n1: ENE DRAM\n' if 'openrgb-ram' in args[3]
            else '0: '+MOTHERBOARD_CONTROLLER+'\n'))
    lights = HomeNightLights(config, run=run, effective_platform='linux')
    for enabled in (False, True):
        assert (lights.on() if enabled else lights.off()) == {
            'ram': {'status': 'requested'}, 'motherboard': {'status': 'requested'}}
        effects = [command for command in commands if '--mode' in command]
        mode = 'Rainbow' if enabled else 'Off'
        assert effects[-2][-4:] == ['--device', RAM_CONTROLLER, '--mode', mode]
        assert effects[-1][4:] == [part for zone in (1, 2, 3) for part in (
            '--device', MOTHERBOARD_CONTROLLER, '--zone', str(zone), '--size', '1', '--mode', mode)]
    # Missing one DIMM must not silently operate a partial/unexpected set;
    # failure remains independent of the motherboard restoration.
    commands.clear()
    def missing_ram(args, **kwargs):
        return SimpleNamespace(stdout=run(args, **kwargs).stdout.replace('1: ENE DRAM\n', ''))
    lights._run = missing_ram
    assert lights.on() == {'ram': {'status': 'unconfirmed', 'reason': '灯效控制器身份或数量不符：ENE DRAM'},
                           'motherboard': {'status': 'requested'}}
    assert not any('--mode' in cmd for cmd in commands if 'openrgb-ram' in cmd[3])


@pytest.mark.parametrize('directory,detector,controller,count,flag', [
    ('openrgb-gpu', GPU_CONTROLLER, GPU_CONTROLLER, 1, 'bedtime_gpu_leds_off'),
    ('openrgb-ram', 'ENE SMBus DRAM', RAM_CONTROLLER, 2, 'bedtime_ram_leds_off'),
    ('openrgb-motherboard', 'ASUS Aura Motherboard', MOTHERBOARD_CONTROLLER, 1, 'bedtime_motherboard_leds_off'),
])
@pytest.mark.parametrize('enabled', [False, True])
def test_close_during_new_controller_detection_prevents_relighting(tmp_path, directory, detector, controller, count, flag, enabled):
    target = tmp_path/directory
    target.mkdir()
    (target/'OpenRGB.json').write_text(json.dumps({'Detectors': {'detectors': {detector: True}}}))
    cancelled = threading.Event()
    def run(args, **kwargs):
        assert args[-1] == '--list-devices'
        cancelled.set()
        return SimpleNamespace(stdout=''.join(f'{i}: {controller}\n' for i in range(count)))
    config = HomeConfig(data_directory=str(tmp_path), lights=HomeLightsConfig(**{flag: True}))
    lights = HomeNightLights(config, run=run, gpu_present=lambda: True, effective_platform='linux')
    result = (lights.on if enabled else lights.off)(cancelled)
    assert all(value['status'] == 'cancelled' for value in result.values())


def test_openrgb_rejected_settings_are_not_reported_as_success_even_with_zero_exit():
    lights = HomeNightLights(HomeConfig(), run=Mock(return_value=SimpleNamespace(
        stdout='Wrong number of colors specified for mode Duet (circuit)\n')))
    with pytest.raises(RuntimeError, match='未接受'):
        lights._run_openrgb(['/opt/openrgb/AppRun'])


def test_windows_unknown_ram_cleanup_stops_other_controllers_and_preserves_owner(monkeypatch):
    from spica.adapters.windows_ram_task import WindowsRamCleanupPending
    owner = dict(installation_identity='bound-installation', instances={'off': ['bound-instance']})
    task = SimpleNamespace(set=Mock(side_effect=WindowsRamCleanupPending('not idle', owner)),
        cleanup_settled=Mock(return_value=False))
    monkeypatch.setattr('spica.adapters.windows_ram_task.WindowsRamTask', lambda: task)
    run = Mock(side_effect=AssertionError('OpenRGB must not overlap unresolved RAM work'))
    config = HomeConfig(lights=HomeLightsConfig(bedtime_ram_leds_off=True,
        bedtime_case_leds_off=True, bedtime_gpu_leds_off=True, bedtime_motherboard_leds_off=True))
    lights = HomeNightLights(config, run=run, effective_platform='windows')
    lights._windows.require_ram_driver = Mock()
    stop = threading.Event()
    result = lights.off(stop)
    assert result['ram'] == dict(status='cleanup_pending', reason='not idle', owner=owner)
    assert all(result[name] == {'status': 'cancelled'} for name in ('case', 'gpu', 'motherboard'))
    task.set.assert_called_once_with(False, stop)
    run.assert_not_called()
    assert lights.cleanup_settled({'ram': result['ram']}) is False
    task.cleanup_settled.assert_called_once_with(owner)
