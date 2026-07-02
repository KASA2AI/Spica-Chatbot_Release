"""RapidOCR ONNX-Runtime adapter (LOCAL_RUNTIME_PLAN §5 / §11).

Wears the EXISTING ``spica/ports/OCRPort`` (NO second port layer, §3.1). A thin
bridge over ``spica.local_runtime.ocr.RapidOcrOrtRuntime``: shape-maps the
runtime's dict result into ``OcrResult`` exactly as ``RapidOcrAdapter`` does, so
both providers are drop-in interchangeable behind the ``build_ocr_adapter``
factory.

Registered as provider ``rapidocr_ort``. Runtime Cutover Rehearsal step 3 made it
the repo production OCR default through ``data/config/app.yaml`` while the schema
built-in fallback remains ``rapidocr``. This is a provider-seam / Path A+B default
cutover rehearsal; it does NOT by itself mean OCR runtime dependency reduction is
fully complete. Best-effort -- never raises into a turn.
"""

from __future__ import annotations

from typing import Any

from spica.local_runtime.ocr.rapidocr_runtime import RapidOcrOrtRuntime
from spica.ports.ocr import OcrResult


class RapidOcrOrtAdapter:
    name = "rapidocr_ort"

    def __init__(self, runtime: Any | None = None) -> None:
        self._runtime = runtime or RapidOcrOrtRuntime()

    def recognize(self, image: Any) -> OcrResult:
        raw = self._runtime.recognize(image)
        return OcrResult(
            text=str(raw.get("raw_text") or ""),
            blocks=raw.get("blocks") if isinstance(raw.get("blocks"), list) else [],
            error=raw.get("error") if isinstance(raw.get("error"), dict) else None,
        )
