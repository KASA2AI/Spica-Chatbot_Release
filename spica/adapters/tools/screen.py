"""inspect_screen as a ToolPort (C7).

The auto "look at the screen" capability: the model decides it needs to look, so it
is a TOOL (unlike a manual screenshot, which is an attachment the user already
framed). C7 makes it the first real ``ToolPort`` in the CapabilityRegistry.

``run`` returns the ``screen_observation.v1`` dict (per ToolPort); the RegistryToolSet
wraps it into the legacy ``tool_success`` string, so the LLM tool round is
byte-identical. Behaviour is unchanged from the legacy ``inspect_screen`` function
-- same intent gate, same capture, same analysis engine (now via ScreenAnalysisPort).

INVARIANTS:
- N0: the ``is_screen_intent_explicit`` gate is re-checked defensively here (the
  ToolSet gates schema selection; the tool re-validates so a forced/mis-registered
  call still cannot bypass it), and analysis is local-only -- never uploaded.

Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from time import perf_counter
from typing import Any

from agent_tools.function_tools import is_screen_intent_explicit
from agent_tools.function_tools.screen.capture import capture_full_screen
from agent_tools.function_tools.screen.config import resolve_effective_screen_config
from agent_tools.function_tools.screen.schema import ScreenToolError
from agent_tools.function_tools.screen.tool import (
    INSPECT_SCREEN_SCHEMA,
    _capture_metadata_for_observation,
    classify_screen_question,
)
from spica.ports.screen import ScreenAnalysisPort


class InspectScreenTool:
    """``ToolPort`` for ``inspect_screen``, analyzing via a ``ScreenAnalysisPort``."""

    name = "inspect_screen"

    def __init__(self, screen: ScreenAnalysisPort, config: Any | None = None) -> None:
        self._screen = screen
        # P0b 2a: production injects the host-resolved ScreenPipelineConfig;
        # None (bare/demo construction) falls back to load_screen_config().
        self._config = config

    def schema(self) -> dict[str, Any]:
        return INSPECT_SCREEN_SCHEMA

    def run(self, *, target: str = "full_screen", question: str = "") -> dict[str, Any]:
        target = (target or "full_screen").strip()
        question = (question or "").strip()
        # Defensive re-validation (N0): the ToolSet already intent-gates schema
        # selection, but the tool re-checks so a forced call cannot bypass it.
        if target != "full_screen":
            raise ScreenToolError("SCREEN_INTENT_NOT_EXPLICIT", "第一阶段只支持 target=full_screen。")
        if not is_screen_intent_explicit(question):
            raise ScreenToolError(
                "SCREEN_INTENT_NOT_EXPLICIT",
                "inspect_screen 只能在用户明确要求查看屏幕、桌面、显示器或当前画面时调用。",
            )
        try:
            # P0b 3 (D-3c): the bare-construction fallback follows the carrier
            # switch too, so every path tracks the same effective chain.
            config = self._config or resolve_effective_screen_config()
            # getattr: the real ScreenPipelineConfig always carries ``enabled``;
            # the fallback only spares duck-typed bare/demo configs (test fakes).
            if not getattr(config, "enabled", True):
                # Hard gate BEFORE any capture (same defensive re-validation
                # discipline as the N0 intent check above): supply filtering via
                # ``available`` is not an execution gate -- a forced call must
                # take zero screenshots. analyzer.py keeps the same check as the
                # last line of defence.
                raise ScreenToolError("SCREEN_DISABLED", "本地 screen pipeline 已禁用。")
            started = perf_counter()
            capture = capture_full_screen()  # local capture, never uploaded (N0)
            capture_ms = round((perf_counter() - started) * 1000.0, 3)
            capture_metadata = _capture_metadata_for_observation(
                capture.image, capture.metadata, config.capture_format
            )
            return self._screen.analyze_image(
                capture.image,
                target,
                question,
                config=config,
                capture=capture_metadata,
                performance={"capture_ms": capture_ms},
                question_type=classify_screen_question(question),
            )
        except ScreenToolError:
            raise
        except Exception as exc:  # noqa: BLE001 -- preserve the legacy error code/message
            raise ScreenToolError("SCREEN_ANALYSIS_FAILED", f"本地屏幕分析失败：{exc}") from exc
