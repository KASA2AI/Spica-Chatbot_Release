from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QThread, Signal


class StartupWarmupWorker(QThread):
    """Thin background thread (Phase 6E): runs ``AppHost.warmup`` off the UI
    thread and forwards its Qt-free ``(stage, message)`` progress to signals.

    The warmup logic itself now lives in ``AppHost.warmup`` (host-orchestrated).
    """

    status_changed = Signal(str)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, host: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.host = host

    def run(self) -> None:
        self.host.warmup(self._on_progress)

    def _on_progress(self, stage: str, message: str) -> None:
        if stage == "ready":
            self.finished_ok.emit(message)
        elif stage == "error":
            self.failed.emit(message)
        else:  # "initializing"
            self.status_changed.emit(message)
