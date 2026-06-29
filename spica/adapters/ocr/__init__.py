"""OCR adapters (Phase 6 + LOCAL_RUNTIME_PLAN cut 1)."""

from spica.adapters.ocr.rapidocr import RapidOcrAdapter
from spica.adapters.ocr.rapidocr_ort import RapidOcrOrtAdapter

__all__ = ["RapidOcrAdapter", "RapidOcrOrtAdapter"]
