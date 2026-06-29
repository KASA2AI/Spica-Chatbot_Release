from __future__ import annotations

from contextvars import ContextVar
from io import BytesIO
from time import perf_counter
from typing import Any

from agent_tools.function_tools.screen.backends.ocr_runtime import run_ocr
from agent_tools.function_tools.screen.config import (
    ScreenPipelineConfig,
    load_screen_config,
    resolve_effective_screen_config,
)
from agent_tools.function_tools.screen.model_manager import get_moondream_manager
from agent_tools.function_tools.screen.schema import ScreenToolError, build_screen_observation


_LAST_ANALYSIS_METADATA: ContextVar[dict[str, Any] | None] = ContextVar(
    "spica_last_screen_analysis_metadata",
    default=None,
)


def analyze_screen_image_local(
    image: Any,
    mode: str,
    prompt: str | None = None,
    *,
    config: ScreenPipelineConfig | None = None,
    capture: dict[str, Any] | None = None,
    performance: dict[str, Any] | None = None,
    question_type: str | None = None,
) -> dict[str, Any]:
    config = config or load_screen_config()
    if not config.enabled:
        raise ScreenToolError("SCREEN_DISABLED", "本地 screen pipeline 已禁用。")

    pil_image = _ensure_pil_image(image)
    target = str(mode or "full_screen")
    user_question = prompt or ""
    resolved_question_type = question_type or _classify_screen_question(user_question)
    capture_payload = _capture_for_image(pil_image, target, capture)

    started = perf_counter()
    metadata: dict[str, Any] = {
        "screen_analysis_engine": "moondream_local",
        "screen_analysis_model": config.model_id,
        "screen_analysis_revision": config.revision,
        "screen_analysis_local": True,
    }
    try:
        perf = dict(performance or {})
        errors: list[dict[str, Any]] = []
        visible_text = {"engine": config.ocr_engine or "rapidocr", "raw_text": "", "blocks": []}

        if config.ocr_enabled:
            ocr_started = perf_counter()
            try:
                # Path B収口 (§2.2): route through the unifying seam, not the bare
                # ocr_image -- so a provider swap covers inspect_screen too. Default
                # (no provider installed) falls back to ocr_image, byte-identical.
                ocr_result = run_ocr(pil_image)
            except Exception as exc:
                ocr_result = {
                    "engine": "rapidocr",
                    "raw_text": "",
                    "blocks": [],
                    "error": _stage_error(
                        "ocr",
                        "SCREEN_OCR_FAILED",
                        f"RapidOCR 识别失败：{type(exc).__name__}: {exc}",
                        exc,
                    ),
                }
            perf["ocr_ms"] = _elapsed_ms(ocr_started)
            visible_text = {
                "engine": str(ocr_result.get("engine") or "rapidocr"),
                "raw_text": str(ocr_result.get("raw_text") or ""),
                "blocks": ocr_result.get("blocks") if isinstance(ocr_result.get("blocks"), list) else [],
            }
            ocr_error = ocr_result.get("error")
            if isinstance(ocr_error, dict):
                errors.append(ocr_error)
        else:
            perf.setdefault("ocr_ms", 0.0)

        text = ""
        question = _build_moondream_question(
            user_question,
            resolved_question_type,
            target,
            bool(config.reasoning),
        )
        infer_started = perf_counter()
        try:
            text = get_moondream_manager(config).query(pil_image, question, reasoning=bool(config.reasoning))
        except ScreenToolError as exc:
            errors.append(_stage_error("moondream", exc.code, exc.message, exc))
        except Exception as exc:
            errors.append(
                _stage_error(
                    "moondream",
                    "SCREEN_MOONDREAM_INFERENCE_FAILED",
                    f"Moondream 推理失败：{type(exc).__name__}: {exc}",
                    exc,
                )
            )
        finally:
            moondream_ms = _elapsed_ms(infer_started)

        total_ms = _elapsed_ms(started)
        perf.setdefault("capture_ms", 0.0)
        perf["moondream_ms"] = moondream_ms
        perf["total_ms"] = total_ms
        metadata["screen_analysis_ocr_ms"] = perf.get("ocr_ms", 0.0)
        metadata["screen_analysis_moondream_ms"] = moondream_ms
        metadata["screen_analysis_total_ms"] = total_ms

        return build_screen_observation(
            user_question=user_question,
            question_type=resolved_question_type,
            target=target,
            capture=capture_payload,
            visual_summary={
                "engine": "moondream",
                "model": config.model_id,
                "revision": config.revision,
                "text": text,
            },
            visible_text=visible_text,
            performance=perf,
            errors=errors,
            answer={"direct_answer": text, "confidence": 0.0},
            followup={
                "context_for_next_turn": text,
                "needs_followup_capture": False,
                "suggested_capture": None,
            },
        )
    finally:
        _LAST_ANALYSIS_METADATA.set(dict(metadata))


def analyze_screen_png_local(
    png_bytes: bytes,
    mode: str,
    prompt: str | None = None,
    *,
    config: ScreenPipelineConfig | None = None,
    capture: dict[str, Any] | None = None,
    performance: dict[str, Any] | None = None,
    question_type: str | None = None,
) -> dict[str, Any]:
    if not isinstance(png_bytes, (bytes, bytearray)):
        raise ScreenToolError("SCREEN_ANALYSIS_FAILED", "screen PNG 分析要求 bytes。")
    image = _image_from_bytes(bytes(png_bytes))
    capture_payload = {"image_format": "png", **dict(capture or {})}
    return analyze_screen_image_local(
        image,
        mode,
        prompt,
        config=config,
        capture=capture_payload,
        performance=performance,
        question_type=question_type,
    )


def analyze_screen_attachment(
    *,
    attachment: dict[str, Any],
    user_question: str,
    config: ScreenPipelineConfig | None = None,
) -> dict[str, Any]:
    # P0b 3 (③-B, 域内开关路线): optional injection; the fallback follows the
    # carrier switch so the attachment turn tracks the same effective chain as
    # production after the json carrier retires. No runtime/deps file changes
    # (the frozen sync_chain and the stages node are untouched; contract tests
    # patch this whole function, so the golden trio stays byte-identical).
    config = config or resolve_effective_screen_config()
    image_bytes = attachment.get("image_bytes")
    if not isinstance(image_bytes, (bytes, bytearray)):
        raise ScreenToolError("SCREEN_ANALYSIS_FAILED", "pending screenshot 缺少图片数据。")
    image = _image_from_bytes(bytes(image_bytes))

    mode = "region"
    source = str(attachment.get("source") or "manual_region_selection")
    capture = {
        "mode": mode,
        "captured_scope": mode,
        "source": source,
        "window": None,
        "region": attachment.get("region") if isinstance(attachment.get("region"), dict) else None,
        "width": _safe_positive_int(attachment.get("width"), image.width),
        "height": _safe_positive_int(attachment.get("height"), image.height),
        "image_format": "png",
        "created_at": attachment.get("created_at") or attachment.get("captured_at"),
        "captured_at": attachment.get("captured_at"),
        "mime_type": "image/png",
        "image": {
            "original_resolution": attachment.get("original_resolution"),
            "sent_resolution": attachment.get("sent_resolution"),
            "downscaled": bool(attachment.get("downscaled", False)),
            "format": "png",
        },
    }
    return analyze_screen_image_local(
        image,
        mode,
        user_question,
        config=config,
        capture=capture,
        question_type=_classify_screen_question(user_question),
    )


def get_last_screen_analysis_metadata() -> dict[str, Any]:
    return dict(_LAST_ANALYSIS_METADATA.get() or {})


def clear_last_screen_analysis_metadata() -> None:
    _LAST_ANALYSIS_METADATA.set(None)


def _ensure_pil_image(image: Any) -> Any:
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ScreenToolError(
            "SCREEN_CAPTURE_DEPENDENCY_MISSING",
            "缺少图片处理依赖 Pillow，请安装 Pillow。",
        ) from exc

    if not isinstance(image, Image.Image):
        raise ScreenToolError("SCREEN_ANALYSIS_FAILED", "本地 screen analyzer 要求 PIL.Image.Image。")
    return image


def _capture_for_image(image: Any, mode: str, capture: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(capture or {})
    payload.setdefault("mode", mode)
    payload.setdefault("captured_scope", mode)
    payload.setdefault("source", "automatic_screenshot" if mode == "full_screen" else "manual_region_selection")
    payload.setdefault("window", None)
    payload.setdefault("region", None)
    payload.setdefault("width", getattr(image, "width", 0))
    payload.setdefault("height", getattr(image, "height", 0))
    payload.setdefault("image_format", "png")
    return payload


def _image_from_bytes(image_bytes: bytes) -> Any:
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ScreenToolError(
            "SCREEN_CAPTURE_DEPENDENCY_MISSING",
            "缺少图片处理依赖 Pillow，请安装 Pillow。",
        ) from exc
    try:
        return Image.open(BytesIO(image_bytes)).convert("RGB")
    except Exception as exc:
        raise ScreenToolError("SCREEN_ANALYSIS_FAILED", f"pending screenshot 图片解码失败：{exc}") from exc


def _stage_error(stage: str, code: str, message: str, exc: BaseException | None = None) -> dict[str, Any]:
    return {
        "stage": stage,
        "code": code,
        "message": message,
        "type": type(exc).__name__ if exc is not None else "",
        "recoverable": True,
    }


def _build_moondream_question(user_question: str, question_type: str, target: str, reasoning: bool) -> str:
    guidance = (
        "Answer the user's question using only this single screenshot. "
        "Do not claim live monitoring, clicking, typing, history, files, or background screen access. "
        "If sensitive content is visible, summarize its type without transcribing secrets. "
        "If uncertain, say so."
    )
    if reasoning:
        guidance += " Briefly reason about the visible UI before answering."
    return "\n".join(
        [
            guidance,
            f"Target: {target}",
            f"Question type: {question_type}",
            f"User question: {user_question or 'Describe the visible screen.'}",
        ]
    )


def _elapsed_ms(started: float) -> float:
    return round((perf_counter() - started) * 1000.0, 3)


def _format_from_mime(value: Any) -> str:
    text = str(value or "").lower()
    if "/" not in text:
        return ""
    return text.rsplit("/", 1)[-1]


def _safe_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed > 0 else int(default)


def _classify_screen_question(question: str) -> str:
    text = (question or "").lower()
    if any(token in text for token in ("几个", "多少个", "有多少", "count", "how many")):
        return "counting"
    if any(token in text for token in ("报错", "错误", "异常", "error", "warning", "警告")):
        return "diagnosis"
    if any(token in text for token in ("出自", "哪个动漫", "是什么", "是谁", "网站", "识别", "identify")):
        return "identification"
    if any(token in text for token in ("在干嘛", "正在", "打开", "浏览", "doing")):
        return "activity"
    return "general_observation"
