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


def _warmup_stt(
    stt_adapter: Any,
    on_progress: Callable[[str, str], None],
    *,
    warmup_on_startup: bool = True,
) -> None:
    """Plan B: warm the local STT model alongside TTS so the first utterance has no
    load/compile lag. Best-effort -- a failure is reported but never blocks startup
    (voice simply loads on first use, or stays unavailable with a clear log). Only
    runs when a local adapter exists and warmup_on_startup is enabled."""
    warmup = getattr(stt_adapter, "warmup", None)
    if stt_adapter is None or warmup is None:
        return
    if getattr(stt_adapter, "requires_model_warmup", True) is False:
        result = warmup()
        on_progress("ready" if result.get("ok") else "error",
                    "云端语音识别已配置，实际连接待说话验证；不加载本地识别模型。"
                    if result.get("ok") else result.get("error", "云端识别不可用"))
        return
    if not warmup_on_startup:
        # stt.warmup_on_startup=false -> lazy load on the first utterance. (The
        # flag existed in SttConfig from day one but was never consumed here --
        # the docstring promised the gate while the code ignored it.)
        on_progress("ready", "语音识别启动预热已关闭（首次说话时加载）。")
        return
    on_progress("initializing", "正在预热本地语音识别模型...")
    result = warmup()
    if result.get("ok"):
        on_progress("ready", f"语音识别模型已就绪（{float(result.get('duration_ms') or 0):.0f}ms）。")
    else:
        # Not fatal: surface a CLEAR cause so a non-transcribing mic is diagnosable.
        on_progress("error", f"语音识别模型预热失败：{result.get('error') or 'unknown'}")


def _warmup_tts(
    surface: Any,
    tts_adapter: Any,
    on_progress: Callable[[str, str], None],
) -> None:
    """TTS (+ LLM-ready) warmup -- the original run_warmup body, unchanged."""
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


def run_warmup(
    surface: Any,
    tts_adapter: Any,
    on_progress: Callable[[str, str], None],
    stt_adapter: Any = None,
    stt_warmup_on_startup: bool = True,
    daily_voice_enabled: bool = True,
) -> None:
    """Run startup warmup, reporting progress as ``on_progress(stage, message)``
    where stage is ``"initializing" | "ready" | "error"``.

    Warms TTS (+ LLM-ready) first, then the optional local STT adapter (Plan B).
    STT runs regardless of the TTS outcome (independent models), so a TTS issue
    never skips loading the voice-input model. The UI runs this on a background
    thread and maps stages to its loading UI.
    """
    try:
        if daily_voice_enabled:
            _warmup_tts(surface, tts_adapter, on_progress)
        else:
            on_progress("ready", "日常无声回复已启用；麦克风偏好不变，闹钟按需准备语音。")
        _warmup_stt(stt_adapter, on_progress, warmup_on_startup=stt_warmup_on_startup)
    finally:
        from common.memory import release_native_buffers
        release_native_buffers()


def prepare_output_audio(tts: Any) -> bool:
    """Output-only preparation: microphone/STT readiness is independent."""
    if tts is None or getattr(tts, 'name', '') == 'text_only':
        return False
    warmup = getattr(tts, 'warmup', None)
    if warmup is None:
        return True
    result = warmup(synthesize=True)
    return bool(result.get('ok') and getattr(tts, 'resource_status', {'ready': True})['ready'])
