"""OCR capability port (Phase 6).

Reuses the existing process-global RapidOCR (Phase 0 ⑤): the adapter is a thin
bridge over ``agent_tools.function_tools.screen.backends.rapidocr.ocr_image`` and
holds NO model itself -- galgame OCR and ``inspect_screen`` share one engine.

Region cropping happens BEFORE recognize() (the caller passes the already-cropped
region image). Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class OcrResult:
    text: str = ""
    blocks: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None


@runtime_checkable
class OCRPort(Protocol):
    def recognize(self, image: Any) -> OcrResult:
        """Run OCR on a PIL image or PNG bytes (already cropped to the region)."""
        ...
