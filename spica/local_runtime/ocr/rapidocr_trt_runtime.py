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
import ctypes
import logging
import os
from pathlib import Path
from threading import RLock
from typing import Any

from spica.local_runtime.errors import LOCAL_RUNTIME_INFERENCE_FAILED, LocalRuntimeError
from spica.local_runtime.ocr.trt_options import (
    build_engine_with_fallback,
    build_trt_provider_options,
    classify_load_status,
    default_cpu_options,
    default_cuda_options,
    ep_list_for_stage,
)

_LOGGER = logging.getLogger(__name__)
_LIBS_PRELOADED = False
# W4-b Windows keep-alives: os.add_dll_directory handles AND ctypes.WinDLL objects
# must stay referenced for the process lifetime -- a dropped handle un-registers
# the directory; a dropped DLL object may unmap the library.
_WIN_DLL_DIR_HANDLES: list[Any] = []
_WIN_LOADED_DLLS: list[Any] = []
# The Windows names of the TRT libs the Linux route loads as libnvinfer*.so.10
# (nvinfer_10.dll confirmed on the real machine -- docs/windows_w4_probe.md §4.D).
_WIN_TRT_DLLS = ("nvinfer_10.dll", "nvinfer_plugin_10.dll", "nvonnxparser_10.dll")


def _ctypes_load_all(so_paths) -> int:
    loaded = 0
    for path in so_paths:
        try:
            ctypes.CDLL(str(path), mode=ctypes.RTLD_GLOBAL)
            loaded += 1
        except OSError:
            continue  # a lib that won't load is skipped; the rest still help
    return loaded


def preload_inference_libs() -> None:
    """Pull CUDA + TensorRT shared libs into the process GLOBAL symbol table so ORT's
    CUDA and TensorRT execution providers resolve libnvinfer / libcudnn / libcublas
    WITHOUT the user setting LD_LIBRARY_PATH -- the self-contained / distributable
    path (D1). External LD_LIBRARY_PATH stays only as a documented fallback.

    Pure in-process ctypes preload -- NO env reads (CLAUDE.md #4 / §3.3). BEST-EFFORT:
    a missing package / a CDLL failure is skipped silently (NOT fatal) -- the
    post-construction ``classify_load_status`` check reports the resulting fallback,
    so we never silently pretend TRT loaded. Runs once per process. Mirrors the
    backend's CUDA preload, extended with the TensorRT libs."""
    global _LIBS_PRELOADED
    if _LIBS_PRELOADED:
        return
    _LIBS_PRELOADED = True  # mark first: a partial/failed scan must not retry every build
    if os.name == "nt":
        # W4-b: Windows resolves DLLs by LoadLibrary search, not global symbols --
        # its own helper (gate-1 validated ordering: cuDNN before nvinfer). The
        # Linux route below is untouched (zero behavior drift).
        _preload_inference_libs_windows()
        return
    total = 0
    try:  # CUDA libs from the pip nvidia-* namespace packages (cudnn/cublas/cuda_runtime)
        import nvidia

        for base in (Path(p) for p in (getattr(nvidia, "__path__", None) or [])):
            try:
                total += _ctypes_load_all(sorted(base.glob("*/lib/*.so*")))
            except OSError:
                continue
    except Exception:  # noqa: BLE001 -- no nvidia packages -> CUDA EP unavailable, reported later
        pass
    try:  # TensorRT libs from the pip tensorrt_libs package; load core libnvinfer first
        import tensorrt_libs

        tdir = Path(tensorrt_libs.__file__).parent
        for soname in ("libnvinfer.so.10", "libnvinfer_plugin.so.10", "libnvonnxparser.so.10"):
            total += _ctypes_load_all([tdir / soname])
    except Exception:  # noqa: BLE001 -- no tensorrt_libs -> TRT EP unavailable, reported later
        pass
    if total:
        _LOGGER.info("preloaded %d CUDA/TensorRT shared libs for rapidocr_trt_ep", total)


def _win_spec_dirs(package: str) -> list[Path]:
    """A package's on-disk dirs via ``find_spec`` WITHOUT importing it (never
    ``import torch`` just to fix a DLL search path -- W4-b ruling). Handles
    namespace packages (nvidia) via ``submodule_search_locations``."""
    import importlib.util

    try:
        spec = importlib.util.find_spec(package)
    except Exception:  # noqa: BLE001 -- a broken package must not kill preload
        return []
    if spec is None:
        return []
    if spec.submodule_search_locations:
        return [Path(p) for p in spec.submodule_search_locations]
    if spec.origin:
        return [Path(spec.origin).parent]
    return []


def _win_candidate_cuda_dirs() -> list[Path]:
    """Dirs that may carry cuDNN 9 / CUDA runtime DLLs on Windows, most likely
    first: pip nvidia-* wheels ship them under ``*/bin``; the torch wheel bundles
    them in ``torch/lib``; ctranslate2 bundles cudnn64_9.dll next to itself
    (all three verified in the W4-a probe, docs/windows_w4_probe.md)."""
    dirs: list[Path] = []
    for base in _win_spec_dirs("nvidia"):
        try:
            dirs.extend(sorted(d for d in base.glob("*/bin") if d.is_dir()))
        except OSError:
            continue
    for base in _win_spec_dirs("torch"):
        lib = base / "lib"
        if lib.is_dir():
            dirs.append(lib)
    dirs.extend(d for d in _win_spec_dirs("ctranslate2") if d.is_dir())
    return dirs


def _win_register_and_load(directories: list[Path], dll_names: tuple[str, ...]) -> int:
    """Register every dir on the process DLL search path, then force-load each
    named DLL from the first dir that carries it. Handles + DLL objects go into
    the module-level keep-alive lists. Best-effort throughout."""
    for directory in directories:
        try:
            _WIN_DLL_DIR_HANDLES.append(os.add_dll_directory(str(directory)))
        except OSError:
            continue
    loaded = 0
    for name in dll_names:
        for directory in directories:
            path = directory / name
            if not path.is_file():
                continue
            try:
                _WIN_LOADED_DLLS.append(ctypes.WinDLL(str(path)))
                loaded += 1
                break  # first resident copy of this DLL wins
            except OSError:
                continue  # a copy that won't load is skipped; try the next dir
    return loaded


def _preload_inference_libs_windows() -> None:
    """Windows variant (W4-b, gate-1 validated): ORT's CUDA/TRT EPs resolve
    ``cudnn64_9.dll`` / ``nvinfer_10.dll`` via the standard LoadLibrary search,
    which does NOT include wheel dirs -- without help the EPs are listed but fail
    init and silently fall back (W4-a §4.D/§4.E). ORDER MATTERS: cuDNN first so
    nvinfer's own dependency chain resolves. Env-free, best-effort, no torch
    import; mirrors the backend's Windows CUDA preload, extended with TRT."""
    total = _win_register_and_load(_win_candidate_cuda_dirs(), ("cudnn64_9.dll",))
    trt_dirs = [d for d in _win_spec_dirs("tensorrt_libs") if d.is_dir()]
    total += _win_register_and_load(trt_dirs, _WIN_TRT_DLLS)
    if _WIN_DLL_DIR_HANDLES or total:
        _LOGGER.info(
            "windows preload for rapidocr_trt_ep: %d dirs registered, %d DLLs resident",
            len(_WIN_DLL_DIR_HANDLES),
            total,
        )


def _label_provider(providers: list[str]) -> str:
    """ORT drops a provider from ``get_providers()`` when it fails to load/build, so
    membership is a reliable signal of what actually runs (trt | cuda | cpu)."""
    if "TensorrtExecutionProvider" in providers:
        return "trt"
    if "CUDAExecutionProvider" in providers:
        return "cuda"
    if "CPUExecutionProvider" in providers:
        return "cpu"
    return providers[0] if providers else "unknown"


def detect_stage_providers(engine: Any) -> dict[str, str]:
    """The ACTUAL EP each of det/cls/rec runs on -- read from the three real sessions,
    NOT from ``get_available_providers()`` (which lists TRT even when libnvinfer can't
    load). Target after a good build: ``{"det":"trt","rec":"trt","cls":"cuda"}``."""
    accessors = {
        "det": lambda e: e.text_det.infer.session,
        "cls": lambda e: e.text_cls.infer.session,
        "rec": lambda e: e.text_rec.session.session,
    }
    out: dict[str, str] = {}
    for stage, get_session in accessors.items():
        try:
            out[stage] = _label_provider(list(get_session(engine).get_providers()))
        except Exception:  # noqa: BLE001 -- rapidocr internal layout drift -> unknown
            out[stage] = "unknown"
    return out


def make_trt_session_class(
    trt_options: dict[str, Any],
    cuda_options: dict[str, Any],
    cpu_options: dict[str, Any],
) -> type:
    """A ``OrtInferSession`` subclass that builds its session with TRT -> CUDA -> CPU.

    Only ``_get_ep_list`` is overridden (the one method that picks providers), and it
    routes PER STAGE (cut 2.1): det/rec get TRT, cls gets CUDA (cls fails TRT engine
    build -- a cls graph issue). Every other behaviour -- preprocessing, ``__call__``,
    metadata, char list -- is inherited, so the output is RapidOCR's, just on a
    different EP. rapidocr is imported here (lazily) so this module stays light."""
    from rapidocr_onnxruntime.utils.infer_engine import OrtInferSession

    class _TrtOrtInferSession(OrtInferSession):
        def __init__(self, config):  # type: ignore[override]
            # Stash the model path BEFORE super().__init__ (it calls _get_ep_list),
            # so _get_ep_list can route by stage (det/cls/rec) from the model file.
            self._model_path = config.get("model_path", "")
            super().__init__(config)

        def _get_ep_list(self):  # type: ignore[override]
            self.use_cuda = self._check_cuda()  # parent verify-providers bookkeeping
            self.use_directml = False
            return [
                (name, dict(opts))
                for name, opts in ep_list_for_stage(
                    self._model_path,
                    trt_options=trt_options,
                    cuda_options=cuda_options,
                    cpu_options=cpu_options,
                )
            ]

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
        # D1: make libnvinfer / CUDA libs loadable IN-PROCESS (self-contained, env-free)
        # BEFORE building, so the TRT EP genuinely loads without external LD_LIBRARY_PATH.
        preload_inference_libs()
        result = build_engine_with_fallback(
            primary_factory=lambda: self._build_rapidocr(use_trt=True),
            fallback_factory=lambda: self._build_rapidocr(use_trt=False),
            warmup=self._warmup,
            on_fallback=lambda exc: _LOGGER.warning(
                "rapidocr_trt_ep build raised (%s: %s); rebuilt on plain CUDA",
                type(exc).__name__,
                exc,
            ),
        )
        self._engine = result.engine
        self.build_path = result.used  # which factory won (hard-failure safety net)
        # Per-stage ACTUAL provider -- the truth (ORT silently drops TRT to CUDA when
        # libnvinfer is missing; "the build didn't throw" does NOT mean TRT ran).
        self.stage_providers = detect_stage_providers(self._engine)
        # used_providers: back-compat summary (det's provider; "trt" when TRT is live).
        self.used_providers = detect_active_provider(self._engine)
        ok, diagnostic = classify_load_status(self.stage_providers)
        if not ok:
            # det/rec expected on TRT but fell back -> a genuine load/build problem, NOT
            # the expected cls=cuda. Surface it loudly; do NOT pretend TRT succeeded.
            _LOGGER.warning("%s (stage_providers=%s)", diagnostic, self.stage_providers)
        else:
            _LOGGER.info(
                "rapidocr_trt_ep ready: stage_providers=%s (fp16=%s)", self.stage_providers, fp16
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
