"""Device selection shared by production playback and the settings test player."""


def output_label(device):
    description = str(device.description() or '').strip()
    identity = bytes(device.id()).decode('utf-8', errors='replace')
    if description.lower() not in {'', '(null)', 'null'}:
        return description
    if identity.startswith('alsa_output.usb-'):
        return 'USB 音频 · ' + identity.removeprefix('alsa_output.usb-').replace('_', ' ')
    if identity.startswith('alsa_output.pci-'):
        interface = 'HDMI / DisplayPort' if '.hdmi' in identity else '模拟音频'
        return interface + ' · ' + identity.removeprefix('alsa_output.pci-')
    return '音频输出 · ' + (identity or '未命名设备')


def resolve_output_device(media_devices, primary_id, fallback_id=''):
    """Return the device and its fixed binding; an empty binding follows default."""
    if not primary_id:
        return media_devices.defaultAudioOutput(), ''
    devices = {bytes(device.id()).hex(): device for device in media_devices.audioOutputs()}
    if primary_id in devices:
        return devices[primary_id], primary_id
    if fallback_id and fallback_id in devices:
        return devices[fallback_id], fallback_id
    raise RuntimeError('已选择的音箱当前不可用，请检查连接；未切换到其他输出设备')
