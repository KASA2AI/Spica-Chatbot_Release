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

PRIVACY GATE (CLAUDE.md §4 "绝不误截其他应用"): the screen rect under a lost or
paused window may show ANOTHER application, so capture is allowed only in the
OCR-monitored visible states ({PLAYING, CHOICE_CHECKING, BACKGROUND_SUMMARIZING}
-- the loop is alive and runs its per-cycle safety check there). Any other state
refuses with ``GAME_WINDOW_NOT_SAFE`` BEFORE ``capture_rect`` is touched.

Qt-free (CLAUDE.md #1); local-only analysis (N0), never uploaded.
"""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Any, Callable

from agent_tools.function_tools.screen.config import resolve_effective_screen_config
from agent_tools.function_tools.screen.schema import ScreenToolError
from agent_tools.function_tools.screen.tool import (
    _capture_metadata_for_observation,
    classify_screen_question,
)
from spica.galgame.privacy_gate import PrivacyGate
from spica.galgame.session import WATCH_SAFE_STATES, GalgameState
from spica.ports.screen import ScreenAnalysisPort
from spica.runtime.window import WatchContext

logger = logging.getLogger(__name__)

# The CURRENT companion play's WatchContext (target + locator/capture handles +
# lock-free session-state snapshot), or None. Phase 8-c2: named carrier replaces
# the historical bare 5-tuple; state staleness stays <= one OCR cycle -- the
# same granularity the loop's own safety check detects occlusion with.
WatchContextProvider = Callable[[], "WatchContext | None"]

# The only states where the OCR loop is alive, runs _evaluate_safety every cycle
# and the window is monitored-visible. CHOICE_CHECKING is the tool's PRIMARY
# scenario ("该选哪个" happens during choice detection); BACKGROUND_SUMMARIZING is
# normal play with a summary running behind. Everything else refuses.
# D-P5-8: the set itself lives in session.py (WATCH_SAFE_STATES) so this gate and
# the P5 reaction gates share one named truth; semantics here are unchanged.
_WATCH_SAFE_STATES = WATCH_SAFE_STATES


def _window_not_safe_message(state: Any) -> str:
    if state is GalgameState.WINDOW_LOST:
        return (
            "游戏窗口现在不可见（被别的窗口挡住或最小化了），为了不误看别的东西，"
            "我现在不截屏。把游戏窗口调回前台就能看了。"
        )
    return "陪玩暂停中，我现在没在看游戏画面。继续陪玩之后再问我吧。"

WATCH_GAME_SCREEN_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "watch_game_screen",
    "strict": True,
    "description": (
        "你正在陪用户玩 galgame。游戏的剧情对话文字已由 OCR 实时识别并放进上下文了——"
        "[CURRENT_GAME_BUFFER] 是最近的对话原文，[CURRENT_LINE] 是当前屏上这一句，"
        "[RECENT_GAME_SUMMARIES] 和 [GAME_PROGRESS] 是剧情摘要与进度。这些文字信息直接读"
        "上下文回答，不要为它们调用本工具截屏。\n\n"
        "本工具只用于获取 OCR 拿不到的【视觉信息】，在以下两类情况才调用，查看当前绑定的游戏窗口：\n"
        "① OCR 没识别到的文字：选项没认出来或认错了；以及明显在对话框以外的界面元素文字"
        "（菜单、道具栏、状态栏、系统提示这类——剧情对话本身已在上下文，不在此列）；\n"
        "② 纯画面视觉：角色外观/立绘、表情神态、CG、场景画面、画面上正在发生的动作。\n\n"
        "该调用的例子：这个角色长什么样 / 她现在什么表情 / 这张 CG 画的是什么 / "
        "这个选项没识别出来该选哪个 / 屏幕上那个菜单（道具）写的是什么。\n"
        "不要调用、直接读上下文回答的例子：剧情到哪了 / 刚才说了什么（这些上下文里都有）。"
        "如果用户只是对剧情发表感想（例如「这个角色真可爱」），直接回应即可，不用截屏。\n"
        "当用户问的是当前屏上这一句的内容或含义：它通常已在 [CURRENT_LINE] 里，优先读它回答；"
        "只有上下文里确实找不到该内容时，才调用本工具看屏。\n\n"
        "（针对需要看屏的视觉信息）当用户询问「现在/当前/这个」的画面、角色或场景，或对话表明"
        "画面已变化时，必须重新调用本工具获取最新画面，不要依赖之前的观察结果；之前的观察只用于"
        "回答关于那一次画面的追问。只观察一次，只截游戏窗口（不截全屏），不点击、不控制游戏。"
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
        target, locator, capture, state = (
            context.target, context.locator, context.capture, context.state
        )
        game_id, window_id = target.game_id, target.window_id
        # Privacy gate (CLAUDE.md §4, Phase 8-c2: the PrivacyGate is the ONE
        # evaluator): watch purpose = state gate only (the historical asymmetry
        # -- no check_safety here), refusing BEFORE any capture.
        gate = PrivacyGate(locator, safe_states=_WATCH_SAFE_STATES)
        result = gate.evaluate(target, state, "watch")
        if not result.ok:
            logger.info(
                "watch_game_screen: refused, window not safe to capture (state=%s)",
                getattr(state, "value", state),
            )
            raise ScreenToolError(result.reason_code, _window_not_safe_message(state))
        # Diagnostic (stale-frame triage): the DECISIVE marker that the tool really
        # ran this turn -- absent => the LLM reused a previous observation instead.
        logger.info("watch_game_screen: capturing window_id=%s game_id=%s", window_id, game_id)
        try:
            # P0b 3 (D-3c): fallback follows the carrier switch (see screen.py).
            config = self._config or resolve_effective_screen_config()
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
