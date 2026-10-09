"""Prepare optional local ASR using the platform's supported environment.

Run with the desktop application's Python after its matching PyTorch is installed.
Linux creates a separate worker environment. Windows checks and reuses the
current app environment; install it with scripts/windows/setup_environment.py.
Cloud ASR users do not need this script. Download and config writes are opt-in.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", type=Path, help="Linux-only separate worker environment")
    parser.add_argument("--model-dir", type=Path, default=ROOT / "models/stt/Qwen3-ASR-1.7B")
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--download", action="store_true", help="Download Qwen/Qwen3-ASR-1.7B from Hugging Face")
    parser.add_argument("--write-config", action="store_true", help="Select the installed local backend in app.yaml")
    args = parser.parse_args(argv)
    single_environment = sys.platform == "win32"
    if single_environment and args.environment is not None:
        parser.error("Windows uses the current app Python. Omit --environment; no second environment is needed.")
    environment = (args.environment or ROOT / ".venv-qwen-asr").resolve()
    if not single_environment and environment in {ROOT, Path(sys.prefix).resolve()}:
        parser.error("Choose a separate ASR environment, not the repository or current Python environment.")
    if not single_environment and environment.exists() and not (environment / "pyvenv.cfg").is_file():
        parser.error("The selected directory already exists and is not a Python virtual environment.")
    # Fail before installation when the requested device cannot use the app's
    # PyTorch. This avoids silently installing CPU inference for a GPU selection.
    try:
        import torch
    except ImportError:
        parser.error("Install PyTorch in the application environment first, then rerun this script.")
    if args.device == "cuda" and not torch.cuda.is_available():
        parser.error("CUDA PyTorch is not available. Install the matching PyTorch build or choose --device cpu.")
    if single_environment:
        from importlib.metadata import PackageNotFoundError, version
        for package, expected in (("qwen-asr", "0.0.6"), ("silero-vad", "6.0.0"),
                                  ("transformers", "4.57.6")):
            try:
                installed = version(package)
            except PackageNotFoundError:
                installed = "missing"
            if installed != expected:
                parser.error(f"{package}: expected {expected}, found {installed}. "
                             "Run scripts/windows/setup_environment.py --profile full --install first.")
        python = Path(sys.executable)
    else:
        if not environment.exists():
            venv.EnvBuilder(with_pip=True, system_site_packages=True).create(environment)
        python = environment / "bin/python"
        subprocess.run([str(python), "-m", "pip", "install", "-r",
                        str(ROOT / "docs/requirements/requirements-qwen-asr.txt")], check=True)
    model = args.model_dir.resolve()
    if args.download:
        subprocess.run([str(python), "-c",
            "from huggingface_hub import snapshot_download; import sys; "
            "snapshot_download('Qwen/Qwen3-ASR-1.7B', local_dir=sys.argv[1])", str(model)], check=True)
    if args.write_config:
        if not (model / "config.json").is_file():
            parser.error("The local Qwen model is missing. Supply --model-dir or explicitly use --download.")
        sys.path.insert(0, str(ROOT))
        from spica.config.manager import ConfigManager
        ConfigManager().update({"stt": {
            "backend": "qwen_asr", "model": str(model), "worker_python": None if single_environment else str(python),
            "device": args.device, "compute_type": "float32" if args.device == "cpu" else "bfloat16",
        }})
    print(f"Qwen Python: {python}\nModel directory: {model}")
    print("Restart Spica after saving these values in Settings. Real transcription has not been tested.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
