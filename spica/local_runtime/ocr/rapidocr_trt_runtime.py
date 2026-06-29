"""RapidOCR via ORT TensorRT EP (LOCAL_RUNTIME_PLAN cut 2, D1/D4).

Adds the TensorRT execution provider to RapidOCR's det/cls/rec ONNX sessions
WITHOUT rewriting OCR inference: ``rapidocr_onnxruntime``'s ``OrtInferSession``
only knows CPU/CUDA/DirectML and ignores unknown config keys, so the only way to
turn on TRT is to swap that session class. We do it with a SCOPED, reversible
monkeypatch active only during ``RapidOCR(...)`` construction -- the whole RapidOCR
pipeline + pre/post is reused unchanged; we changed only the provider list.

D4: fp32 is the cut-2 default (verify the integration -- engine builds, cache hits,
fallback works, speedup, parity -- with one variable; fp16 is a configurable step 2).

Hard constraint: TRT init/build failure MUST degrade to CUDA, never crash (handled
by ``build_engine_with_fallback`` + a warmup that surfaces the lazy engine build at
construction time, not mid-turn). Env-free (§3.3): the engine-cache dir is an
injected absolute path; nothing here reads os.environ.

ALL ``rapidocr_onnxruntime`` imports are deferred into functions so importing THIS
module stays light (CI can import it without pulling the real OCR stack / building
anything).
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from threading import RLock
from typing import Any

from spica.local_runtime.errors import LOCAL_RUNTIME_INFERENCE_FAILED, LocalRuntimeError
from spica.local_runtime.ocr.trt_options import (
    build_engine_with_fallback,
    build_ep_list,
    build_trt_provider_options,
    default_cpu_options,
    default_cuda_options,
)

_LOGGER = logging.getLogger(__name__)


def make_trt_session_class(
    trt_options: dict[str, Any],
    cuda_options: dict[str, Any],
    cpu_options: dict[str, Any],
) -> type:
    """A ``OrtInferSession`` subclass that builds its session with TRT -> CUDA -> CPU.

    Only ``_get_ep_list`` is overridden (the one method that picks providers); every
    other behaviour -- preprocessing, ``__call__``, metadata, char list -- is
    inherited, so the recognized output is RapidOCR's, just on a different EP.
    rapidocr is imported here (lazily) so this module stays light at import time."""
    from rapidocr_onnxruntime.utils.infer_engine import OrtInferSession

    ep_list = build_ep_list(trt_options, cuda_options=cuda_options, cpu_options=cpu_options)

    class _TrtOrtInferSession(OrtInferSession):
        def _get_ep_list(self):  # type: ignore[override]
            # Keep the parent's verify-providers bookkeeping consistent.
            self.use_cuda = self._check_cuda()
            self.use_directml = False
            return [(name, dict(opts)) for name, opts in ep_list]

    return _TrtOrtInferSession


# The three RapidOCR submodules that bound ``OrtInferSession`` at import time. The
# scoped patch swaps the name in each; the reverse-drift CI test pins their
# existence so a rapidocr upgrade that renames them fails CI loudly, not silently.
_PATCH_TARGETS = (
    ("rapidocr_onnxruntime.ch_ppocr_det.text_detect", "OrtInferSession"),
    ("rapidocr_onnxruntime.ch_ppocr_cls.text_cls", "OrtInferSession"),
    ("rapidocr_onnxruntime.ch_ppocr_rec.text_recognize", "OrtInferSession"),
)


def detect_active_provider(engine: Any) -> str:
    """The EP the built RapidOCR det session ACTUALLY runs on (``"trt"`` | ``"cuda"``
    | ``"cpu"`` | the raw name | ``"unknown"``).

    Critical for honest reporting: when the TensorRT libraries (libnvinfer) are
    absent, ORT does NOT raise -- it logs an EP error and silently falls back to
    CUDA while ``InferenceSession`` construction still SUCCEEDS. So "the TRT factory
    didn't throw" does NOT mean TRT is running. We read the session's real provider
    list instead of trusting the build path."""
    try:
        providers = list(engine.text_det.infer.session.get_providers())
    except Exception:  # noqa: BLE001
        return "unknown"
    if "TensorrtExecutionProvider" in providers:
        return "trt"
    if "CUDAExecutionProvider" in providers:
        return "cuda"
    if "CPUExecutionProvider" in providers:
        return "cpu"
    return providers[0] if providers else "unknown"


@contextlib.contextmanager
def _patched_session_class(session_cls: type):
    """Temporarily point all three RapidOCR stage modules at ``session_cls``.

    Active only while RapidOCR builds its sessions (in its ``__init__``); restored
    immediately after, so the patch never leaks to other code / the default
    rapidocr provider."""
    import importlib

    modules = [importlib.import_module(name) for name, _ in _PATCH_TARGETS]
    originals = [(mod, getattr(mod, attr)) for mod, (_, attr) in zip(modules, _PATCH_TARGETS)]
    try:
        for mod, (_, attr) in zip(modules, _PATCH_TARGETS):
            setattr(mod, attr, session_cls)
        yield
    finally:
        for mod, original in originals:
            setattr(mod, "OrtInferSession", original)


class RapidOcrTrtEpRuntime:
    """RapidOCR engine on the ORT TensorRT EP, owning its own engine + inference lock.

    Built eagerly on construction (so the build + warmup + any TRT->CUDA fallback
    happen once, when the runtime is created -- the adapter defers creating the
    runtime until first use / an explicit warmup, keeping adapter construction cheap
    and CI-safe). ``used_providers`` records whether TRT or the CUDA fallback won."""

    def __init__(
        self,
        *,
        fp16: bool = False,
        engine_cache_dir: str = "artifacts/trt",
        timing_cache: bool = True,
        profiles: dict[str, str] | None = None,
        device_id: int = 0,
    ) -> None:
        self._lock = RLock()
        self._device_id = device_id
        self._fp16 = fp16
        cache_dir = Path(engine_cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)  # pathlib, cross-platform (§13)
        self._trt_options = build_trt_provider_options(
            fp16=fp16,
            engine_cache_dir=str(cache_dir),
            timing_cache=timing_cache,
            profiles=profiles,
            device_id=device_id,
        )
        result = build_engine_with_fallback(
            primary_factory=lambda: self._build_rapidocr(use_trt=True),
            fallback_factory=lambda: self._build_rapidocr(use_trt=False),
            warmup=self._warmup,
            on_fallback=lambda exc: _LOGGER.warning(
                "TRT EP init/build failed (%s: %s); falling back to CUDA EP",
                type(exc).__name__,
                exc,
            ),
        )
        self._engine = result.engine
        # build_path = which factory won (my explicit TRT->CUDA fallback). used_providers
        # = the ACTUAL EP the session runs on (ORT may have fallen back to CUDA INSIDE a
        # "successful" TRT build when libnvinfer is missing -- detect, don't assume).
        self.build_path = result.used
        self.used_providers = detect_active_provider(self._engine)
        if self.used_providers != "trt":
            _LOGGER.warning(
                "rapidocr_trt_ep is NOT running on TensorRT (actual=%s, build_path=%s) -- "
                "check TensorRT libs (libnvinfer) are installed; OCR proceeds on the fallback EP",
                self.used_providers,
                self.build_path,
            )
        _LOGGER.info(
            "rapidocr_trt_ep engine ready (actual_provider=%s, fp16=%s)", self.used_providers, fp16
        )

    def _build_rapidocr(self, *, use_trt: bool) -> Any:
        from rapidocr_onnxruntime import RapidOCR

        # use_cuda=True so the subclass's CUDA fallback opts are configured and the
        # plain-CUDA fallback path uses GPU too (RapidOCR auto-degrades to CPU if no GPU).
        kwargs = dict(det_use_cuda=True, cls_use_cuda=True, rec_use_cuda=True)
        if not use_trt:
            return RapidOCR(**kwargs)
        session_cls = make_trt_session_class(
            self._trt_options,
            default_cuda_options(self._device_id),
            default_cpu_options(),
        )
        with _patched_session_class(session_cls):
            return RapidOCR(**kwargs)

    def _warmup(self, engine: Any) -> None:
        """Force the lazy TRT engine build NOW (it happens at first inference, not
        construction) so a build failure surfaces here and triggers the CUDA
        fallback -- not mid-turn. Uses a synthetic text image so det+cls+rec all run.
        ``recognize_with_engine`` swallows errors into a payload, so re-raise on
        error to drive the fallback."""
        from PIL import Image, ImageDraw

        from agent_tools.function_tools.screen.backends.rapidocr import recognize_with_engine

        img = Image.new("RGB", (320, 64), "white")
        ImageDraw.Draw(img).text((8, 20), "Aa1あい", fill="black")
        result = recognize_with_engine(lambda: engine, img, self._lock)
        error = result.get("error")
        if error:
            raise LocalRuntimeError(LOCAL_RUNTIME_INFERENCE_FAILED, f"warmup failed: {error}")

    def recognize(self, image: Any) -> dict[str, Any]:
        """OCR on the owned engine, reusing the shared prepare/parse/error body."""
        from agent_tools.function_tools.screen.backends.rapidocr import recognize_with_engine

        return recognize_with_engine(lambda: self._engine, image, self._lock)
