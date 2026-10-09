"""Install or check Spica in the current Windows Python 3.11 environment.

No environment is created. Without --install or --check, print the install plan.
No models, services, drivers, startup entries, configuration or hardware are changed.
"""
from __future__ import annotations

import argparse
from importlib import import_module, metadata
from pathlib import Path
import shutil
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def install_commands(profile: str) -> list[list[str]]:
    pip = [sys.executable, "-m", "pip"]
    requirements = ROOT / "docs/requirements"
    base = str(requirements / "requirements-windows-base.txt")
    if profile == "base":
        return [pip + ["install", "-r", base]]
    constraints = str(requirements / "constraints-windows-app.txt")
    return [
        pip + ["install", "torch==2.6.0", "torchaudio==2.6.0", "torchvision==0.21.0",
               "--index-url", "https://download.pytorch.org/whl/cu124"],
        pip + ["install", "-c", constraints, "-r", base,
               "-r", str(requirements / "requirements-windows-heavy.txt"),
               "-r", str(requirements / "requirements-windows-app.txt"),
               "-r", str(requirements / "requirements-qwen-asr.txt")],
        # This pinned runtime supports NumPy 1.26; its newer metadata requests
        # NumPy 2 and a different ONNX distribution. Keep the tested stack.
        pip + ["install", "--no-deps", "audio-separator==0.44.2"],
        # RapidOCR and Silero request the CPU distribution transitively. CPU,
        # DirectML and GPU builds share files: remove alternatives, then repair
        # the GPU build LAST so no provider DLLs/modules are left overwritten.
        pip + ["uninstall", "-y", "onnxruntime", "onnxruntime-directml"],
        pip + ["install", "--force-reinstall", "--no-deps", "onnxruntime-gpu==1.26.0"],
    ]


def check_environment(profile: str) -> bool:
    sys.path.insert(0, str(ROOT))
    from scripts.windows.check_imports import main as check_base
    ok = check_base() == 0
    if profile == "base":
        return ok
    for name, expected in {
        "numpy": "1.26.4", "torch": "2.6.0", "torchaudio": "2.6.0", "torchvision": "0.21.0",
        "transformers": "4.57.6", "tokenizers": "0.22.2", "accelerate": "1.12.0",
        "qwen-asr": "0.0.6", "silero-vad": "6.0.0", "onnxruntime-gpu": "1.26.0",
    }.items():
        try:
            actual = metadata.version(name)
            if actual.split("+")[0] != expected:
                raise ValueError(f"expected {expected}, found {actual}")
            print(f"OK    {name} == {actual}")
        except (metadata.PackageNotFoundError, ValueError) as error:
            print(f"FAIL  {name}: {error}")
            ok = False
    for name in ("onnxruntime", "onnxruntime-directml"):
        try:
            metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
        print(f"FAIL  remove conflicting ONNX distribution: {name}")
        ok = False
    for name in ("qwen_asr", "silero_vad", "soundfile", "librosa", "pyopenjtalk", "faiss", "pyncm"):
        try:
            import_module(name)
            print(f"OK    import {name}")
        except Exception as error:
            print(f"FAIL  import {name}: {type(error).__name__}: {error}")
            ok = False
    try:
        import torch
        if torch.version.cuda != "12.4" or not torch.cuda.is_available():
            raise RuntimeError("CUDA 12.4 PyTorch and an available NVIDIA GPU are required for this full profile")
        print(f"OK    GPU: {torch.cuda.get_device_name(0)}")
    except Exception as error:
        print(f"FAIL  GPU: {error}")
        ok = False
    for name in ("ffmpeg", "ffprobe"):
        if not shutil.which(name):
            print(f"FAIL  {name} is not on PATH (needed for audio/song/video)")
            ok = False
    print("This check imports libraries only; it does not load models or test synthesis.")
    return ok


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("base", "full"), default="base")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--install", action="store_true", help="Modify the current environment; close Spica first")
    action.add_argument("--check", action="store_true", help="Read-only import/version check")
    args = parser.parse_args(argv)
    if sys.platform != "win32" or sys.version_info[:2] != (3, 11) or struct.calcsize("P") != 8:
        parser.error("Use the application's 64-bit Windows Python 3.11. Linux uses the documented Linux setup.")
    print(f"Python: {sys.executable}\nEnvironment: {sys.prefix}\nProfile: {args.profile}")
    if args.check:
        return 0 if check_environment(args.profile) else 1
    if args.install and sys.prefix == sys.base_prefix and not (Path(sys.prefix) / "conda-meta").is_dir():
        parser.error("Activate an existing Conda/venv environment; do not install into the system Python.")
    if args.install and args.profile == "base":
        try:
            metadata.version("onnxruntime-gpu")
        except metadata.PackageNotFoundError:
            pass
        else:
            parser.error("This environment already has GPU ONNX Runtime. Use --profile full to preserve GPU providers.")
    commands = install_commands(args.profile)
    for command in commands:
        print(subprocess.list2cmdline(command), flush=True)
        if args.install:
            subprocess.run(command, check=True)
    if args.install:
        # A fresh process is essential after replacing shared provider modules.
        return subprocess.run([sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
                               "--profile", args.profile, "--check"], check=False).returncode
    print("Plan only. Pass --install to apply to this environment, or --check to inspect it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
