from __future__ import annotations

from copy import deepcopy
from typing import Any


SCREEN_OBSERVATION_SCHEMA_VERSION = "screen_observation.v1"
SCREEN_OBSERVATION_TYPE = "screen_observation"


class ScreenToolError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def empty_screen_observation(
    *,
    user_question: str,
    question_type: str,
    target: str,
    capture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCREEN_OBSERVATION_SCHEMA_VERSION,
        "type": SCREEN_OBSERVATION_TYPE,
        "request": {
            "user_question": user_question or "",
            "question_type": question_type or "general_observation",
            "target": target or "full_screen",
        },
        "capture": capture or {},
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
    base["type"] = SCREEN_OBSERVATION_TYPE
    base["request"] = {
        **base.get("request", {}),
        "user_question": user_question or "",
        "question_type": question_type or "general_observation",
        "target": target or "full_screen",
    }
    base["capture"] = {**(base.get("capture") or {}), **capture}

    answer = base.get("answer") if isinstance(base.get("answer"), dict) else {}
    direct_answer = str(answer.get("direct_answer") or "").strip()
    confidence = _safe_float(answer.get("confidence"), default=0.0)
    base["answer"] = {
        **answer,
        "direct_answer": direct_answer,
        "confidence": max(0.0, min(1.0, confidence)),
    }

    followup = base.get("followup") if isinstance(base.get("followup"), dict) else {}
    base["followup"] = {
        "context_for_next_turn": str(followup.get("context_for_next_turn") or ""),
        "needs_followup_capture": bool(followup.get("needs_followup_capture", False)),
        "suggested_capture": followup.get("suggested_capture"),
    }

    for key in ("visible_apps", "objects", "ui_elements", "counts", "spatial_hints", "ambiguity", "limitations"):
        if not isinstance(base.get(key), list):
            base[key] = []
    for key in ("scene", "visible_text", "privacy"):
        if not isinstance(base.get(key), dict):
            base[key] = {}
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
        "schema_version": observation.get("schema_version") or SCREEN_OBSERVATION_SCHEMA_VERSION,
        "type": observation.get("type") or SCREEN_OBSERVATION_TYPE,
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
        return ""
    request = observation.get("request") if isinstance(observation.get("request"), dict) else {}
    capture = observation.get("capture") if isinstance(observation.get("capture"), dict) else {}
    target = str(request.get("target") or capture.get("captured_scope") or "").strip()
    source = str(capture.get("source") or "").strip()
    prefix = " / ".join(part for part in (target, source) if part)
    if prefix:
        return _compact_text(f"{prefix}: {context}", 520)
    return _compact_text(context, 520)


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
