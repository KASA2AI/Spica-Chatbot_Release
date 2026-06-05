from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from agent_tools.function_tools.screen.config import BASE_DIR, ScreenVisionConfig
from agent_tools.function_tools.screen.schema import ScreenToolError


def prepare_image_for_vision(image: Any, config: ScreenVisionConfig) -> tuple[bytes, dict[str, Any]]:
    try:
        resample = _resample_filter()
        original_width, original_height = int(image.width), int(image.height)
        max_long_edge = max(1, int(config.max_long_edge))
        longest = max(original_width, original_height)
        processed = image.convert("RGB")
        downscaled = False

        if longest > max_long_edge:
            scale = max_long_edge / float(longest)
            sent_width = max(1, round(original_width * scale))
            sent_height = max(1, round(original_height * scale))
            processed = processed.resize((sent_width, sent_height), resample)
            downscaled = True
        else:
            sent_width, sent_height = original_width, original_height

        buffer = BytesIO()
        quality = max(30, min(95, int(config.jpeg_quality)))
        processed.save(buffer, format="JPEG", quality=quality, optimize=True)
        jpeg_bytes = buffer.getvalue()
        metadata: dict[str, Any] = {
            "original_resolution": {"width": original_width, "height": original_height},
            "sent_resolution": {"width": sent_width, "height": sent_height},
            "downscaled": downscaled,
            "format": "jpeg",
            "quality": quality,
            "bytes": len(jpeg_bytes),
        }
        if config.debug_save_images:
            metadata["debug_image_path"] = _save_debug_image(jpeg_bytes)
        return jpeg_bytes, metadata
    except ScreenToolError:
        raise
    except Exception as exc:
        raise ScreenToolError("SCREEN_CAPTURE_FAILED", f"截图压缩失败：{exc}") from exc


def _resample_filter() -> Any:
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ScreenToolError(
            "SCREEN_CAPTURE_DEPENDENCY_MISSING",
            "缺少图片处理依赖 Pillow，请安装：pip install Pillow",
        ) from exc
    resampling = getattr(Image, "Resampling", None)
    if resampling is not None and hasattr(resampling, "LANCZOS"):
        return resampling.LANCZOS
    return getattr(Image, "LANCZOS", getattr(Image, "ANTIALIAS", 1))


def _save_debug_image(jpeg_bytes: bytes) -> str:
    debug_dir = BASE_DIR / "static" / "screen_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    path = debug_dir / f"screen_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
    path.write_bytes(jpeg_bytes)
    return str(path)
