from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QThread, Signal

from agent import SimpleAgent


class StartupWarmupWorker(QThread):
    status_changed = Signal(str)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        agent: SimpleAgent,
        tts_provider: Any,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.agent = agent
        self.tts_provider = tts_provider

    def run(self) -> None:
        try:
            model = str(getattr(self.agent, "model", "") or "unknown")
            self.status_changed.emit(f"LLM API 初始化完成：{model}")
            public_config = getattr(self.tts_provider, "public_config", None)
            warmup = getattr(self.tts_provider, "warmup", None)
            if public_config is None or warmup is None:
                provider_name = str(getattr(self.tts_provider, "name", None) or "TTS")
                self.finished_ok.emit(f"LLM API 已初始化，{provider_name} 无需启动预热。")
                return

            provider_name = str(getattr(self.tts_provider, "name", None) or "TTS")
            config = public_config()
            if not bool(config.get("warmup_on_startup", True)):
                self.finished_ok.emit(f"LLM API 已初始化，{provider_name} 启动预热已关闭。")
                return

            configured_emotions = config.get("warmup_emotions")
            if isinstance(configured_emotions, list) and configured_emotions:
                emotions = [str(item) for item in configured_emotions if str(item).strip()]
            else:
                emotions = [str(config.get("warmup_emotion") or "happy")]
            if not emotions:
                emotions = [str(config.get("warmup_emotion") or "happy")]

            self.status_changed.emit(f"正在预热 {provider_name} 模型...")
            results = [warmup(emotion=item, synthesize=True) for item in emotions]
            failed_results = [item for item in results if not item.get("ok")]
            total_duration_ms = sum(float(item.get("duration_ms") or 0) for item in results)
            if failed_results:
                messages = ", ".join(str(item.get("error") or "unknown") for item in failed_results)
                self.failed.emit(f"{provider_name} warmup failed：{messages}")
                return
            self.finished_ok.emit(f"{provider_name} 模型已就绪（{total_duration_ms:.0f}ms）。")
        except Exception as exc:
            self.failed.emit(f"启动预热失败：{exc}")

