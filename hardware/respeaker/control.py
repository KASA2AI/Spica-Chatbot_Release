from __future__ import annotations

import importlib
import importlib.util
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


class ReSpeakerControl:
    def __init__(self) -> None:
        usb_core, usb_util = self._load_pyusb()
        tuning_module = self._load_tuning_module()

        try:
            dev = usb_core.find(idVendor=VID, idProduct=PID)
        except Exception as exc:
            raise ReSpeakerControlError(self._format_usb_error(exc)) from exc

        if dev is None:
            raise ReSpeakerControlError(
                "未找到 ReSpeaker USB Mic Array (2886:0018)。请确认设备已连接并可被当前用户访问。"
            )

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
