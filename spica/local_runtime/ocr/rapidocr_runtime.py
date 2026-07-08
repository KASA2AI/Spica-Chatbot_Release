"""RapidOCR local-runtime boundary (LOCAL_RUNTIME_PLAN §11, first cut -- D1 (ii)).

FIRST-CUT SCOPE (decided D1 = thin boundary wrapper): RapidOCR is already
ONNX-Runtime-backed, so this cut does NOT re-own the ORT session yet. It moves
the OCR call site INTO ``spica/local_runtime`` and establishes the provider seam,
while delegating inference to the shared process-global engine
(``agent_tools...backends.rapidocr.ocr_image`` -- the same ``_ENGINE`` + the
cross-path ``_INFER_LOCK``). So ``rapidocr_ort`` output is byte-identical to the
``rapidocr`` provider (parity ~0 by construction -- that IS the point: it proves
the boundary extraction is faithful).

STEP 2 (NOT this cut): ``recognize`` here will build and own its own ORT session
(explicit det/cls/rec sessions + execution-provider order from ``device.py``),
and ``rapidocr_trt_ep`` attaches ORT's TensorRT EP with engine/timing cache. That
swap happens INSIDE this method -- callers (the adapter, both OCR paths) and the
parity harness never change.

``spica -> agent_tools`` is the allowed layer direction (RapidOcrAdapter already
relies on it); this module reuses the same shared engine to guarantee one model
load (CLAUDE.md #1.8 / galgame Phase 0 ⑤).
"""

from __future__ import annotations

from typing import Any

from agent_tools.function_tools.screen.backends.rapidocr import ocr_image


class RapidOcrOrtRuntime:
    """Spica-owned RapidOCR runtime boundary.

    ``providers`` reserves the ONNX Runtime execution-provider order for step 2
    (e.g. ``["CUDAExecutionProvider", "CPUExecutionProvider"]`` or, later,
    ``TensorrtExecutionProvider`` first). It is unused in the first cut, where
    inference delegates to the shared engine for byte-identical behaviour."""

    def __init__(self, *, providers: list[str] | None = None) -> None:
        self._providers = list(providers) if providers else None

    def recognize(self, image: Any) -> dict[str, Any]:
        """Run OCR -> the same ``{engine, raw_text, blocks, error}`` dict shape the
        backend produces. Best-effort: ``ocr_image`` never raises (it returns an
        error payload), so this never breaks a turn."""
        return ocr_image(image)
