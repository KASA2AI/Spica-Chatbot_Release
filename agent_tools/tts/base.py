from __future__ import annotations

from typing import Protocol

from .schemas import TTSRequest, TTSResult


class TTSAdapter(Protocol):
    name: str

    def synthesize(self, request: TTSRequest) -> TTSResult:
        ...
