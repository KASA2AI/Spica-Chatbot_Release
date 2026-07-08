from __future__ import annotations

from typing import Any

from agent_tools.tts.schemas import TTSRequest, TTSResult


class CurrentGPTSoVITSAdapter:
    name = "gptsovits_current"

    def __init__(self, service: Any | None = None, config_path: str | None = None):
        if service is None:
            from agent_tools.tts.gptsovits import GPTSoVITSTool

            service = GPTSoVITSTool(config_path=config_path) if config_path else GPTSoVITSTool()
        self.service = service

    def synthesize(self, request: TTSRequest) -> TTSResult:
        try:
            raw = self.service.synthesize(
                text=request.text,
                emotion=request.emotion,
                tts_param_overrides=self._tts_param_overrides(request),
            )
            if not isinstance(raw, dict):
                raise TypeError(f"GPT-SoVITS returned unsupported result type: {type(raw).__name__}")

            timing = raw.get("timing") if isinstance(raw.get("timing"), dict) else {}
            duration_ms = timing.get("tts_total_ms")
            if not isinstance(duration_ms, (int, float)):
                duration_ms = raw.get("duration_ms")
            if not isinstance(duration_ms, (int, float)):
                duration_ms = None

            return TTSResult(
                ok=bool(raw.get("ok", True)),
                provider=self.name,
                audio_path=raw.get("audio_path"),
                audio_url=raw.get("audio_url"),
                sample_rate=raw.get("sampling_rate") or raw.get("sample_rate"),
                duration_ms=duration_ms,
                mime_type=str(raw.get("mime_type") or "audio/wav"),
                chunks=self._chunks(raw),
                timing=timing,
                metadata=raw,
                error=raw.get("error"),
            )
        except Exception as exc:
            return TTSResult(
                ok=False,
                provider=self.name,
                error=str(exc),
            )

    def warmup(self, *args: Any, **kwargs: Any) -> Any:
        warmup = getattr(self.service, "warmup", None)
        if warmup is None:
            raise AttributeError("Wrapped TTS service does not provide warmup")
        return warmup(*args, **kwargs)

    def public_config(self) -> Any:
        public_config = getattr(self.service, "public_config", None)
        if public_config is None:
            raise AttributeError("Wrapped TTS service does not provide public_config")
        return public_config()

    def _tts_param_overrides(self, request: TTSRequest) -> dict[str, Any] | None:
        overrides: dict[str, Any] = {}
        legacy_overrides = request.extra.get("tts_param_overrides")
        if isinstance(legacy_overrides, dict):
            overrides.update(legacy_overrides)
        provider_overrides = request.extra.get(self.name)
        if isinstance(provider_overrides, dict):
            overrides.update(provider_overrides)
        if request.speed != 1.0:
            overrides["speed"] = request.speed
        return overrides or None

    def _chunks(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        chunk_audio = raw.get("tts_chunk_audio")
        if isinstance(chunk_audio, list):
            return [dict(item) for item in chunk_audio if isinstance(item, dict)]

        tts_chunks = raw.get("tts_chunks")
        if isinstance(tts_chunks, list):
            return [
                {"index": index, "text": str(text)}
                for index, text in enumerate(tts_chunks)
            ]

        return []
