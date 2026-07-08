"""Local-runtime environment self-check (LOCAL_RUNTIME_PLAN §4 / §13).

Prints what THIS machine can run -- ONNX Runtime execution providers, CUDA EP,
TensorRT EP/import, NVIDIA driver, OS/arch -- so a user can diagnose before a
provider switch. Run:  python -m scripts.local_runtime.doctor

ENV NAMES (§3.3): this CLI lives under ``scripts/`` (outside ``spica/``), so it is
permitted to read environment variables -- but currently reads NONE. Any future
env knob (e.g. an override for the engine-cache dir) MUST be declared in this
block so the local-runtime env surface stays documented in one place. Production
runtime code under ``spica/local_runtime`` never reads env (that wall is enforced
by ``test_no_getenv``); only scripts like this may.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from spica.local_runtime.device import probe_device  # noqa: E402


def main() -> int:
    info = probe_device()
    print(json.dumps(info.to_dict(), indent=2, ensure_ascii=False))
    if not info.onnx_providers:
        print("\n[warn] onnxruntime not importable -- OCR runs CPU-only / may be unavailable.")
    elif not info.cuda_ep:
        print("\n[note] no CUDAExecutionProvider -- OCR will run on CPU.")
    if not info.tensorrt_ep:
        print("[note] no TensorrtExecutionProvider -- rapidocr_trt_ep (step 2) unavailable here.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
