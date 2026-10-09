from __future__ import annotations

import importlib
import importlib.util
import math
import re
import sys
from pathlib import Path
from types import ModuleType

from spica.config.manager import respeaker_env_overrides


VID = 0x2886
PID = 0x0018
UDEV_RULE = 'SUBSYSTEM=="usb", ATTR{idVendor}=="2886", ATTR{idProduct}=="0018", MODE="0666"'
CLONE_HINT = "git clone https://github.com/respeaker/usb_4_mic_array.git"


class ReSpeakerControlError(RuntimeError):
    pass


class ReSpeakerDeviceMismatchError(ReSpeakerControlError):
    """Never recover an uncertain hardware identity by choosing another device."""


def usb_address_for_input(info):
    """Resolve this PortAudio ALSA PCM's current USB bus/address, not its ordinal."""
    match = re.search(r'\(hw:(\d+),\d+\)$', str(info.get('name', '')))
    if match is None:
        return None  # Other backends may use USB control only if it is unique.
    try:
        value = (Path('/proc/asound') / f'card{int(match[1])}' / 'usbbus').read_text().strip()
        bus, address = value.split('/')
        return int(bus), int(address)
    except (OSError, ValueError) as exc:
        raise ReSpeakerDeviceMismatchError('无法确认所选麦克风的 USB 身份，未使用其他设备。') from exc


class ReSpeakerControl:
    def __init__(self, *, usb_address: tuple[int, int] | None = None) -> None:
        usb_core, usb_util = self._load_pyusb()
        tuning_module = self._load_tuning_module()

        try:
            devices = list(usb_core.find(find_all=True, idVendor=VID, idProduct=PID))
        except Exception as exc:
            raise ReSpeakerControlError(self._format_usb_error(exc)) from exc

        if usb_address is not None:
            devices = [dev for dev in devices if (dev.bus, dev.address) == usb_address]
            if not devices:
                raise ReSpeakerDeviceMismatchError('所选麦克风对应的 ReSpeaker USB 设备已不可用，未使用其他设备。')
        if not devices:
            raise ReSpeakerControlError(
                "未找到 ReSpeaker USB Mic Array (2886:0018)。请确认设备已连接并可被当前用户访问。"
            )
        if len(devices) != 1:
            raise ReSpeakerDeviceMismatchError('无法唯一匹配 ReSpeaker USB 与录音设备，请选择可匹配的输入或使用普通麦克风模式。')
        dev = devices[0]

        tuning_cls = getattr(tuning_module, "Tuning", None)
        if tuning_cls is None:
            raise ReSpeakerControlError("tuning.py 中未找到 Tuning 类，无法读取 ReSpeaker 硬件 VAD。")

        try:
            self._tuning = tuning_cls(dev)
        except Exception as exc:
            raise ReSpeakerControlError(self._format_usb_error(exc)) from exc

        self._usb_util = usb_util
        self._dev = dev

    def is_voice(self) -> bool:
        try:
            return bool(self._tuning.is_voice())
        except Exception as exc:
            raise ReSpeakerControlError(self._format_usb_error(exc)) from exc

    def limit_agc_gain(self, maximum: float) -> None:
        """Apply a validated linear gain ceiling without boosting current gain."""
        try:
            if not math.isclose(float(self._tuning.read("AGCMAXGAIN")), maximum, rel_tol=1e-6):
                self._tuning.write("AGCMAXGAIN", maximum)
                if not math.isclose(float(self._tuning.read("AGCMAXGAIN")), maximum, rel_tol=1e-6):
                    raise ReSpeakerControlError("ReSpeaker AGC 最大增益写入后读回不一致。")
            if float(self._tuning.read("AGCGAIN")) > maximum:
                self._tuning.write("AGCGAIN", maximum)
        except ReSpeakerControlError:
            raise
        except Exception as exc:
            raise ReSpeakerControlError(self._format_usb_error(exc)) from exc

    def turn_leds_off(self) -> None:
        """ReSpeaker USB pixel-ring commands; leave audio and gain untouched."""
        try:
            # See respeaker/pixel_ring usb_pixel_ring_v2: brightness, VAD LED, RGB.
            for command, data in ((0x20, [0]), (0x22, [0]), (1, [0, 0, 0, 0])):
                self._dev.ctrl_transfer(0x40, 0, command, 0x1C, data, 1000)
        except Exception as exc:
            raise ReSpeakerControlError(self._format_usb_error(exc)) from exc

    def turn_leds_on(self) -> None:
        """Restore a visible cyan ring and VAD indication without touching audio."""
        try:
            for command, data in ((0x20, [20]), (0x22, [1]), (1, [0, 128, 255, 0])):
                self._dev.ctrl_transfer(0x40, 0, command, 0x1C, data, 1000)
        except Exception as exc:
            raise ReSpeakerControlError(self._format_usb_error(exc)) from exc

    def close(self) -> None:
        try:
            close = getattr(self._tuning, "close", None)
            if callable(close):
                close()
            elif self._dev is not None:
                self._usb_util.dispose_resources(self._dev)
        except Exception:
            pass

    @staticmethod
    def _load_pyusb() -> tuple[ModuleType, ModuleType]:
        try:
            return importlib.import_module("usb.core"), importlib.import_module("usb.util")
        except Exception as exc:
            raise ReSpeakerControlError(
                "缺少 pyusb，无法读取 ReSpeaker 硬件 VAD。请在 gptsovits 环境安装：pip install pyusb"
            ) from exc

    @staticmethod
    def _load_tuning_module() -> ModuleType:
        tuning_path = _find_tuning_py()
        if tuning_path is None:
            raise ReSpeakerControlError(
                "找不到 ReSpeaker tuning.py，无法读取板载硬件 VAD。"
                f"请执行：{CLONE_HINT}，然后设置 RESPEAKER_TUNING_PATH=/path/to/usb_4_mic_array"
            )

        module_name = f"_respeaker_tuning_{abs(hash(tuning_path))}"
        if module_name in sys.modules:
            return sys.modules[module_name]

        spec = importlib.util.spec_from_file_location(module_name, tuning_path)
        if spec is None or spec.loader is None:
            raise ReSpeakerControlError(f"无法加载 ReSpeaker tuning.py：{tuning_path}")

        module = importlib.util.module_from_spec(spec)
        try:
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise ReSpeakerControlError(f"加载 ReSpeaker tuning.py 失败：{exc}") from exc
        return module

    @staticmethod
    def _format_usb_error(exc: BaseException) -> str:
        message = str(exc)
        lower_message = message.lower()
        if "no backend available" in lower_message:
            return "pyusb 找不到 libusb backend，无法访问 ReSpeaker USB control。请安装系统库 libusb-1.0-0。"
        if "access" in lower_message or "permission" in lower_message or "errno 13" in lower_message:
            return (
                "当前用户没有访问 ReSpeaker USB control 的权限。请配置 udev rule 后重新插拔设备："
                f"{UDEV_RULE}"
            )
        return f"访问 ReSpeaker USB control 失败：{message}"


def _find_tuning_py() -> Path | None:
    env_path = respeaker_env_overrides()["tuning_path"]
    candidates: list[Path] = []
    if env_path:
        env_candidate = Path(env_path).expanduser()
        candidates.extend([env_candidate / "tuning.py", env_candidate])

    repo_root = Path(__file__).resolve().parents[2]
    candidates.append(repo_root / "third_party" / "respeaker_usb_4_mic_array" / "tuning.py")

    for candidate in candidates:
        if candidate.is_file() and candidate.name == "tuning.py":
            return candidate
    return None
