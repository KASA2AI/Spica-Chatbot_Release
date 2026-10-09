"""PortAudio input identities; device indexes are resolved anew on every open."""
from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
import re


def device_identity(info, host_name):
    # ALSA card numbers change after replug/reboot. Identical names remain
    # ambiguous and are rejected rather than silently choosing another mic.
    name = re.sub(r"\s*\(hw:\d+,\d+\)$", "", str(info["name"])).strip()
    return json.dumps([str(host_name), name, int(info["maxInputChannels"])], ensure_ascii=False, separators=(",", ":"))


def input_devices(audio):
    devices = []
    for index in range(audio.get_device_count()):
        info = audio.get_device_info_by_index(index)
        if int(info.get("maxInputChannels", 0)) <= 0:
            continue
        host = audio.get_host_api_info_by_index(int(info["hostApi"]))["name"]
        devices.append({"id": device_identity(info, host), "name": str(info["name"]),
                        "host": host, "index": index, "channels": int(info["maxInputChannels"])})
    return devices


def _alsa_capture_devices(directory=Path('/proc/asound')):
    """ALSA metadata stays visible while an exclusive capture is busy."""
    try:
        cards = {int(index): (identity.strip(), name.strip()) for index, identity, name in re.findall(
            r'^\s*(\d+)\s+\[([^]]+)\]\s*:\s*[^\n]*? - (.+)$',
            (directory / 'cards').read_text(), re.MULTILINE)}
        result = []
        for line in (directory / 'pcm').read_text().splitlines():
            match = re.match(r'(\d+)-(\d+):\s*([^:]+):', line)
            if match is None or not re.search(r'\bcapture [1-9]\d*', line):
                continue
            card, pcm, name = int(match[1]), int(match[2]), match[3].strip()
            if card not in cards:
                continue
            identity, label = cards[card]
            result.append({'id': json.dumps(['alsa_pcm', identity, pcm], separators=(',', ':')),
                'name': f'{label} · {name}', 'host': 'ALSA', 'card': card, 'pcm': pcm})
        return result
    except OSError:
        return []  # Non-ALSA platforms retain PortAudio's native identities.


def _alsa_address(device):
    match = re.search(r'\(hw:(\d+),(\d+)\)$', device['name']) if device['host'] == 'ALSA' else None
    return (int(match[1]), int(match[2])) if match else None


def _discover_inputs(audio):
    available, result = input_devices(audio), []
    hardware = _alsa_capture_devices()
    addresses = {(item['card'], item['pcm']) for item in hardware}
    for item in hardware:
        matches = [value for value in available if _alsa_address(value) == (item['card'], item['pcm'])]
        result.append({**item, 'name': item['name'] + ('' if matches else '（占用中或暂不可录音）'),
                       'aliases': [value['id'] for value in matches]})
    result.extend(value for value in available if _alsa_address(value) not in addresses)
    return result


def resolve_input_device(audio, identity):
    if not identity:
        return None
    try:
        binding = json.loads(identity)
    except (TypeError, ValueError):
        binding = None
    if isinstance(binding, list) and len(binding) == 3 and binding[0] == 'alsa_pcm':
        hardware = [item for item in _alsa_capture_devices() if item['id'] == identity]
        address = (hardware[0]['card'], hardware[0]['pcm']) if len(hardware) == 1 else None
        matches = [device for device in input_devices(audio) if address is not None and _alsa_address(device) == address]
    else:
        matches = [device for device in input_devices(audio) if device["id"] == identity]
    if len(matches) != 1:
        raise RuntimeError("所选麦克风已断开、被占用或存在同名设备，未切换到其他麦克风。请检查设备后重试。")
    return matches[0]["index"]


def _enumerate(output):
    audio = None
    try:
        import pyaudio
        audio = pyaudio.PyAudio()
        output.send({"devices": _discover_inputs(audio)})
    except Exception:
        output.send({"error": "无法读取麦克风列表，请检查 PyAudio 和系统音频服务。"})
    finally:
        if audio is not None:
            audio.terminate()
        output.close()


def list_input_devices(timeout=5.):
    """Enumeration can block in a native driver; own a bounded child process."""
    context = multiprocessing.get_context("spawn")
    incoming, outgoing = context.Pipe(duplex=False)
    process = context.Process(target=_enumerate, args=(outgoing,), daemon=True, name="spica-input-list")
    started = False
    try:
        process.start()
        started = True
        outgoing.close()
        if not incoming.poll(timeout):
            raise RuntimeError("枚举麦克风超时，请检查系统音频服务后重试。")
        result = incoming.recv()
        if "error" in result:
            raise RuntimeError(result["error"])
        return result["devices"]
    finally:
        incoming.close()
        outgoing.close()
        if started:
            process.join(.1)
            if process.is_alive():
                process.terminate()
                process.join(.3)
            if process.is_alive():
                process.kill()
                process.join(.3)
        process.close()
