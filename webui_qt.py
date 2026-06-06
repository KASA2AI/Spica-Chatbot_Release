from __future__ import annotations

import ctypes.util
import os
import subprocess
import sys
from pathlib import Path


def _check_linux_qt_xcb_dependency() -> bool:
    if sys.platform != "linux":
        return True

    platform = os.environ.get("QT_QPA_PLATFORM", "").strip().lower()
    if platform and platform != "xcb":
        return True

    if _qt_xcb_plugin_has_cursor_dependency():
        return True

    print(
        "\nQt xcb 平台插件缺少系统库：libxcb-cursor.so.0\n"
        "Ubuntu 22.04 修复命令：\n"
        "  sudo apt update\n"
        "  sudo apt install -y libxcb-cursor0\n\n"
        "安装后重新运行：\n"
        "  /home/san/anaconda3/envs/gptsovits/bin/python webui_qt.py\n",
        file=sys.stderr,
    )
    return False


def _qt_xcb_plugin_has_cursor_dependency() -> bool:
    if not ctypes.util.find_library("xcb-cursor"):
        return False

    try:
        from PySide6.QtCore import QLibraryInfo
    except Exception:
        return True

    plugin_root = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
    xcb_plugin = plugin_root / "platforms" / "libqxcb.so"
    if not xcb_plugin.exists():
        return True

    result = subprocess.run(
        ["ldd", str(xcb_plugin)],
        check=False,
        capture_output=True,
        text=True,
    )
    output = f"{result.stdout}\n{result.stderr}"
    return "libxcb-cursor.so.0 => not found" not in output


def _configure_linux_input_method() -> None:
    if sys.platform != "linux":
        return

    qt_im_module = os.environ.get("QT_IM_MODULE", "").strip().lower()
    if qt_im_module != "fcitx":
        return

    try:
        from PySide6.QtCore import QLibraryInfo
    except Exception:
        return

    plugin_root = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
    input_contexts = plugin_root / "platforminputcontexts"
    has_fcitx_plugin = any(input_contexts.glob("*fcitx*"))
    if has_fcitx_plugin:
        return

    if os.environ.get("XMODIFIERS", "").strip().lower() == "@im=fcitx":
        os.environ["QT_IM_MODULE"] = "xim"
        print(
            "\n当前系统是 fcitx4，但 PySide6/Qt6 没有 fcitx 输入法插件；"
            "已自动改用 QT_IM_MODULE=xim 走 fcitx 的 XIM 前端。\n",
            file=sys.stderr,
        )
        return

    print(
        "\n当前 QT_IM_MODULE=fcitx，但 PySide6 的 Qt 输入法插件目录没有 fcitx 插件。\n"
        "这会导致中文输入法在 QLineEdit 里不能正常组合输入。\n"
        "可选修复：安装 Qt6 的 fcitx 前端插件，设置 QT_IM_MODULE=xim，或切换到 ibus 输入法环境后再启动。\n",
        file=sys.stderr,
    )


def _configure_linux_alsa_plugins() -> None:
    if sys.platform != "linux" or os.environ.get("ALSA_PLUGIN_DIR"):
        return

    local_plugin = Path(sys.prefix) / "lib" / "alsa-lib" / "libasound_module_pcm_pulse.so"
    if local_plugin.exists():
        return

    system_plugins = [Path("/usr/lib/alsa-lib/libasound_module_pcm_pulse.so")]
    system_plugins.extend(Path("/usr/lib").glob("*/alsa-lib/libasound_module_pcm_pulse.so"))
    for plugin in system_plugins:
        if plugin.exists():
            os.environ["ALSA_PLUGIN_DIR"] = str(plugin.parent)
            return


def main() -> int:
    if not _check_linux_qt_xcb_dependency():
        return 2
    _configure_linux_alsa_plugins()
    _configure_linux_input_method()

    from ui.qt_overlay import main as qt_main

    return qt_main()


if __name__ == "__main__":
    raise SystemExit(main())
