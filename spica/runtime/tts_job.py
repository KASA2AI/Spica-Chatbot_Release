"""Per-unit TTS synthesis job (Phase 6C).

Moved verbatim from agent/streaming_pipeline.py. Runs on the streaming TTS
executor: synthesizes audio for one play unit via the TTS port, emitting
``unit_audio_started`` / ``unit_audio_ready`` events.

``ctx`` (TurnContext) / ``services`` are typed ``Any`` to avoid a spica -> agent
import. Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from typing import Any

from common.timing import elapsed_ms, now_ms
from agent_tools.tts.schemas import TTSRequest


def synthesize_unit_audio(
    services: Any,
    ctx: Any,
    unit: dict[str, Any],
    request_start_ms: float,
    set_timing_once: Any,
    put_unit_event: Any,
) -> dict[str, Any]:
    unit_timing = unit["timing"]
    unit_index = int(unit["index"])
    tts_start_ms = now_ms()
    tts_start_relative_ms = round(tts_start_ms - request_start_ms, 2)
    unit_timing["tts_start_ms"] = tts_start_relative_ms
    if unit_index == 0:
        set_timing_once("first_tts_start_ms", tts_start_relative_ms)
    put_unit_event(
        "unit_audio_started",
        {
            "index": unit_index,
            "tts_text": unit["tts_text"],
            "emotion": unit["emotion"],
            "timing": {
                "tts_start_ms": tts_start_relative_ms,
            },
        },
    )
    audio_payload: dict[str, Any] = {
        "audio_url": None,
        "audio_path": None,
        "audio_error": None,
        "tts_result": None,
        "duration_ms": None,
    }
    try:
        if services.tts_adapter is None:
            raise RuntimeError("TTS adapter is not configured")
        result = services.tts_adapter.synthesize(
            TTSRequest(
                text=unit["tts_text"],
                emotion=unit["emotion"],
                extra={"tts_param_overrides": ctx.request.tts_param_overrides or {}},
            )
        )
        if not result.ok:
            raise RuntimeError(result.error or "TTS synthesis failed")
        duration_ms = result.duration_ms
        if not isinstance(duration_ms, (int, float)):
            duration_ms = result.timing.get("tts_total_ms")
        if not isinstance(duration_ms, (int, float)):
            duration_ms = elapsed_ms(tts_start_ms)
        unit_timing["tts_duration_ms"] = duration_ms
        audio_payload = {
            "audio_url": result.audio_url,
            "audio_path": result.audio_path,
            "audio_error": None,
            "tts_result": result,
            "duration_ms": duration_ms,
        }
    except Exception as exc:
        duration_ms = elapsed_ms(tts_start_ms)
        unit_timing["tts_duration_ms"] = duration_ms
        unit_timing["tts_error"] = str(exc)
        audio_payload = {
            "audio_url": None,
            "audio_path": None,
            "audio_error": str(exc),
            "tts_result": None,
            "duration_ms": duration_ms,
        }
    finally:
        tts_done_relative_ms = round(now_ms() - request_start_ms, 2)
        unit_timing["tts_done_ms"] = tts_done_relative_ms
        if unit_index == 0:
            set_timing_once("first_tts_done_ms", tts_done_relative_ms)
            set_timing_once("first_audio_ready_ms", tts_done_relative_ms)
        put_unit_event(
            "unit_audio_ready",
            {
                "index": unit_index,
                "audio_url": audio_payload.get("audio_url"),
                "audio_path": audio_payload.get("audio_path"),
                "audio_error": audio_payload.get("audio_error"),
                "timing": {
                    "tts_ms": unit_timing.get("tts_duration_ms"),
                    "tts_start_ms": unit_timing.get("tts_start_ms"),
                    "tts_done_ms": unit_timing.get("tts_done_ms"),
                },
            },
        )
    return audio_payload
