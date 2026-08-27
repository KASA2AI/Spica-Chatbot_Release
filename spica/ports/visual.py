"""Visual (sprite/立绘) capability port (Phase 5).

Matches the call surface the pipeline already uses on ``VisualDiffService``:
a full-answer payload (sync path), a per-unit payload (streaming path), and a
per-request stream context. Selection stays fully local to the adapter.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class VisualPort(Protocol):
    def build_visual_payload(
        self,
        answer: str,
        emotion: str,
        requested_costume: str | None = None,
        requested_mode: str | None = None,
    ) -> dict[str, Any]:
        ...

    def prepare_stream_context(
        self,
        requested_costume: str | None = None,
        requested_mode: str | None = None,
    ) -> dict[str, Any]:
        ...

    def build_unit_visual_payload(self, **kwargs: Any) -> dict[str, Any]:
        ...
