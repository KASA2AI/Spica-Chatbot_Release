"""Manifest parse/validate + engine-cache key (LOCAL_RUNTIME_PLAN §7.2 / §11.3).

First-cut MECHANISM only (no real ONNX artifact): pins the schema and the
cache-key invalidation rule so later cuts drop entries in without re-deciding the
format. Cross-platform (§13): the cache key consumes a device-info mapping, never
assumes Linux, reads no env.
"""

import unittest

from spica.local_runtime.errors import LOCAL_RUNTIME_MANIFEST_INVALID, LocalRuntimeError
from spica.local_runtime.manifest import (
    ModelManifestEntry,
    engine_cache_key,
    parse_manifest,
)

_VALID = {
    "models": {
        "rapidocr_det": {
            "source": "rapidocr_onnxruntime/models/det.onnx",
            "onnx": "artifacts/onnx/rapidocr_det.onnx",
            "engine_cache_dir": "artifacts/trt/",
            "precision": "fp16",
            "dynamic_shapes": {"input": [[1, 3, 32, 32], [1, 3, 48, 320], [1, 3, 48, 1280]]},
            "checksum": "abc123",
            "min_cuda": "12.x",
            "min_tensorrt": "10.x",
            "gpu_arch_hint": "sm_89",
        }
    }
}

_DEVICE = {
    "os_name": "Linux",
    "machine": "x86_64",
    "cuda_ep": True,
    "tensorrt_ep": True,
}


class ManifestParseTest(unittest.TestCase):
    def test_parses_valid_entry(self):
        parsed = parse_manifest(_VALID)
        self.assertIn("rapidocr_det", parsed)
        entry = parsed["rapidocr_det"]
        self.assertIsInstance(entry, ModelManifestEntry)
        self.assertEqual(entry.precision, "fp16")
        self.assertEqual(entry.checksum, "abc123")
        self.assertEqual(entry.gpu_arch_hint, "sm_89")

    def test_missing_required_field_raises(self):
        bad = {"models": {"m": {"onnx": "x.onnx", "precision": "fp16", "checksum": "c"}}}  # no source
        with self.assertRaises(LocalRuntimeError) as ctx:
            parse_manifest(bad)
        self.assertEqual(ctx.exception.code, LOCAL_RUNTIME_MANIFEST_INVALID)

    def test_invalid_precision_raises(self):
        bad = {"models": {"m": {"source": "s", "onnx": "x", "precision": "fp8", "checksum": "c"}}}
        with self.assertRaises(LocalRuntimeError):
            parse_manifest(bad)

    def test_non_mapping_and_empty_models_raise(self):
        with self.assertRaises(LocalRuntimeError):
            parse_manifest([])
        with self.assertRaises(LocalRuntimeError):
            parse_manifest({"models": {}})


class EngineCacheKeyTest(unittest.TestCase):
    def setUp(self):
        self.entry = parse_manifest(_VALID)["rapidocr_det"]

    def test_stable_for_same_inputs(self):
        self.assertEqual(
            engine_cache_key(self.entry, _DEVICE),
            engine_cache_key(self.entry, _DEVICE),
        )
        self.assertTrue(engine_cache_key(self.entry, _DEVICE).startswith("rapidocr_det-"))

    def test_changes_when_os_or_arch_changes(self):
        base = engine_cache_key(self.entry, _DEVICE)
        win = engine_cache_key(self.entry, {**_DEVICE, "os_name": "Windows"})
        arm = engine_cache_key(self.entry, {**_DEVICE, "machine": "arm64"})
        self.assertNotEqual(base, win)
        self.assertNotEqual(base, arm)

    def test_changes_when_precision_or_checksum_changes(self):
        base = engine_cache_key(self.entry, _DEVICE)
        other_precision = engine_cache_key(
            ModelManifestEntry(**{**self.entry.to_dict(), "precision": "int8"}), _DEVICE
        )
        other_checksum = engine_cache_key(
            ModelManifestEntry(**{**self.entry.to_dict(), "checksum": "zzz"}), _DEVICE
        )
        self.assertNotEqual(base, other_precision)
        self.assertNotEqual(base, other_checksum)

    def test_changes_when_trt_or_cuda_flips(self):
        base = engine_cache_key(self.entry, _DEVICE)
        no_trt = engine_cache_key(self.entry, {**_DEVICE, "tensorrt_ep": False})
        self.assertNotEqual(base, no_trt)


if __name__ == "__main__":
    unittest.main()
