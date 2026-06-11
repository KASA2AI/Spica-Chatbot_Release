"""watch_game_screen: look at the BOUND game window during companion play (Phase 9).

A thin shell over already-verified parts -- it invents no analysis and no capture:

- analysis: the same ``ScreenAnalysisPort`` instance family as ``inspect_screen``
  (Moondream manager is a process-global singleton -- no second model load);
- capture: ``capture_window_image`` below is the GENERIC "capture a given window"
  form (window_id is a parameter; the companion's bound window is just one caller)
  over the galgame locator/capture ports the OCR loop already uses.

Companion awareness comes from an injected LAZY provider (the play-history-bridge
closure shape): the host wires it to read the companion controller's published
watch target at RUN time (adapters only exist after ``initialize()``). Not
playing -> ``NO_ACTIVE_COMPANION`` tool error; the LLM answers accordingly. There
is deliberately NO full-screen fallback (principle: only the bound game window)
and NO keyword re-gate here -- the active-companion precondition is the strong
gate; question wording during play is too varied for a wordlist to re-judge.

Qt-free (CLAUDE.md #1); local-only analysis (N0), never uploaded.
"""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Any, Callable

from agent_tools.function_tools.screen.config import load_screen_config
from agent_tools.function_tools.screen.schema import ScreenToolError
from agent_tools.function_tools.screen.tool import (
    _capture_metadata_for_observation,
    classify_screen_question,
)
from spica.ports.screen import ScreenAnalysisPort

logger = logging.getLogger(__name__)

# (game_id, window_id, locator, capture) of the CURRENT companion play, or None.
WatchContextProvider = Callable[[], "tuple[str, str, Any, Any] | None"]

WATCH_GAME_SCREEN_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "watch_game_screen",
    "strict": True,
    "description": (
        "你正在陪用户玩 galgame。当用户想了解当前游戏画面上的任何内容——角色外观、立绘、"
        "选项框、界面文字、场景、正在发生的画面——时，调用此工具查看当前绑定的游戏窗口。"
        "例如：这个角色长什么样 / 这是谁 / 该选哪个 / 画面上写了什么 / 现在什么场面。"
        "用户询问“现在/当前/这个”画面、或对话表明游戏已推进/画面已变化时，必须重新调用"
        "本工具获取最新画面，不要依赖之前的观察结果；之前的观察只用于回答关于那一次画面"
        "的追问。只观察一次，只截游戏窗口（不截全屏），不点击、不控制游戏。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "用户想了解游戏画面的什么（如：这个角色是谁 / 该选哪个 / 这写的什么）。",
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    },
}


def capture_window_image(locator: Any, capture: Any, window_id: str) -> tuple[Any, dict[str, Any]]:
    """GENERIC window capture: the window's on-screen rect via the locator, grabbed
    via the screen-capture port. Raises ScreenToolError on an unavailable window."""
    geometry = locator.get_window_geometry(window_id)
    if geometry is None:
        raise ScreenToolError(
            "GAME_WINDOW_UNAVAILABLE", "无法获取游戏窗口几何（窗口可能已关闭或最小化）。"
        )
    captured = capture.capture_rect(geometry.x, geometry.y, geometry.width, geometry.height)
    metadata = {
        "captured_scope": "window",
        "source": "automatic_screenshot",
        "window": {"window_id": window_id},
        "region": {
            "left": int(geometry.x),
            "top": int(geometry.y),
            "width": int(geometry.width),
            "height": int(geometry.height),
        },
        "monitor": None,
    }
    return captured.image, metadata


class WatchGameScreenTool:
    """``ToolPort`` for ``watch_game_screen`` (registered like InspectScreenTool)."""

    name = "watch_game_screen"

    def __init__(
        self,
        screen: ScreenAnalysisPort,
        watch_context: WatchContextProvider,
        config: Any | None = None,
    ) -> None:
        self._screen = screen
        self._watch_context = watch_context
        # P0b 2a: production injects the host-resolved ScreenPipelineConfig;
        # None (bare/demo construction) falls back to load_screen_config().
        self._config = config

    def schema(self) -> dict[str, Any]:
        return WATCH_GAME_SCREEN_SCHEMA

    def run(self, *, question: str = "") -> dict[str, Any]:
        question = (question or "").strip()
        context = self._watch_context()
        if context is None:
            raise ScreenToolError(
                "NO_ACTIVE_COMPANION",
                "当前没有正在陪玩的游戏，无法查看游戏画面。要先开始陪玩并绑定游戏窗口。",
            )
        game_id, window_id, locator, capture = context
        # Diagnostic (stale-frame triage): the DECISIVE marker that the tool really
        # ran this turn -- absent => the LLM reused a previous observation instead.
        logger.info("watch_game_screen: capturing window_id=%s game_id=%s", window_id, game_id)
        try:
            config = self._config or load_screen_config()
            started = perf_counter()
            image, window_metadata = capture_window_image(locator, capture, window_id)
            capture_ms = round((perf_counter() - started) * 1000.0, 3)
            window_metadata["window"]["game_id"] = game_id
            capture_metadata = _capture_metadata_for_observation(
                image, window_metadata, config.capture_format
            )
            return self._screen.analyze_image(
                image,
                "game_window",
                question,
                config=config,
                capture=capture_metadata,
                performance={"capture_ms": capture_ms},
                question_type=classify_screen_question(question),
            )
        except ScreenToolError:
            raise
        except Exception as exc:  # noqa: BLE001 -- mirror InspectScreenTool's catch-all
            raise ScreenToolError("SCREEN_ANALYSIS_FAILED", f"游戏画面分析失败：{exc}") from exc
