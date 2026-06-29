"""Model manifest parse / validate (LOCAL_RUNTIME_PLAN §7.2).

FIRST-CUT SCOPE (D4): this is the parse/validate MECHANISM only -- no real ONNX
artifact is shipped (RapidOCR bundles its own ONNX; a real export manifest lands
with the TRT-EP / GPT-SoVITS cuts). It pins the schema and the engine-cache-key
rule now so later cuts drop their entries in without re-litigating the format.

Engine-cache key (§7.2) folds os + gpu_arch + tensorrt + cuda + precision +
shape_profile + model_checksum -- any change invalidates the cached engine. The
key construction is cross-platform (§13): it consumes a device-info mapping (from
``device.probe_device``), never assumes Linux, never reads env.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from spica.local_runtime.errors import LOCAL_RUNTIME_MANIFEST_INVALID, LocalRuntimeError

_REQUIRED_FIELDS = ("source", "onnx", "precision", "checksum")
_VALID_PRECISIONS = frozenset({"fp16", "int8", "fp32"})


@dataclass(frozen=True)
class ModelManifestEntry:
    model_id: str
    source: str
    onnx: str
    precision: str
    checksum: str
    engine_cache_dir: str | None = None
    dynamic_shapes: dict[str, Any] | None = None
    min_cuda: str | None = None
    min_tensorrt: str | None = None
    gpu_arch_hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "source": self.source,
            "onnx": self.onnx,
            "precision": self.precision,
            "checksum": self.checksum,
            "engine_cache_dir": self.engine_cache_dir,
            "dynamic_shapes": self.dynamic_shapes,
            "min_cuda": self.min_cuda,
            "min_tensorrt": self.min_tensorrt,
            "gpu_arch_hint": self.gpu_arch_hint,
        }


def _require(model_id: str, entry: dict[str, Any]) -> None:
    if not isinstance(entry, dict):
        raise LocalRuntimeError(
            LOCAL_RUNTIME_MANIFEST_INVALID, f"model {model_id!r}: entry must be a mapping"
        )
    missing = [field for field in _REQUIRED_FIELDS if not entry.get(field)]
    if missing:
        raise LocalRuntimeError(
            LOCAL_RUNTIME_MANIFEST_INVALID,
            f"model {model_id!r}: missing required field(s): {', '.join(missing)}",
        )
    precision = entry.get("precision")
    if precision not in _VALID_PRECISIONS:
        raise LocalRuntimeError(
            LOCAL_RUNTIME_MANIFEST_INVALID,
            f"model {model_id!r}: precision {precision!r} not in {sorted(_VALID_PRECISIONS)}",
        )


def parse_manifest(data: Any) -> dict[str, ModelManifestEntry]:
    """Validate + parse a manifest mapping into ``{model_id: ModelManifestEntry}``.

    Raises ``LocalRuntimeError(LOCAL_RUNTIME_MANIFEST_INVALID)`` on a malformed
    document, a missing required field, or an unknown precision."""
    if not isinstance(data, dict):
        raise LocalRuntimeError(LOCAL_RUNTIME_MANIFEST_INVALID, "manifest must be a mapping")
    models = data.get("models")
    if not isinstance(models, dict) or not models:
        raise LocalRuntimeError(
            LOCAL_RUNTIME_MANIFEST_INVALID, "manifest must have a non-empty 'models' mapping"
        )
    parsed: dict[str, ModelManifestEntry] = {}
    for model_id, entry in models.items():
        _require(model_id, entry)
        parsed[model_id] = ModelManifestEntry(
            model_id=model_id,
            source=str(entry["source"]),
            onnx=str(entry["onnx"]),
            precision=str(entry["precision"]),
            checksum=str(entry["checksum"]),
            engine_cache_dir=entry.get("engine_cache_dir"),
            dynamic_shapes=entry.get("dynamic_shapes"),
            min_cuda=entry.get("min_cuda"),
            min_tensorrt=entry.get("min_tensorrt"),
            gpu_arch_hint=entry.get("gpu_arch_hint"),
        )
    return parsed


def engine_cache_key(entry: ModelManifestEntry, device_info: dict[str, Any]) -> str:
    """A stable cache key folding everything that invalidates a built engine
    (§7.2): os + gpu_arch + tensorrt + cuda + precision + shape_profile + checksum.

    Cross-platform (§13): derives os/arch from the passed ``device_info`` mapping
    (``device.probe_device().to_dict()``-shaped), assumes no platform, reads no env.
    """
    components = {
        "os": device_info.get("os_name", ""),
        "gpu_arch": device_info.get("machine", "") or entry.gpu_arch_hint or "",
        "tensorrt": device_info.get("tensorrt_ep", False),
        "cuda": device_info.get("cuda_ep", False),
        "precision": entry.precision,
        "shape_profile": entry.dynamic_shapes or {},
        "checksum": entry.checksum,
    }
    blob = json.dumps(components, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return f"{entry.model_id}-{digest}"
