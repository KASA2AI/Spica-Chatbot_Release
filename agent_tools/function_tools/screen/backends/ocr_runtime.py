"""Path-B OCR dispatch seam (LOCAL_RUNTIME_PLAN §2.2 / §11.1 #5).

Before this cut, screen analysis (inspect_screen / manual attachment -- "path B")
called ``backends.rapidocr.ocr_image`` DIRECTLY, bypassing ``OCRPort``. So a
provider swap on the galgame path ("path A", which goes through ``OCRPort``)
would leave path B on the old engine -- the two paths forking (§2.2).

This module is the unifying seam: ``analyzer.py`` now calls ``run_ocr`` instead of
``ocr_image`` directly. ``run_ocr`` routes through the host-installed OCR provider
(the SAME ``OCRPort`` object path A uses) when one is installed, else falls back to
the legacy ``ocr_image`` -- byte-identical to before.

WHY an install hook, not an import: ``analyzer`` lives in ``agent_tools`` and the
provider factory + new runtime live in ``spica/local_runtime`` / ``spica/host``.
Rather than have ``analyzer`` import the spica factory (provider-coupled +
cycle-risk), the host (``spica``, which already imports ``agent_tools``) INSTALLS
the chosen provider here at startup. Process-global, set once -- consistent with
the existing process-global ``_ENGINE`` singleton in ``backends.rapidocr``.

ZERO-DIFF DEFAULT: when ``ocr.provider == "rapidocr"`` (default/fallback) the host
does NOT install a provider, so ``run_ocr`` calls the legacy ``ocr_image`` -- the
default path is unchanged down to the byte.
"""

from __future__ import annotations

from typing import Any

from agent_tools.function_tools.screen.backends.rapidocr import ocr_image

# An OCRPort-shaped object (has ``.recognize(image) -> OcrResult``) or None.
# None -> legacy default (bare ``ocr_image``), the zero-diff path.
_ACTIVE_OCR_PROVIDER: Any | None = None


def set_active_ocr_provider(provider: Any | None) -> None:
    """Install the OCR provider both paths share (called once by the host)."""
    global _ACTIVE_OCR_PROVIDER
    _ACTIVE_OCR_PROVIDER = provider


def get_active_ocr_provider() -> Any | None:
    return _ACTIVE_OCR_PROVIDER


def reset_active_ocr_provider() -> None:
    """Clear the installed provider (test isolation; restores the legacy default)."""
    global _ACTIVE_OCR_PROVIDER
    _ACTIVE_OCR_PROVIDER = None


def run_ocr(image: Any) -> dict[str, Any]:
    """Run OCR through the installed provider, falling back to the legacy engine.

    Returns the same ``{engine, raw_text, blocks, error}`` dict shape ``ocr_image``
    produces, so ``analyzer`` consumes it unchanged. When a provider is installed,
    its ``OcrResult`` is shape-mapped back to that dict (``engine`` <- the
    provider's ``name``). Best-effort: provider adapters are contractually
    non-raising (they return errors in ``OcrResult.error``)."""
    provider = _ACTIVE_OCR_PROVIDER
    if provider is None:
        return ocr_image(image)  # legacy default -- byte-identical to pre-cut behaviour
    result = provider.recognize(image)
    blocks = getattr(result, "blocks", None)
    return {
        "engine": getattr(provider, "name", "rapidocr"),
        "raw_text": str(getattr(result, "text", "") or ""),
        "blocks": list(blocks) if isinstance(blocks, list) else [],
        "error": getattr(result, "error", None),
    }
