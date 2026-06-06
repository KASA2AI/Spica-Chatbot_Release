from __future__ import annotations

from datetime import datetime
from io import BytesIO
from typing import Any

from agent_tools.function_tools.screen.config import BASE_DIR, ScreenPipelineConfig
from agent_tools.function_tools.screen.schema import ScreenToolError


def encode_screen_image_png(image: Any, config: ScreenPipelineConfig) -> tuple[bytes, dict[str, Any]]:
    """Encode a screen image as PNG bytes for local analysis/attachment.

    This function only prepares local PNG bytes. It does not create JPEGs or
    payloads intended for remote vision APIs.
    """

    try:
        resample = _resample_filter()
        original_width, original_height = int(image.width), int(image.height)
        max_side = max(1, int(config.max_side))
        longest = max(original_width, original_height)
        processed = image.convert("RGB")
        downscaled = False

        if longest > max_side:
            scale = max_side / float(longest)
            sent_width = max(1, round(original_width * scale))
            sent_height = max(1, round(original_height * scale))
            processed = processed.resize((sent_width, sent_height), resample)
            downscaled = True
        else:
            sent_width, sent_height = original_width, original_height

        buffer = BytesIO()
        processed.save(buffer, format="PNG")
        png_bytes = buffer.getvalue()
        metadata: dict[str, Any] = {
            "original_resolution": {"width": original_width, "height": original_height},
            "sent_resolution": {"width": sent_width, "height": sent_height},
            "downscaled": downscaled,
            "format": "png",
            "bytes": len(png_bytes),
        }
        if config.debug_save_images:
            metadata["debug_image_path"] = _save_debug_image(png_bytes)
        return png_bytes, metadata
    except ScreenToolError:
        raise
    except Exception as exc:
        raise ScreenToolError("SCREEN_CAPTURE_FAILED", f"截图处理失败：{exc}") from exc


def _resample_filter() -> Any:
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ScreenToolError(
            "SCREEN_CAPTURE_DEPENDENCY_MISSING",
            "缺少图片处理依赖 Pillow，请安装 Pillow。",
        ) from exc
    resampling = getattr(Image, "Resampling", None)
    if resampling is not None and hasattr(resampling, "LANCZOS"):
        return resampling.LANCZOS
    return getattr(Image, "LANCZOS", getattr(Image, "ANTIALIAS", 1))


def _save_debug_image(png_bytes: bytes) -> str:
    debug_dir = BASE_DIR / "static" / "screen_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    path = debug_dir / f"screen_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
    path.write_bytes(png_bytes)
    return str(path)
