from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QThread, Signal


class StartupWarmupWorker(QThread):
    """Thin background thread (Phase 6E): runs ``AppHost.warmup`` off the UI
    thread and forwards its Qt-free ``(stage, message)`` progress to signals.

    The warmup logic itself now lives in ``AppHost.warmup`` (host-orchestrated).

    Signal contract (2026-07 review fix): per-stage progress -- including each
    stage's own "ready"/"error" -- goes to ``status_changed``; ``finished_ok`` /
    ``failed`` fire exactly ONCE, after the WHOLE warmup (TTS + STT) returns.
    Historically every stage's "ready" fired ``finished_ok``, so consumers
    chained on it (dangling-session recovery) started after the FIRST stage
    while STT warmup was still running.

    Failure terminal state carries the FIRST error message (a later stage's
    success text must not mask an earlier failure), and an unexpected exception
    from ``host.warmup`` still emits ``failed`` -- the terminal signal is
    guaranteed either way, so recovery chained on finished/failed always runs.
    """

    status_changed = Signal(str)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, host: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.host = host
        self._first_error = ""
        self._last_message = ""

    def run(self) -> None:
        self._first_error = ""
        self._last_message = ""
        try:
            self.host.warmup(self._on_progress)
        except Exception as exc:  # noqa: BLE001 -- terminal signal must fire regardless
            if not self._first_error:
                self._first_error = f"启动预热异常：{exc}"
        if self._first_error:
            self.failed.emit(self._first_error)
        else:
            self.finished_ok.emit(self._last_message)

    def _on_progress(self, stage: str, message: str) -> None:
        self._last_message = message
        if stage == "error" and not self._first_error:
            self._first_error = message
        self.status_changed.emit(message)
