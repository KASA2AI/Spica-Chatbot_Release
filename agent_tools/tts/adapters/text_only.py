from __future__ import annotations

from agent_tools.tts.schemas import TTSRequest, TTSResult


class TextOnlyTTSAdapter:
    """No-model TTS for tts.enabled=false: ok=True with no audio, zero VRAM.

    The streaming chain treats "ok result, audio_path/url None" as a unit with
    text but nothing to play (same UI path as an audio failure, minus the
    audio_error noise) -- subtitles keep streaming, playback pump advances.
    It deliberately exposes NO ``public_config``/``warmup``, so ``run_warmup``
    takes its "无需预热" branch while the warmup worker itself still runs
    (keeping STT warmup + dangling-session recovery alive; that lifecycle is
    skipped entirely only when tts_adapter is None -- see TtsConfig docstring).
    """

    name = "text_only"

    def synthesize(self, request: TTSRequest) -> TTSResult:
        return TTSResult(
            ok=True,
            provider=self.name,
            audio_path=None,
            audio_url=None,
            duration_ms=0.0,
            metadata={"text": request.text, "emotion": request.emotion, "text_only": True},
        )
