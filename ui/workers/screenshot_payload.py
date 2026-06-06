from __future__ import annotations

from typing import Any


def make_pending_screen_attachment(
    *,
    image_bytes: bytes | None = None,
    png_bytes: bytes | None = None,
    image_metadata: dict[str, Any],
    captured_at: str,
    screen_name: str,
    screen_index: int,
    logical_rect: dict[str, int],
    physical_rect: dict[str, int],
    device_pixel_ratio: float,
    quality: int | None = None,
) -> dict[str, Any]:
    payload_bytes = image_bytes if image_bytes is not None else png_bytes
    if payload_bytes is None:
        payload_bytes = b""
    sent_resolution = image_metadata.get("sent_resolution") if isinstance(image_metadata.get("sent_resolution"), dict) else {}
    original_resolution = (
        image_metadata.get("original_resolution") if isinstance(image_metadata.get("original_resolution"), dict) else {}
    )
    width = int(sent_resolution.get("width") or original_resolution.get("width") or 0)
    height = int(sent_resolution.get("height") or original_resolution.get("height") or 0)
    return {
        "kind": "screen_capture",
        "target": "selected_region",
        "mode": "region",
        "source": "manual_region_selection",
        "created_at": captured_at,
        "captured_at": captured_at,
        "image_bytes": payload_bytes,
        "mime_type": "image/png",
        "width": width,
        "height": height,
        "original_resolution": image_metadata.get("original_resolution"),
        "sent_resolution": image_metadata.get("sent_resolution"),
        "downscaled": bool(image_metadata.get("downscaled", False)),
        "format": "png",
        "quality": None,
        "region": {
            "screen_name": screen_name,
            "screen_index": int(screen_index),
            "logical": logical_rect,
            "physical": physical_rect,
            "device_pixel_ratio": float(device_pixel_ratio),
        },
    }
