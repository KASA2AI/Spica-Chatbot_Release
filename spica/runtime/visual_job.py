"""Per-unit visual (sprite) selection job (Phase 6C).

Moved verbatim from agent/streaming_pipeline.py. Runs on the streaming visual
executor: builds the visual payload for one play unit via the visual port and
emits a ``unit_visual_ready`` event. Selection stays fully local to the port.

``state`` / ``services`` are typed ``Any`` to avoid a spica -> agent import.
Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from typing import Any

from common.timing import elapsed_ms, log_timing, now_ms


def build_unit_visual_and_emit(
    services: Any,
    state: Any,
    unit: dict[str, Any],
    request_start_ms: float,
    set_timing_once: Any,
    put_unit_event: Any,
) -> dict[str, Any]:
    visual = _build_unit_visual(services, state, unit, request_start_ms, set_timing_once)
    unit_timing = unit["timing"]
    unit_index = int(unit["index"])
    visual_ready_ms = round(now_ms() - request_start_ms, 2)
    unit_timing["visual_ready_ms"] = visual_ready_ms
    put_unit_event(
        "unit_visual_ready",
        {
            "index": unit_index,
            "visual": visual,
            "cue": _cue_from_visual_payload(visual),
            "visual_error": visual.get("selection_error"),
            "timing": {
                "visual_ms": unit_timing.get("visual_classifier_duration_ms"),
                "visual_ready_ms": visual_ready_ms,
            },
        },
    )
    return visual


def _build_unit_visual(
    services: Any,
    state: Any,
    unit: dict[str, Any],
    request_start_ms: float,
    set_timing_once: Any,
) -> dict[str, Any]:
    unit_timing = unit["timing"]
    unit_index = int(unit["index"])
    classifier_start_abs = now_ms()
    unit_timing["visual_classifier_start_ms"] = round(classifier_start_abs - request_start_ms, 2)
    try:
        if services.visual_tool is None:
            raise RuntimeError("visual tool is not configured")
        payload = services.visual_tool.build_unit_visual_payload(
            current_unit_text=unit["display_text"],
            emotion=unit["emotion"],
            unit_index=unit_index,
            previous_units=unit["previous_units"],
            full_answer_so_far=unit["full_answer_so_far"],
            runtime_context=state.metadata.get("stream_visual_context"),
            requested_costume=state.visual_overrides.get("costume_set"),
            requested_mode=state.visual_overrides.get("costume_mode"),
        )
        classifier = payload.get("classifier") if isinstance(payload.get("classifier"), dict) else {}
        duration_ms = classifier.get("duration_ms")
        if not isinstance(duration_ms, (int, float)):
            duration_ms = elapsed_ms(classifier_start_abs)
        cue = payload.get("cue") if isinstance(payload.get("cue"), dict) else {}
        visual = {
            "expression_id": cue.get("expression_id"),
            "hand_pose": cue.get("hand_pose"),
            "image_url": cue.get("image_url"),
            "image_path": cue.get("image_path"),
            "reason": cue.get("reason"),
            "selection_source": payload.get("selection_source") or "local_vote_classifier",
            "classifier_version": payload.get("classifier_version"),
            "duration_ms": duration_ms,
            "confidence": classifier.get("confidence"),
            "signals": classifier.get("signals", []),
            "selection_error": payload.get("selection_error"),
            "costume": payload.get("costume"),
            "costume_mode": payload.get("costume_mode"),
            "background_url": payload.get("background_url"),
            "dialog": payload.get("dialog"),
            "character": payload.get("character"),
            "cue": cue,
            "cues": payload.get("cues") if isinstance(payload.get("cues"), list) else [cue],
        }
        unit_timing["visual_classifier_duration_ms"] = duration_ms
        unit_timing["visual_classifier_version"] = visual["classifier_version"]
        unit_timing["visual_selection_source"] = visual["selection_source"]
        unit_timing["visual_selection_error"] = visual["selection_error"]
        log_timing(
            "visual_classifier_unit",
            duration_ms,
            unit_index=unit_index,
            chars=len(unit["display_text"]),
            version=visual["classifier_version"],
            source=visual["selection_source"],
            error=visual["selection_error"],
        )
        return visual
    except Exception as exc:
        duration_ms = elapsed_ms(classifier_start_abs)
        unit_timing["visual_classifier_duration_ms"] = duration_ms
        unit_timing["visual_classifier_version"] = None
        unit_timing["visual_selection_source"] = "visual_error"
        unit_timing["visual_selection_error"] = str(exc)
        return {
            "expression_id": None,
            "hand_pose": None,
            "image_url": None,
            "reason": "visual classifier failed",
            "selection_source": "visual_error",
            "classifier_version": None,
            "duration_ms": duration_ms,
            "selection_error": str(exc),
        }
    finally:
        if unit_index == 0:
            set_timing_once("first_visual_ready_ms", round(now_ms() - request_start_ms, 2))


def _cue_from_visual_payload(visual: dict[str, Any]) -> dict[str, Any]:
    cue = visual.get("cue") if isinstance(visual.get("cue"), dict) else {}
    if cue:
        return cue
    cues = visual.get("cues") if isinstance(visual.get("cues"), list) else []
    if cues and isinstance(cues[0], dict):
        return cues[0]
    return {}
