"""TTS capability port (Phase 5).

Folds in the existing ``agent_tools.tts.base.TTSAdapter`` shape: a synthesize call
taking a ``TTSRequest`` and returning a ``TTSResult``. Kept structural so current
adapters satisfy it without modification.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TTSPort(Protocol):
    name: str

    def synthesize(self, request: Any) -> Any:  # TTSRequest -> TTSResult
        ...
