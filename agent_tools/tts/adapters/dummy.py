from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_tools.tts.schemas import TTSRequest, TTSResult


class DummyTTSAdapter:
    name = "dummy"

    def __init__(self, audio_path: str | Path | None = None, audio_url: str | None = None):
        self.audio_path = str(audio_path) if audio_path else None
        self.audio_url = audio_url

    def synthesize(self, request: TTSRequest) -> TTSResult:
        if not self.audio_path:
            return TTSResult(
                ok=False,
                provider=self.name,
                error="DummyTTSAdapter has no test audio configured.",
                metadata={"text": request.text, "emotion": request.emotion},
            )

        return TTSResult(
            ok=True,
            provider=self.name,
            audio_path=self.audio_path,
            audio_url=self.audio_url,
            chunks=[
                {
                    "index": 0,
                    "text": request.text,
                    "audio_path": self.audio_path,
                    "audio_url": self.audio_url,
                }
            ],
            metadata={"text": request.text, "emotion": request.emotion},
        )
