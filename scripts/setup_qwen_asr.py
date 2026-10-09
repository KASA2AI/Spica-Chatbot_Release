"""Prepare the optional local ASR environment; never modify the app environment.

Run with the desktop application's Python after its matching PyTorch is installed.
The separate environment reuses that PyTorch, and owns Qwen's newer Transformers.
Cloud ASR users do not need this script. Download and config writes are opt-in.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", type=Path, default=ROOT / ".venv-qwen-asr")
    parser.add_argument("--model-dir", type=Path, default=ROOT / "models/stt/Qwen3-ASR-1.7B")
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--download", action="store_true", help="Download Qwen/Qwen3-ASR-1.7B from Hugging Face")
    parser.add_argument("--write-config", action="store_true", help="Select the installed local backend in app.yaml")
    args = parser.parse_args()
    environment = args.environment.resolve()
    if environment in {ROOT, Path(sys.prefix).resolve()}:
        parser.error("Choose a separate ASR environment, not the repository or current Python environment.")
    if environment.exists() and not (environment / "pyvenv.cfg").is_file():
        parser.error("The selected directory already exists and is not a Python virtual environment.")
    # Fail before installation when the requested device cannot use the app's
    # PyTorch. This avoids silently installing CPU inference for a GPU selection.
    try:
        import torch
    except ImportError:
        parser.error("Install PyTorch in the application environment first, then rerun this script.")
    if args.device == "cuda" and not torch.cuda.is_available():
        parser.error("CUDA PyTorch is not available. Install the matching PyTorch build or choose --device cpu.")
    if not environment.exists():
        venv.EnvBuilder(with_pip=True, system_site_packages=True).create(environment)
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    subprocess.run([str(python), "-m", "pip", "install", "-r",
                    str(ROOT / "docs/requirements/requirements-qwen-asr.txt")], check=True)
    model = args.model_dir.resolve()
    if args.download:
        subprocess.run([str(python), "-c",
            "from huggingface_hub import snapshot_download; import sys; "
            "snapshot_download('Qwen/Qwen3-ASR-1.7B', local_dir=sys.argv[1])", str(model)], check=True)
    if args.write_config:
        sys.path.insert(0, str(ROOT))
        from spica.config.manager import ConfigManager
        ConfigManager().update({"stt": {
            "backend": "qwen_asr", "model": str(model), "worker_python": str(python),
            "device": args.device, "compute_type": "float32" if args.device == "cpu" else "bfloat16",
        }})
    print(f"Qwen Python: {python}\nModel directory: {model}")
    print("Restart Spica after saving these values in Settings. Real transcription has not been tested.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
