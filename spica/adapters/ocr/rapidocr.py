"""RapidOCR adapter (Phase 6) -- a THIN bridge over the existing global engine.

It calls ``agent_tools.function_tools.screen.backends.rapidocr.ocr_image``, which
owns the process-global ``_ENGINE`` singleton. This adapter holds NO model and
NEVER instantiates RapidOCR, so galgame OCR and ``inspect_screen`` share exactly
one engine (Phase 0 ⑤ hard constraint -- no second model load). N3-layer allows
``spica`` -> ``agent_tools``.

Region cropping is the caller's job (done before recognize). The cross-path
inference serialization lock is Phase 7/9 (no concurrency in the Phase 6 single
test capture). Qt-free.
"""

from __future__ import annotations

from typing import Any

from agent_tools.function_tools.screen.backends.rapidocr import ocr_image
from spica.ports.ocr import OcrResult


class RapidOcrAdapter:
    name = "rapidocr"

    def recognize(self, image: Any) -> OcrResult:
        raw = ocr_image(image)  # PIL image or PNG bytes; reuses the global _ENGINE
        return OcrResult(
            text=str(raw.get("raw_text") or ""),
            blocks=raw.get("blocks") if isinstance(raw.get("blocks"), list) else [],
            error=raw.get("error") if isinstance(raw.get("error"), dict) else None,
        )
