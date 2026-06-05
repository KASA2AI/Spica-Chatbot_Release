from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_tools.function_tools.screen.schema import ScreenToolError


@dataclass(frozen=True)
class ScreenCaptureResult:
    image: Any
    metadata: dict[str, Any]


def capture_full_screen() -> ScreenCaptureResult:
    try:
        import mss  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ScreenToolError(
            "SCREEN_CAPTURE_DEPENDENCY_MISSING",
            "缺少屏幕截图依赖 mss，请安装：pip install mss Pillow",
        ) from exc

    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ScreenToolError(
            "SCREEN_CAPTURE_DEPENDENCY_MISSING",
            "缺少图片处理依赖 Pillow，请安装：pip install Pillow",
        ) from exc

    try:
        with mss.mss() as sct:
            monitor = _primary_monitor(sct.monitors)
            raw = sct.grab(monitor)
            image = Image.frombytes("RGB", raw.size, raw.rgb)
            return ScreenCaptureResult(
                image=image,
                metadata={
                    "captured_scope": "full_screen",
                    "source": "automatic_screenshot",
                    "window": None,
                    "region": None,
                    "monitor": {
                        "left": int(monitor.get("left", 0)),
                        "top": int(monitor.get("top", 0)),
                        "width": int(monitor.get("width", image.width)),
                        "height": int(monitor.get("height", image.height)),
                    },
                },
            )
    except ScreenToolError:
        raise
    except PermissionError as exc:
        raise ScreenToolError(
            "SCREEN_PERMISSION_DENIED",
            "系统拒绝屏幕截图权限，请在系统隐私/屏幕录制权限中允许当前应用。",
        ) from exc
    except OSError as exc:
        if _looks_like_permission_error(str(exc)):
            raise ScreenToolError(
                "SCREEN_PERMISSION_DENIED",
                "系统拒绝屏幕截图权限，请在系统隐私/屏幕录制权限中允许当前应用。",
            ) from exc
        raise ScreenToolError("SCREEN_CAPTURE_FAILED", f"截图失败：{exc}") from exc
    except Exception as exc:
        if _looks_like_permission_error(str(exc)):
            raise ScreenToolError(
                "SCREEN_PERMISSION_DENIED",
                "系统拒绝屏幕截图权限，请在系统隐私/屏幕录制权限中允许当前应用。",
            ) from exc
        raise ScreenToolError("SCREEN_CAPTURE_FAILED", f"截图失败：{exc}") from exc


def _primary_monitor(monitors: list[dict[str, Any]]) -> dict[str, Any]:
    if len(monitors) > 1:
        return monitors[1]
    if monitors:
        return monitors[0]
    raise ScreenToolError("SCREEN_CAPTURE_FAILED", "没有检测到可截图的显示器。")


def _looks_like_permission_error(message: str) -> bool:
    lowered = (message or "").lower()
    return any(
        token in lowered
        for token in (
            "permission",
            "denied",
            "not authorized",
            "not authorised",
            "screen recording",
            "privacy",
            "accessibility",
        )
    )
