from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


SCREEN_OBSERVATION_SCHEMA_VERSION = "screen_observation.v1"
SCREEN_OBSERVATION_TYPE = "screen_observation"


class ScreenToolError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def build_screen_observation(
    *,
    user_question: str = "",
    question_type: str = "general_observation",
    target: str = "full_screen",
    capture: dict[str, Any] | None = None,
    visual_summary: dict[str, Any] | str | None = None,
    visible_text: dict[str, Any] | None = None,
    performance: dict[str, Any] | None = None,
    errors: list[Any] | None = None,
    answer: dict[str, Any] | str | None = None,
    followup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a local screen_observation.v1 payload.

    The new local pipeline should use this builder. It emits the new local
    fields while preserving legacy keys consumed by the existing agent layer.
    """

    observation = empty_screen_observation(
        user_question=user_question,
        question_type=question_type,
        target=target,
        capture=_normalize_capture_payload(capture, target=target),
    )
    observation["visual_summary"] = _normalize_visual_summary(visual_summary)
    observation["visible_text"] = _normalize_visible_text(visible_text)
    observation["performance"] = _normalize_performance(performance)
    observation["errors"] = _normalize_errors(errors)

    answer_payload = _normalize_answer(answer)
    if not answer_payload["direct_answer"]:
        answer_payload["direct_answer"] = str(observation["visual_summary"].get("text") or "").strip()
    observation["answer"] = answer_payload

    followup_payload = _normalize_followup(followup)
    if not followup_payload["context_for_next_turn"]:
        followup_payload["context_for_next_turn"] = observation["answer"]["direct_answer"]
    observation["followup"] = followup_payload
    return observation


def empty_screen_observation(
    *,
    user_question: str,
    question_type: str,
    target: str,
    capture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": SCREEN_OBSERVATION_SCHEMA_VERSION,
        "schema_version": SCREEN_OBSERVATION_SCHEMA_VERSION,
        "type": SCREEN_OBSERVATION_TYPE,
        "source": "screen",
        "request": {
            "user_question": user_question or "",
            "question_type": question_type or "general_observation",
            "target": target or "full_screen",
        },
        "capture": _normalize_capture_payload(capture, target=target),
        "visual_summary": _normalize_visual_summary(None),
        "answer": {
            "direct_answer": "",
            "confidence": 0.0,
        },
        "scene": {},
        "visible_apps": [],
        "visible_text": {},
        "objects": [],
        "ui_elements": [],
        "counts": [],
        "identification": None,
        "diagnosis": None,
        "game": None,
        "spatial_hints": [],
        "ambiguity": [],
        "followup": {
            "context_for_next_turn": "",
            "needs_followup_capture": False,
            "suggested_capture": None,
        },
        "privacy": {},
        "limitations": [],
        "performance": _normalize_performance(None),
        "errors": [],
    }


def normalize_screen_observation(
    value: dict[str, Any] | None,
    *,
    user_question: str,
    question_type: str,
    target: str,
    capture: dict[str, Any],
) -> dict[str, Any]:
    base = empty_screen_observation(
        user_question=user_question,
        question_type=question_type,
        target=target,
        capture=capture,
    )
    if isinstance(value, dict):
        _deep_update(base, value)

    base["schema_version"] = SCREEN_OBSERVATION_SCHEMA_VERSION
    base["schema"] = SCREEN_OBSERVATION_SCHEMA_VERSION
    base["type"] = SCREEN_OBSERVATION_TYPE
    base["source"] = str(base.get("source") or "screen")
    base["request"] = {
        **base.get("request", {}),
        "user_question": user_question or "",
        "question_type": question_type or "general_observation",
        "target": target or "full_screen",
    }
    merged_capture = {**(base.get("capture") or {}), **capture}
    base["capture"] = _normalize_capture_payload(merged_capture, target=target)

    base["visual_summary"] = _normalize_visual_summary(base.get("visual_summary"))
    base["visible_text"] = _normalize_visible_text(base.get("visible_text"))
    base["performance"] = _normalize_performance(base.get("performance"))
    base["errors"] = _normalize_errors(base.get("errors"))
    base["answer"] = _normalize_answer(base.get("answer"))

    base["followup"] = _normalize_followup(base.get("followup") if isinstance(base.get("followup"), dict) else None)

    for key in ("visible_apps", "objects", "ui_elements", "counts", "spatial_hints", "ambiguity", "limitations"):
        if not isinstance(base.get(key), list):
            base[key] = []
    for key in ("scene", "visible_text", "privacy", "visual_summary", "performance"):
        if not isinstance(base.get(key), dict):
            base[key] = {}
    if not isinstance(base.get("errors"), list):
        base["errors"] = []
    return base


def default_capture_metadata(*, image_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    image_metadata = dict(image_metadata or {})
    return {
        "captured_scope": "full_screen",
        "source": "automatic_screenshot",
        "window": None,
        "region": None,
        "image": image_metadata,
    }


def compact_screen_observation_for_prompt(observation: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(observation, dict):
        return {}
    request = observation.get("request") if isinstance(observation.get("request"), dict) else {}
    capture = observation.get("capture") if isinstance(observation.get("capture"), dict) else {}
    answer = observation.get("answer") if isinstance(observation.get("answer"), dict) else {}
    followup = observation.get("followup") if isinstance(observation.get("followup"), dict) else {}

    compact: dict[str, Any] = {
        "schema": observation.get("schema") or SCREEN_OBSERVATION_SCHEMA_VERSION,
        "schema_version": observation.get("schema_version") or SCREEN_OBSERVATION_SCHEMA_VERSION,
        "type": observation.get("type") or SCREEN_OBSERVATION_TYPE,
        "source": observation.get("source") or "screen",
        "request": {
            "user_question": _compact_text(str(request.get("user_question") or ""), 240),
            "question_type": request.get("question_type") or "",
            "target": request.get("target") or "",
        },
        "capture": {
            "captured_scope": capture.get("captured_scope"),
            "source": capture.get("source"),
            "region": _compact_region(capture.get("region")),
        },
        "answer": {
            "direct_answer": _compact_text(str(answer.get("direct_answer") or ""), 420),
            "confidence": _safe_float(answer.get("confidence"), 0.0),
        },
        "followup": {
            "context_for_next_turn": _compact_text(str(followup.get("context_for_next_turn") or ""), 520),
            "needs_followup_capture": bool(followup.get("needs_followup_capture", False)),
            "suggested_capture": followup.get("suggested_capture"),
        },
        "limitations": _compact_list(observation.get("limitations"), 5, 180),
    }

    visual_summary = observation.get("visual_summary") if isinstance(observation.get("visual_summary"), dict) else {}
    visual_text = _compact_text(str(visual_summary.get("text") or ""), 520)
    if visual_text:
        compact["visual_summary"] = {
            "engine": visual_summary.get("engine") or "moondream",
            "model": visual_summary.get("model") or "",
            "revision": visual_summary.get("revision") or "",
            "text": visual_text,
        }

    errors = _normalize_errors(observation.get("errors"))
    if errors:
        compact["errors"] = _compact_list(errors, 5, 220)

    for key in ("diagnosis", "identification", "counts", "game"):
        value = observation.get(key)
        if _has_content(value):
            compact[key] = _compact_value(value, max_chars=900)

    ambiguity = observation.get("ambiguity")
    if _has_content(ambiguity):
        compact["ambiguity"] = _compact_value(ambiguity, max_chars=420)
    return compact


def screen_observation_context_for_next_turn(observation: dict[str, Any] | None) -> str:
    if not isinstance(observation, dict):
        return ""
    followup = observation.get("followup") if isinstance(observation.get("followup"), dict) else {}
    answer = observation.get("answer") if isinstance(observation.get("answer"), dict) else {}
    context = str(followup.get("context_for_next_turn") or "").strip()
    if not context:
        context = str(answer.get("direct_answer") or "").strip()
    if not context:
        visual_summary = observation.get("visual_summary") if isinstance(observation.get("visual_summary"), dict) else {}
        context = str(visual_summary.get("text") or "").strip()
    if not context:
        return ""
    request = observation.get("request") if isinstance(observation.get("request"), dict) else {}
    capture = observation.get("capture") if isinstance(observation.get("capture"), dict) else {}
    target = str(request.get("target") or capture.get("captured_scope") or "").strip()
    source = str(capture.get("source") or "").strip()
    prefix = " / ".join(part for part in (target, source) if part)
    # Staleness self-identification (stale-frame fix, plan d-a): by the time this
    # reaches the NEXT turn it is by definition a previous-turn snapshot -- say so,
    # so the LLM answers follow-ups about THAT view from it, but re-captures when
    # asked about the CURRENT screen. The observation itself stays in context
    # (特判一 "follow-ups don't forget" intact -- it only self-identifies as old).
    stale_note = "[上一轮查看的画面，非当前画面] "
    if prefix:
        return _compact_text(f"{stale_note}{prefix}: {context}", 520)
    return _compact_text(f"{stale_note}{context}", 520)


def _deep_update(base: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            _deep_update(base[key], value)
        else:
            base[key] = deepcopy(value)


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_capture_payload(value: dict[str, Any] | None, *, target: str) -> dict[str, Any]:
    capture = dict(value or {})
    image = capture.get("image") if isinstance(capture.get("image"), dict) else {}
    mode = str(capture.get("mode") or capture.get("captured_scope") or target or "full_screen")
    image_format = str(
        capture.get("image_format")
        or capture.get("format")
        or image.get("format")
        or _format_from_mime(capture.get("mime_type"))
        or "png"
    ).lower()

    capture["mode"] = mode
    capture["width"] = _safe_int(capture.get("width"), _resolution_value(capture, image, "width"))
    capture["height"] = _safe_int(capture.get("height"), _resolution_value(capture, image, "height"))
    capture["created_at"] = str(capture.get("created_at") or capture.get("captured_at") or _utc_now())
    capture["image_format"] = image_format
    capture.setdefault("captured_scope", mode)
    capture.setdefault("source", "automatic_screenshot" if mode == "full_screen" else "manual_region_selection")
    return capture


def _normalize_visual_summary(value: dict[str, Any] | str | None) -> dict[str, Any]:
    if isinstance(value, str):
        value = {"text": value}
    summary = dict(value or {})
    return {
        "engine": str(summary.get("engine") or "moondream"),
        "model": str(summary.get("model") or "vikhyatk/moondream2"),
        "revision": str(summary.get("revision") or "2025-06-21"),
        "text": str(summary.get("text") or "").strip(),
    }


def _normalize_visible_text(value: dict[str, Any] | None) -> dict[str, Any]:
    visible = dict(value or {})
    blocks = visible.get("blocks")
    return {
        **visible,
        "engine": str(visible.get("engine") or "rapidocr"),
        "raw_text": str(visible.get("raw_text") or visible.get("raw") or visible.get("text") or ""),
        "blocks": blocks if isinstance(blocks, list) else [],
    }


def _normalize_performance(value: dict[str, Any] | None) -> dict[str, float]:
    perf = dict(value or {})
    return {
        "capture_ms": round(_safe_float(perf.get("capture_ms"), 0.0), 3),
        "ocr_ms": round(_safe_float(perf.get("ocr_ms"), 0.0), 3),
        "moondream_ms": round(_safe_float(perf.get("moondream_ms"), 0.0), 3),
        "total_ms": round(_safe_float(perf.get("total_ms"), 0.0), 3),
    }


def _normalize_errors(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    raw_errors = value if isinstance(value, list) else [value]
    errors: list[dict[str, Any]] = []
    for item in raw_errors:
        if isinstance(item, dict):
            errors.append(
                {
                    "stage": str(item.get("stage") or ""),
                    "code": str(item.get("code") or ""),
                    "message": str(item.get("message") or item.get("error") or ""),
                    "recoverable": bool(item.get("recoverable", True)),
                }
            )
        else:
            errors.append({"stage": "", "code": "", "message": str(item), "recoverable": True})
    return [error for error in errors if error["message"] or error["code"]]


def _normalize_answer(value: dict[str, Any] | str | None) -> dict[str, Any]:
    if isinstance(value, str):
        value = {"direct_answer": value}
    answer = dict(value or {})
    confidence = _safe_float(answer.get("confidence"), default=0.0)
    return {
        **answer,
        "direct_answer": str(answer.get("direct_answer") or "").strip(),
        "confidence": max(0.0, min(1.0, confidence)),
    }


def _normalize_followup(value: dict[str, Any] | None) -> dict[str, Any]:
    followup = dict(value or {})
    return {
        "context_for_next_turn": str(followup.get("context_for_next_turn") or ""),
        "needs_followup_capture": bool(followup.get("needs_followup_capture", False)),
        "suggested_capture": followup.get("suggested_capture"),
    }


def _resolution_value(capture: dict[str, Any], image: dict[str, Any], key: str) -> int:
    for container in (
        capture.get("sent_resolution"),
        capture.get("original_resolution"),
        image.get("sent_resolution"),
        image.get("original_resolution"),
        image,
    ):
        if isinstance(container, dict):
            parsed = _safe_int(container.get(key), 0)
            if parsed:
                return parsed
    return 0


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _format_from_mime(value: Any) -> str:
    text = str(value or "").lower()
    if "/" not in text:
        return ""
    return text.rsplit("/", 1)[-1]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compact_text(text: str, max_chars: int) -> str:
    compact = " ".join((text or "").split())
    if max_chars <= 0 or len(compact) <= max_chars:
        return compact
    return f"{compact[:max_chars].rstrip()}..."


def _compact_region(value: Any) -> Any:
    if not isinstance(value, dict):
        return None
    region: dict[str, Any] = {}
    for key in ("screen_name", "screen_index", "logical", "physical", "device_pixel_ratio"):
        if key in value:
            region[key] = value[key]
    return region or None


def _compact_list(value: Any, max_items: int, item_chars: int) -> list[Any]:
    if not isinstance(value, list):
        return []
    return [_compact_value(item, max_chars=item_chars) for item in value[:max_items]]


def _compact_value(value: Any, max_chars: int) -> Any:
    if isinstance(value, str):
        return _compact_text(value, max_chars)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"visible_text", "raw_text", "ocr", "full_text", "image_bytes"}:
                continue
            result[key] = _compact_value(item, max_chars=max(80, max_chars // 2))
        return result
    if isinstance(value, list):
        return [_compact_value(item, max_chars=max(80, max_chars // 3)) for item in value[:8]]
    return value


def _has_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, list, dict)):
        return bool(value)
    return True
