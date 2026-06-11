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


def ocr_image(image: Any) -> dict[str, Any]:
    """Run local RapidOCR on a PIL image or PNG bytes.

    OCR is best-effort: dependency, image decoding, and inference failures are
    returned as an empty result with an error payload so screen analysis can
    continue through Moondream.
    """

    try:
        prepared = _prepare_image(image)
        engine = _get_engine()
        with _INFER_LOCK:  # serialize inference across all OCR paths (Phase 7)
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
