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
from spica.runtime.context import is_turn_cancelled


def synthesize_unit_audio(
    tts: Any,
    ctx: Any,
    unit: dict[str, Any],
    request_start_ms: float,
    observer: Any,
    put_unit_event: Any,
) -> dict[str, Any]:
    cancellation = getattr(ctx.request, 'audio_cancelled', None)
    def audio_revoked():
        return is_turn_cancelled(ctx.request) or cancellation is not None and cancellation.is_set()
    if not ctx.request.want_audio or audio_revoked():
        return {
            "audio_url": None,
            "audio_path": None,
            "audio_error": None,
            "tts_result": None,
            "duration_ms": None,
        }
    unit_timing = unit["timing"]
    unit_index = int(unit["index"])
    tts_start_ms = now_ms()
    tts_start_relative_ms = round(tts_start_ms - request_start_ms, 2)
    unit_timing["tts_start_ms"] = tts_start_relative_ms
    if unit_index == 0:
        observer.mark_once("first_tts_start_ms", tts_start_relative_ms)
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
        if tts is None:
            raise RuntimeError("TTS adapter is not configured")
        result = tts.synthesize(
            TTSRequest(
                text=unit["tts_text"],
                emotion=unit["emotion"],
                extra={"tts_param_overrides": ctx.request.tts_param_overrides or {}},
                cancelled=cancellation,
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
        if audio_revoked():
            audio_payload = dict(audio_url=None, audio_path=None, audio_error=None,
                                 tts_result=None, duration_ms=None)
        tts_done_relative_ms = round(now_ms() - request_start_ms, 2)
        unit_timing["tts_done_ms"] = tts_done_relative_ms
        if unit_index == 0:
            observer.mark_once("first_tts_done_ms", tts_done_relative_ms)
            observer.mark_once("first_audio_ready_ms", tts_done_relative_ms)
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


def local_alarm_events(cue, cancelled):
    """One fixed packaged cue through the normal presentation/receipt owner."""
    from pathlib import Path
    from spica.core.events import UnitReadyEvent, DoneEvent
    if cue != 'home_wake':
        raise ValueError('unsupported local audio cue')
    if cancelled.is_set():
        return
    path = Path(__file__).with_name('assets') / 'home_wake.wav'
    if not path.is_file():
        raise FileNotFoundError('本地闹钟铃声资源缺失')
    yield UnitReadyEvent(index=0, display_text='叫醒时间到了。角色语音暂不可用，正在播放本地铃声。',
        tts_text='', emotion='neutral', visual={}, audio_path=str(path), audio_url=None)
    yield DoneEvent(answer='叫醒时间到了。', emotion='neutral', emotion_label='', emotion_reason='', units_count=1)
