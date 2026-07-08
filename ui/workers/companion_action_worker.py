"""Generic background thread for galgame companion actions (Path B stage 3).

Same shape as StartupWarmupWorker / ScreenshotWorker: a QThread wrapping ONE
Qt-free callable, forwarding the result / error as signals delivered on the GUI
thread. Used for every blocking companion action -- begin_bind (wmctrl/xprop
subprocesses), calibration capture+OCR (first call loads the RapidOCR engine,
seconds), controller.start, and ESPECIALLY controller.stop (drains summary jobs
+ runs the final-summary LLM: seconds to tens of seconds -- it must NEVER run on
the UI thread).
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal


class CompanionActionWorker(QThread):
    finished_ok = Signal(object)  # the callable's return value (may be None)
    failed = Signal(str)

    def __init__(self, fn: Callable[[], Any], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self.finished_ok.emit(self._fn())
        except Exception as exc:  # noqa: BLE001 -- surfaced to the UI as a message
            self.failed.emit(str(exc))
