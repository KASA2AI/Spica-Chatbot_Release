"""Device / runtime capability probing (LOCAL_RUNTIME_PLAN §3.3 + §13).

Answers "what can this machine run?" -- available ONNX Runtime execution
providers, CUDA presence, TensorRT availability, OS/arch -- for provider
selection, the ``doctor`` CLI, and engine-cache keys.

HARD CONSTRAINT (§3.3 / CLAUDE.md #4): NO ``os.getenv`` / ``os.environ`` here.
Detection goes through ``import`` probes, ``subprocess``, and ``platform`` only.
CROSS-PLATFORM (§13): no Linux assumptions baked in -- ``platform`` branches,
``pathlib`` paths, ``subprocess`` with graceful failure (this build script
discipline is what lets the Windows phase reuse, not rewrite, these probes).
"""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass, field


def available_onnx_providers() -> list[str]:
    """ONNX Runtime execution providers available in THIS process, e.g.
    ``["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"]``.

    Returns ``[]`` when onnxruntime is not importable (no-ORT env) -- callers
    treat empty as "CPU-only / unknown" and degrade, never crash."""
    try:
        import onnxruntime  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 -- absent/broken ORT -> no providers, silent
        return []
    try:
        return list(onnxruntime.get_available_providers())
    except Exception:  # noqa: BLE001
        return []


def has_cuda_ep() -> bool:
    """ORT can place ops on CUDA (the rapidocr_ort GPU path)."""
    return "CUDAExecutionProvider" in available_onnx_providers()


def has_tensorrt_ep() -> bool:
    """ORT exposes the TensorRT execution provider (the rapidocr_trt_ep step-2 path)."""
    return "TensorrtExecutionProvider" in available_onnx_providers()


def tensorrt_importable() -> bool:
    """The standalone ``tensorrt`` python package is importable (build path)."""
    try:
        import tensorrt  # type: ignore[import-not-found]  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def nvidia_smi_present() -> bool:
    """A usable ``nvidia-smi`` exists -> an NVIDIA driver is installed.

    Pure probe via ``subprocess``; any failure (missing binary, non-zero exit,
    timeout, OSError) reads as "no GPU driver" without raising."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


@dataclass(frozen=True)
class DeviceInfo:
    os_name: str
    machine: str  # cpu arch, e.g. x86_64 / AMD64 / arm64
    onnx_providers: list[str] = field(default_factory=list)
    cuda_ep: bool = False
    tensorrt_ep: bool = False
    tensorrt_importable: bool = False
    nvidia_driver: bool = False

    def to_dict(self) -> dict:
        return {
            "os_name": self.os_name,
            "machine": self.machine,
            "onnx_providers": list(self.onnx_providers),
            "cuda_ep": self.cuda_ep,
            "tensorrt_ep": self.tensorrt_ep,
            "tensorrt_importable": self.tensorrt_importable,
            "nvidia_driver": self.nvidia_driver,
        }


def probe_device() -> DeviceInfo:
    """One snapshot of runtime capabilities. Env-free (§3.3)."""
    providers = available_onnx_providers()
    return DeviceInfo(
        os_name=platform.system(),
        machine=platform.machine(),
        onnx_providers=providers,
        cuda_ep="CUDAExecutionProvider" in providers,
        tensorrt_ep="TensorrtExecutionProvider" in providers,
        tensorrt_importable=tensorrt_importable(),
        nvidia_driver=nvidia_smi_present(),
    )
