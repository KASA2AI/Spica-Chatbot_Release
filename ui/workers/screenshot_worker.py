from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from typing import Any

from PySide6.QtCore import QBuffer, QIODevice, QRect, QThread, Signal
from PySide6.QtGui import QGuiApplication, QPixmap

from agent_tools.function_tools.screen.config import load_screen_config
from agent_tools.function_tools.screen.image_processing import encode_screen_image_png
from agent_tools.function_tools.screen.schema import ScreenToolError
from ui.workers.screenshot_payload import make_pending_screen_attachment


class ScreenshotWorker(QThread):
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(
        self, payload: dict[str, Any], parent: Any | None = None, config: Any | None = None
    ) -> None:
        super().__init__(parent)
        self.payload = dict(payload)
        # P0b 2a: the overlay passes the host-resolved ScreenPipelineConfig;
        # None falls back to load_screen_config() (standalone use).
        self.config = config

    def run(self) -> None:
        try:
            self.finished_ok.emit(build_selected_region_attachment(self.payload, self.config))
        except ScreenToolError as exc:
            self.failed.emit(exc.message)
        except Exception as exc:
            self.failed.emit(str(exc))


def build_selected_region_attachment(
    payload: dict[str, Any], config: Any | None = None
) -> dict[str, Any]:
    screen = payload.get("screen") or QGuiApplication.primaryScreen()
    if screen is None:
        raise ScreenToolError("SCREEN_CAPTURE_FAILED", "没有可用显示器，无法截图。")
    logical_rect = payload.get("logical_rect")
    if not isinstance(logical_rect, QRect) or logical_rect.isEmpty():
        raise ScreenToolError("SCREEN_CAPTURE_FAILED", "没有有效截图区域。")

    pixmap = screen.grabWindow(
        0,
        logical_rect.x(),
        logical_rect.y(),
        logical_rect.width(),
        logical_rect.height(),
    )
    if pixmap.isNull():
        raise ScreenToolError("SCREEN_CAPTURE_FAILED", "系统没有返回截图内容。")

    pil_image = _pixmap_to_pil_image(pixmap)
    config = config or load_screen_config()
    image_bytes, image_metadata = encode_screen_image_png(pil_image, config)
    dpr = float(payload.get("device_pixel_ratio") or screen.devicePixelRatio() or 1.0)
    physical_rect = _physical_rect_for_selection(logical_rect, screen.geometry(), dpr, pixmap)
    screen_index = payload.get("screen_index")
    if screen_index is None:
        screen_index = -1
    return make_pending_screen_attachment(
        image_bytes=image_bytes,
        image_metadata=image_metadata,
        captured_at=datetime.now(timezone.utc).isoformat(),
        screen_name=str(payload.get("screen_name") or screen.name() or ""),
        screen_index=int(screen_index),
        logical_rect=_rect_payload(logical_rect),
        physical_rect=_rect_payload(physical_rect),
        device_pixel_ratio=dpr,
    )


def _pixmap_to_pil_image(pixmap: QPixmap) -> Any:
    try:
        from PIL import Image
    except ImportError as exc:
        raise ScreenToolError(
            "SCREEN_CAPTURE_DEPENDENCY_MISSING",
            "缺少图片处理依赖 Pillow，请安装：pip install Pillow",
        ) from exc

    image = pixmap.toImage()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return Image.open(BytesIO(bytes(buffer.data()))).convert("RGB")


def _physical_rect_for_selection(
    logical_rect: QRect,
    screen_geometry: QRect,
    dpr: float,
    pixmap: QPixmap,
) -> QRect:
    relative_x = logical_rect.x() - screen_geometry.x()
    relative_y = logical_rect.y() - screen_geometry.y()
    width = pixmap.width() or round(logical_rect.width() * dpr)
    height = pixmap.height() or round(logical_rect.height() * dpr)
    return QRect(round(relative_x * dpr), round(relative_y * dpr), width, height)


def _rect_payload(rect: QRect) -> dict[str, int]:
    return {
        "x": int(rect.x()),
        "y": int(rect.y()),
        "width": int(rect.width()),
        "height": int(rect.height()),
    }
