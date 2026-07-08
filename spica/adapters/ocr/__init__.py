"""OCR adapters (Phase 6 + LOCAL_RUNTIME_PLAN cut 1/2)."""

from spica.adapters.ocr.rapidocr import RapidOcrAdapter
from spica.adapters.ocr.rapidocr_ort import RapidOcrOrtAdapter
from spica.adapters.ocr.rapidocr_trt_ep import RapidOcrTrtEpAdapter

__all__ = ["RapidOcrAdapter", "RapidOcrOrtAdapter", "RapidOcrTrtEpAdapter"]
