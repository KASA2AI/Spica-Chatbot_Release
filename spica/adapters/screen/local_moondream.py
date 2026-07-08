"""Local Moondream + OCR screen analysis (C7).

``ScreenAnalysisPort`` backed by ``analyze_screen_image_local`` -- the existing
local engine, unchanged. A thin pass-through: the inspect_screen tool and the
manual-attachment stage keep their own capture / decode / metadata logic and route
only the *analyze* call here, so this is a formalization (one adapter behind both
entry points), not a behaviour change.

INVARIANT (N0): local only -- the screenshot is never uploaded.

Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from typing import Any

from agent_tools.function_tools.screen.analyzer import analyze_screen_image_local


class LocalMoondreamScreenAnalysis:
    """``ScreenAnalysisPort`` over the local Moondream + OCR engine."""

    def analyze_image(
        self,
        image: Any,
        mode: str,
        prompt: str | None = None,
        *,
        config: Any = None,
        capture: dict[str, Any] | None = None,
        performance: dict[str, Any] | None = None,
        question_type: str | None = None,
    ) -> dict[str, Any]:
        return analyze_screen_image_local(
            image,
            mode,
            prompt,
            config=config,
            capture=capture,
            performance=performance,
            question_type=question_type,
        )
