"""Screen-analysis capability port (C7).

The local Moondream + OCR screen-analysis engine as a capability port. Both the
``inspect_screen`` tool (auto: the model decided it needs to look) and the manual
screen-attachment stage (the user already framed the shot) call this SAME engine.
C7 formalizes the already-shared ``analyze_screen_image_local`` as
``ScreenAnalysisPort`` so there is ONE analysis adapter behind both entry points.

INVARIANT (N0): analysis is LOCAL ONLY -- the screenshot is never uploaded.

Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ScreenAnalysisPort(Protocol):
    """The local screen-analysis engine. Mirrors ``analyze_screen_image_local``."""

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
        """Analyze a captured screen image -> a ``screen_observation.v1`` dict."""
        ...
