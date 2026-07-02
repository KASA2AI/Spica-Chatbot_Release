"""Vision local-runtime boundary (LOCAL_RUNTIME_PLAN, cut 4 -- Moondream).

The 4th and final isolation cut: the Moondream screen-vision inference
implementation moves INTO ``spica/local_runtime/vision`` and is packaged as the
``moondream_hf`` provider, so the ``transformers`` / ``torch`` VLM load path is
owned by the local-runtime layer (like OCR / TTS / RVC before it).

This cut is ARCHITECTURE-ALIGNMENT + DEPENDENCY-ISOLATION, not a slim: Moondream
has no vendored tree to prune (model code arrives via ``trust_remote_code`` from
HF, weights live in the HF cache). The ``from_pretrained`` load path is moved
VERBATIM -- ``moondream_hf`` is byte-for-byte the legacy ``MoondreamBackend``
logic, only the provider name differs. Default ``moondream_local`` is untouched
(the legacy backend stays the fallback until parity passes).
"""

from __future__ import annotations

from spica.local_runtime.vision.moondream_hf import (
    MoondreamHfBackend,
    MoondreamHfProvider,
)

__all__ = ["MoondreamHfBackend", "MoondreamHfProvider"]
