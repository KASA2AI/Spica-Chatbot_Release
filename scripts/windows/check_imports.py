#!/usr/bin/env python3
"""W2-a import smoke for the Windows base environment.

Checks that every package in requirements-windows-base.txt imports and prints
its version. Pure Python, runnable on Linux and Windows alike:

  - no Windows API usage (no ctypes/windll/pywin32),
  - no os.getenv / os.environ reads,
  - no spica imports or config loading (AppHost.initialize() is a W2 gate,
    not part of this script).

Gate semantics (W2-a ruling):
  - REQUIRED: every package must import; any failure exits non-zero.
  - PREFLIGHT: failures print a WARN and do not change the exit code.

Also prints onnxruntime.get_available_providers(); on the Windows base env
(CPU-only onnxruntime) expect at least CPUExecutionProvider.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import sys

# (pip distribution name, import module name, alternate distributions that
#  may provide the same module — for version lookup only)
REQUIRED = [
    # Local Config Studio sidecar (requirements-config-studio.txt).
    ("fastapi", "fastapi", ()),
    ("uvicorn", "uvicorn", ()),
    ("ruamel.yaml", "ruamel.yaml", ()),
    ("PySide6", "PySide6", ()),
    ("openai", "openai", ()),
    ("httpx", "httpx", ()),
    ("pydantic", "pydantic", ()),
    ("PyYAML", "yaml", ()),
    ("python-dotenv", "dotenv", ()),
    ("numpy", "numpy", ()),
    ("Pillow", "PIL", ()),
    ("mss", "mss", ()),
    ("rapidocr-onnxruntime", "rapidocr_onnxruntime", ()),
    # On the Linux dev machine the module is provided by onnxruntime-gpu;
    # the Windows base env installs the CPU "onnxruntime" distribution.
    ("onnxruntime", "onnxruntime", ("onnxruntime-gpu", "onnxruntime-directml")),
    ("faster-whisper", "faster_whisper", ()),
    # W3: both promoted/added as hard deps of the Windows voice loop.
    ("PyAudio", "pyaudio", ()),
    ("webrtcvad-wheels", "webrtcvad", ()),
    # Anime-watch (看动漫): requests (adapters) + yt-dlp (bilibili downloader,
    # used via subprocess but importable). External tools (qBittorrent/ffmpeg/
    # player) are checked by hand, not here -- see docs/windows_heavy_install.md.
    ("requests", "requests", ()),
    ("yt-dlp", "yt_dlp", ()),
]

PREFLIGHT: list[tuple[str, str, tuple[str, ...]]] = []


def _version(dist_name: str, module: object, alternates: tuple[str, ...]) -> str:
    for name in (dist_name, *alternates):
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return str(getattr(module, "__version__", "unknown"))


def _check(entries, *, required: bool) -> bool:
    ok = True
    for dist_name, module_name, alternates in entries:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - report any import-time failure
            if required:
                ok = False
                print(f"FAIL  {dist_name}: import {module_name} failed: {exc!r}")
            else:
                print(f"WARN  {dist_name}: import {module_name} failed: {exc!r} "
                      "(PREFLIGHT — does not block W2-a)")
            continue
        label = "OK   " if required else "OK(p)"
        print(f"{label} {dist_name} == {_version(dist_name, module, alternates)}")
    return ok


def main() -> int:
    print(f"python == {sys.version.split()[0]} ({sys.platform})")

    print("\n[REQUIRED]")
    required_ok = _check(REQUIRED, required=True)

    print("\n[PREFLIGHT]")
    _check(PREFLIGHT, required=False)

    print("\n[onnxruntime providers]")
    try:
        import onnxruntime

        print(onnxruntime.get_available_providers())
    except Exception as exc:  # noqa: BLE001
        print(f"unavailable: {exc!r}")

    if not required_ok:
        print("\nRESULT: FAIL (one or more REQUIRED imports failed)")
        return 1
    print("\nRESULT: OK (all REQUIRED imports green)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
