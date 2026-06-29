"""RapidOCR TensorRT-EP adapter (LOCAL_RUNTIME_PLAN cut 2) -- experimental.

Wears the EXISTING ``OCRPort`` (no second port, §3.1). LAZY: construction is cheap
(stores config; builds NO engine) so the factory + CI can create it without a GPU /
TRT / engine build. The runtime -- and thus the engine build + warmup + any
TRT->CUDA fallback -- is created on first ``recognize`` or an explicit ``warmup``
(used by the parity / benchmark scripts and, when the default is eventually
switched, by host startup).

Registered as an EXPERIMENTAL provider (``name = "rapidocr_trt_ep"``). NOT the
production default this cut: the default stays ``rapidocr`` until a real-machine
parity + benchmark report clears the gate (§6.1). Best-effort -- a total build
failure degrades to an empty result + error, never raises into a turn.
"""

from __future__ import annotations

from typing import Any

from spica.local_runtime.errors import LOCAL_RUNTIME_INFERENCE_FAILED
from spica.ports.ocr import OcrResult


class RapidOcrTrtEpAdapter:
    name = "rapidocr_trt_ep"

    def __init__(
        self,
        *,
        fp16: bool = False,
        engine_cache_dir: str = "artifacts/trt",
        timing_cache: bool = True,
        profiles: dict[str, str] | None = None,
        device_id: int = 0,
        runtime: Any | None = None,
    ) -> None:
        self._cfg = dict(
            fp16=fp16,
            engine_cache_dir=str(engine_cache_dir),
            timing_cache=timing_cache,
            profiles=profiles,
            device_id=device_id,
        )
        self._runtime = runtime  # lazily built unless injected (tests)

    def _ensure_runtime(self) -> Any:
        if self._runtime is None:
            from spica.local_runtime.ocr.rapidocr_trt_runtime import RapidOcrTrtEpRuntime

            self._runtime = RapidOcrTrtEpRuntime(**self._cfg)
        return self._runtime

    def recognize(self, image: Any) -> OcrResult:
        try:
            runtime = self._ensure_runtime()
        except Exception as exc:  # noqa: BLE001 -- best-effort: never raise into a turn
            return OcrResult(
                text="",
                blocks=[],
                error={
                    "stage": "ocr",
                    "code": LOCAL_RUNTIME_INFERENCE_FAILED,
                    "message": f"rapidocr_trt_ep build failed: {type(exc).__name__}: {exc}",
                    "recoverable": True,
                },
            )
        raw = runtime.recognize(image)
        return OcrResult(
            text=str(raw.get("raw_text") or ""),
            blocks=raw.get("blocks") if isinstance(raw.get("blocks"), list) else [],
            error=raw.get("error") if isinstance(raw.get("error"), dict) else None,
        )

    def warmup(self) -> str:
        """Build + warm the engine now (surfaces TRT build / fallback at a chosen
        time -- e.g. a script or, later, host startup). Returns the used providers
        (``"trt"`` | ``"cuda"``)."""
        return self._ensure_runtime().used_providers
