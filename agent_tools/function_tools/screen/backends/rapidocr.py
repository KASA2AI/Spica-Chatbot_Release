from __future__ import annotations

import logging
from io import BytesIO
from threading import RLock
from typing import Any

from agent_tools.function_tools.screen.schema import ScreenToolError


_LOGGER = logging.getLogger(__name__)
_ENGINE: Any | None = None
_ENGINE_LOCK = RLock()  # guards engine *creation* (the singleton)
# Phase 7: serializes RapidOCR *inference*. EVERY OCR path -- inspect_screen
# (analyzer.analyze_screen_image_local) and the galgame OCR loop (RapidOcrAdapter)
# -- calls ocr_image(), so wrapping the inference here covers BOTH paths with one
# lock; two inferences never run concurrently on the shared _ENGINE (Phase 0 ⑤).
_INFER_LOCK = RLock()
_CUDA_PRELOADED = False
# W4-b Windows keep-alives: os.add_dll_directory handles AND ctypes.WinDLL objects
# must stay referenced for the process lifetime -- a dropped handle un-registers
# the directory; a dropped DLL object may unmap the library.
_WIN_DLL_DIR_HANDLES: list[Any] = []
_WIN_LOADED_DLLS: list[Any] = []


def ocr_image(image: Any) -> dict[str, Any]:
    """Run local RapidOCR on a PIL image or PNG bytes.

    OCR is best-effort: dependency, image decoding, and inference failures are
    returned as an empty result with an error payload so screen analysis can
    continue through Moondream.

    Thin caller over ``recognize_with_engine`` bound to the process-global engine
    provider + ``_INFER_LOCK`` -- byte-identical to the pre-extraction behaviour
    (the extraction lets the local_runtime TRT runtime reuse the same
    prepare/parse/error path with a DIFFERENT engine + lock, LOCAL_RUNTIME_PLAN
    cut 2 / D2).
    """
    return recognize_with_engine(_get_engine, image, _INFER_LOCK)


def recognize_with_engine(engine_provider: Any, image: Any, lock: Any) -> dict[str, Any]:
    """Prepare -> (locked) infer -> parse -> ``{engine, raw_text, blocks, error}``.

    The shared OCR body, parametrized over the engine + its inference lock.
    ``engine_provider`` is a zero-arg callable returning the engine, resolved INSIDE
    the protected block so an engine-load failure (e.g. missing rapidocr dependency,
    a ``ScreenToolError``) is caught here -- matching the pre-extraction ``ocr_image``
    that acquired the engine inside its try. Used by ``ocr_image`` (global CUDA
    engine via ``_get_engine`` + ``_INFER_LOCK``) and by the local_runtime TRT
    runtime (its own engine + lock). Best-effort: never raises -- failures come back
    as an empty result + error payload."""
    try:
        prepared = _prepare_image(image)
        engine = engine_provider()
        with lock:  # serialize inference on THIS engine
            raw_result = engine(prepared)
        blocks = _parse_blocks(raw_result)
        return {
            "engine": "rapidocr",
            "raw_text": "\n".join(block["text"] for block in blocks if block.get("text")),
            "blocks": blocks,
            "error": None,
        }
    except ScreenToolError as exc:
        _log_ocr_error(exc)
        return _empty_result(_error_payload(exc.code, exc.message, type(exc).__name__))
    except Exception as exc:
        _log_ocr_error(exc)
        return _empty_result(
            _error_payload(
                "SCREEN_OCR_FAILED",
                f"RapidOCR 识别失败：{type(exc).__name__}: {exc}",
                type(exc).__name__,
            )
        )


def clear_rapidocr_engine() -> None:
    global _ENGINE
    with _ENGINE_LOCK:
        _ENGINE = None


def _preload_cuda_libraries() -> None:
    """Pull the pip ``nvidia-*`` CUDA shared libs into the process's GLOBAL symbol
    table BEFORE RapidOCR/onnxruntime builds its CUDA provider, so the provider
    resolves cublas/cudnn/cuda_runtime/... without LD_LIBRARY_PATH.

    Pure in-process ctypes preload -- NO env reads (CLAUDE.md #4), NO LD_LIBRARY_PATH.
    Best-effort: if the nvidia packages / libs are absent (no-GPU env), skip silently
    so RapidOCR falls back to CPU. Runs at most once per process. MUST run before the
    RapidOCR(...) instantiation in _get_engine."""
    global _CUDA_PRELOADED
    if _CUDA_PRELOADED:
        return
    _CUDA_PRELOADED = True  # mark first: a partial/failed scan must not retry every call
    import os

    if os.name == "nt":
        # W4-b: Windows resolves DLLs by LoadLibrary search, not global symbols --
        # a different mechanism entirely, so it gets its own helper. The Linux
        # route below is untouched (zero behavior drift).
        _preload_cuda_libraries_windows()
        return
    try:
        import ctypes
        from pathlib import Path

        import nvidia  # the pip nvidia-cu* namespace package
    except Exception:  # noqa: BLE001 -- no nvidia packages -> CPU path, silent
        return
    search_paths = [Path(p) for p in (getattr(nvidia, "__path__", None) or [])]
    if not search_paths and getattr(nvidia, "__file__", None):
        search_paths = [Path(nvidia.__file__).parent]
    loaded = 0
    for base in search_paths:
        try:
            libs = sorted(base.glob("*/lib/*.so*"))
        except OSError:
            continue
        for lib in libs:
            try:
                ctypes.CDLL(str(lib), mode=ctypes.RTLD_GLOBAL)
                loaded += 1
            except OSError:
                continue  # a lib that won't load is skipped; the rest still help
    if loaded:
        _LOGGER.info("preloaded %d nvidia CUDA libraries for RapidOCR GPU", loaded)


def _win_spec_dirs(package: str) -> list:
    """A package's on-disk dirs via ``find_spec`` WITHOUT importing it (never
    ``import torch`` just to fix a DLL search path -- W4-b ruling). Handles
    namespace packages (nvidia) via ``submodule_search_locations``."""
    import importlib.util
    from pathlib import Path

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


def _win_candidate_cuda_dirs() -> list:
    """Dirs that may carry cuDNN 9 / CUDA runtime DLLs on Windows, most likely
    first: pip nvidia-* wheels ship them under ``*/bin``; the torch wheel bundles
    them in ``torch/lib``; ctranslate2 bundles cudnn64_9.dll next to itself
    (all three verified in the W4-a probe, docs/windows_w4_probe.md)."""
    dirs = []
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


def _preload_cuda_libraries_windows() -> None:
    """Windows variant (W4-b, gate-1 validated): ORT's CUDA EP resolves
    ``cudnn64_9.dll`` via the standard LoadLibrary search, which does NOT include
    wheel dirs -- without help the EP is listed but fails init and silently falls
    back to CPU (W4-a §4.E). So: register every candidate dir
    (``os.add_dll_directory``) AND force-load the core cuDNN DLL
    (``ctypes.WinDLL``) so it is already mapped when the EP asks. Handles and DLL
    objects are kept alive at module level. Env-free, best-effort, no torch
    import (``find_spec`` locates the dirs)."""
    import ctypes
    import os

    candidates = _win_candidate_cuda_dirs()
    for directory in candidates:
        try:
            _WIN_DLL_DIR_HANDLES.append(os.add_dll_directory(str(directory)))
        except OSError:
            continue
    loaded = False
    for directory in candidates:
        dll = directory / "cudnn64_9.dll"
        if not dll.is_file():
            continue
        try:
            _WIN_LOADED_DLLS.append(ctypes.WinDLL(str(dll)))
            loaded = True
            break  # one resident copy is enough
        except OSError:
            continue  # a copy that won't load is skipped; try the next candidate
    if _WIN_DLL_DIR_HANDLES or loaded:
        _LOGGER.info(
            "windows CUDA preload: %d dirs registered, cudnn64_9 resident=%s",
            len(_WIN_DLL_DIR_HANDLES),
            loaded,
        )


def _get_engine() -> Any:
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            return _ENGINE
        try:
            _preload_cuda_libraries()  # MUST precede RapidOCR(): CUDA libs into global symbols
            rapidocr_class = _load_rapidocr_class()
            try:
                # GPU on det/cls/rec. RapidOCR/onnxruntime auto-falls back to CPU when
                # CUDA is unavailable, so this never crashes a no-GPU box (Change 3).
                _ENGINE = rapidocr_class(det_use_cuda=True, cls_use_cuda=True, rec_use_cuda=True)
            except TypeError:
                # a RapidOCR build without these kwargs -> default construction.
                _ENGINE = rapidocr_class()
            return _ENGINE
        except ScreenToolError:
            raise
        except Exception as exc:
            raise ScreenToolError(
                "SCREEN_OCR_LOAD_FAILED",
                f"RapidOCR 初始化失败：{type(exc).__name__}: {exc}",
            ) from exc


def _load_rapidocr_class() -> Any:
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ScreenToolError(
            "SCREEN_OCR_DEPENDENCY_MISSING",
            "缺少 rapidocr-onnxruntime，无法运行本地 OCR。请安装：pip install 'rapidocr-onnxruntime>=1.4,<2'",
        ) from exc
    return RapidOCR


def _prepare_image(image: Any) -> Any:
    try:
        import numpy as np  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ScreenToolError(
            "SCREEN_OCR_DEPENDENCY_MISSING",
            f"缺少 OCR 图片依赖：{type(exc).__name__}: {exc}",
        ) from exc

    try:
        if isinstance(image, Image.Image):
            pil_image = image
        elif isinstance(image, (bytes, bytearray)):
            pil_image = Image.open(BytesIO(bytes(image)))
        else:
            raise ScreenToolError("SCREEN_OCR_INVALID_IMAGE", "RapidOCR 要求 PIL.Image.Image 或 PNG bytes。")
        return np.asarray(pil_image.convert("RGB"))
    except ScreenToolError:
        raise
    except Exception as exc:
        raise ScreenToolError(
            "SCREEN_OCR_IMAGE_DECODE_FAILED",
            f"OCR 图片解码失败：{type(exc).__name__}: {exc}",
        ) from exc


def _parse_blocks(raw_result: Any) -> list[dict[str, Any]]:
    result = _unwrap_result(raw_result)
    if result is None:
        return []
    if isinstance(result, dict):
        for key in ("results", "result", "ocr_result", "data"):
            if key in result:
                result = result.get(key)
                break
    if not isinstance(result, (list, tuple)):
        return []

    blocks: list[dict[str, Any]] = []
    for item in result:
        block = _parse_block(item)
        if block and block["text"]:
            blocks.append(block)
    return blocks


def _unwrap_result(raw_result: Any) -> Any:
    if isinstance(raw_result, tuple) and raw_result:
        return raw_result[0]
    return raw_result


def _parse_block(item: Any) -> dict[str, Any] | None:
    if isinstance(item, dict):
        text = str(item.get("text") or item.get("rec_text") or item.get("label") or "").strip()
        confidence = _safe_float(item.get("confidence", item.get("score", item.get("rec_score"))), 0.0)
        points = _normalize_points(item.get("box") or item.get("points") or item.get("dt_box"))
        return {"text": text, "confidence": confidence, "box": points}

    if not isinstance(item, (list, tuple)):
        return None

    if len(item) >= 3:
        points, text, confidence = item[0], item[1], item[2]
        return {
            "text": str(text or "").strip(),
            "confidence": _safe_float(confidence, 0.0),
            "box": _normalize_points(points),
        }
    if len(item) >= 2 and isinstance(item[0], str):
        return {
            "text": str(item[0] or "").strip(),
            "confidence": _safe_float(item[1], 0.0),
            "box": [],
        }
    return None


def _normalize_points(value: Any) -> list[list[float]]:
    if not isinstance(value, (list, tuple)):
        return []
    points: list[list[float]] = []
    for point in value:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            points.append([_safe_float(point[0], 0.0), _safe_float(point[1], 0.0)])
    return points


def _empty_result(error: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "engine": "rapidocr",
        "raw_text": "",
        "blocks": [],
        "error": error,
    }


def _error_payload(code: str, message: str, error_type: str) -> dict[str, Any]:
    return {
        "stage": "ocr",
        "code": code,
        "message": message,
        "type": error_type,
        "recoverable": True,
    }


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _log_ocr_error(exc: BaseException) -> None:
    _LOGGER.warning(
        "RapidOCR failed: %s: %s",
        type(exc).__name__,
        exc,
        exc_info=not isinstance(exc, ScreenToolError),
    )
