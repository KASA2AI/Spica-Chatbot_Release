"""Spica sprite-diff visual adapter (Phase 5).

``VisualDiffService`` already satisfies ``VisualPort`` structurally
(build_visual_payload / build_unit_visual_payload / prepare_stream_context), so
this is just a named factory for registry resolution. Selection remains fully
local (no model call).
"""

from __future__ import annotations

from typing import Any

from agent_tools.visual import VisualDiffService


def build_spica_visual(config_path: Any = None, **_kwargs: Any) -> VisualDiffService:
    # Phase 7b: use the active character package's visual config when provided;
    # otherwise the engine default (Spica behaviour unchanged).
    if config_path:
        return VisualDiffService(config_path=config_path)
    return VisualDiffService()
