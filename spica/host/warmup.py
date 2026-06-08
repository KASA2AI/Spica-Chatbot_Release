"""Startup warmup (Phase 6E).

The host orchestrates startup warmup (LLM ready + TTS model warmup) so the UI
just runs it on a background thread and maps progress stages to its loading UI.
Pulling the logic out of ``AppHost`` keeps the host thin (CLAUDE.md #6): it owns
only the ``warmup`` forwarding method, while the procedure lives here as a plain
function over the surfaces it actually uses.

INVARIANT (CLAUDE.md #1): Qt-free.
"""

from __future__ import annotations

from typing import Any, Callable


def run_warmup(
    surface: Any,
    tts_adapter: Any,
    on_progress: Callable[[str, str], None],
) -> None:
    """Run startup warmup, reporting progress as ``on_progress(stage, message)``
    where stage is ``"initializing" | "ready" | "error"``.

    ``surface`` is the conversation surface (for the LLM model name); ``tts_adapter``
    is the active TTS adapter (for model warmup). Formerly lived in the UI's
    StartupWarmupWorker, then on AppHost.warmup; the host now forwards here.
    """
    tts = tts_adapter
    try:
        model = str(getattr(surface, "model", "") or "unknown")
        on_progress("initializing", f"LLM API 初始化完成：{model}")
        public_config = getattr(tts, "public_config", None)
        warmup = getattr(tts, "warmup", None)
        provider_name = str(getattr(tts, "name", None) or "TTS")
        if public_config is None or warmup is None:
            on_progress("ready", f"LLM API 已初始化，{provider_name} 无需启动预热。")
            return

        config = public_config()
        if not bool(config.get("warmup_on_startup", True)):
            on_progress("ready", f"LLM API 已初始化，{provider_name} 启动预热已关闭。")
            return

        configured_emotions = config.get("warmup_emotions")
        if isinstance(configured_emotions, list) and configured_emotions:
            emotions = [str(item) for item in configured_emotions if str(item).strip()]
        else:
            emotions = [str(config.get("warmup_emotion") or "happy")]
        if not emotions:
            emotions = [str(config.get("warmup_emotion") or "happy")]

        on_progress("initializing", f"正在预热 {provider_name} 模型...")
        results = [warmup(emotion=item, synthesize=True) for item in emotions]
        failed_results = [item for item in results if not item.get("ok")]
        total_duration_ms = sum(float(item.get("duration_ms") or 0) for item in results)
        if failed_results:
            messages = ", ".join(str(item.get("error") or "unknown") for item in failed_results)
            on_progress("error", f"{provider_name} warmup failed：{messages}")
            return
        on_progress("ready", f"{provider_name} 模型已就绪（{total_duration_ms:.0f}ms）。")
    except Exception as exc:
        on_progress("error", f"启动预热失败：{exc}")
