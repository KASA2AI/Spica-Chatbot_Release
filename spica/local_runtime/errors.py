"""Local-runtime error codes (LOCAL_RUNTIME_PLAN §8.3).

One English code vocabulary for the whole layer. Adapters surface these through
the existing ``OCRPort`` / ``ToolError`` envelopes (each cut confirms its mapping)
-- the codes here are the internal, stable identifiers, not user-facing strings.
"""

from __future__ import annotations

# Stable error-code constants (§8.3).
LOCAL_RUNTIME_MODEL_NOT_FOUND = "LOCAL_RUNTIME_MODEL_NOT_FOUND"
LOCAL_RUNTIME_ONNX_MISSING = "LOCAL_RUNTIME_ONNX_MISSING"
LOCAL_RUNTIME_ENGINE_BUILD_FAILED = "LOCAL_RUNTIME_ENGINE_BUILD_FAILED"
LOCAL_RUNTIME_DEVICE_UNSUPPORTED = "LOCAL_RUNTIME_DEVICE_UNSUPPORTED"  # no CUDA / TRT / driver mismatch
LOCAL_RUNTIME_INFERENCE_FAILED = "LOCAL_RUNTIME_INFERENCE_FAILED"
LOCAL_RUNTIME_PARITY_FAILED = "LOCAL_RUNTIME_PARITY_FAILED"
LOCAL_RUNTIME_MANIFEST_INVALID = "LOCAL_RUNTIME_MANIFEST_INVALID"

_KNOWN_CODES = frozenset(
    {
        LOCAL_RUNTIME_MODEL_NOT_FOUND,
        LOCAL_RUNTIME_ONNX_MISSING,
        LOCAL_RUNTIME_ENGINE_BUILD_FAILED,
        LOCAL_RUNTIME_DEVICE_UNSUPPORTED,
        LOCAL_RUNTIME_INFERENCE_FAILED,
        LOCAL_RUNTIME_PARITY_FAILED,
        LOCAL_RUNTIME_MANIFEST_INVALID,
    }
)


class LocalRuntimeError(Exception):
    """A local-runtime failure carrying a stable ``code`` from §8.3.

    Best-effort inference paths (OCR/TTS adapters) should NOT raise this into a
    turn -- they catch and return the error inside their port envelope. It is
    raised by setup/parse/build helpers (manifest parse, engine build) where a
    hard failure is the correct signal.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")
