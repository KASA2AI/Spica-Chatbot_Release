from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class TTSRequest:
    text: str
    emotion: str = "neutral"
    voice: str | None = None
    language: str = "ja"
    speed: float = 1.0
    output_format: Literal["wav", "mp3", "pcm"] = "wav"
    output_mode: Literal["file", "bytes", "stream"] = "file"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class TTSResult:
    ok: bool
    provider: str
    audio_path: str | None = None
    audio_url: str | None = None
    sample_rate: int | None = None
    duration_ms: float | None = None
    mime_type: str = "audio/wav"
    chunks: list[dict[str, Any]] = field(default_factory=list)
    timing: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
