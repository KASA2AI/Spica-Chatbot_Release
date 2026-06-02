from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal

from agent_tools.function_tools.song import CancellationToken, SongPipeline, SongRequest


class SongWorker(QThread):
    completed = Signal(int, dict)
    failed = Signal(int, str)

    def __init__(
        self,
        request: SongRequest,
        job_id: int,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.request = request
        self.job_id = job_id
        self.cancellation = CancellationToken()

    def cancel(self) -> None:
        self.cancellation.cancel()
        self.requestInterruption()

    def run(self) -> None:
        try:
            result = SongPipeline().run(self.request, self.cancellation)
            if self.isInterruptionRequested() or self.cancellation.cancelled():
                return
            if result.ok:
                self.completed.emit(self.job_id, result.to_payload())
            else:
                self.failed.emit(self.job_id, result.error or result.message or "唱歌任务失败。")
        except Exception as exc:
            if not self.isInterruptionRequested() and not self.cancellation.cancelled():
                self.failed.emit(self.job_id, str(exc))

