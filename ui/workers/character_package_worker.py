"""Folder copies run outside Qt's event loop; completion crosses a signal."""

from __future__ import annotations

import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal

from spica.core.events import GenericEvent


class CharacterPackageWorker(QObject):
    completed = Signal(object)

    def __init__(self, operation: Callable[[], dict], parent: QObject | None = None):
        super().__init__(parent)
        self._operation = operation

    def start(self) -> None:
        threading.Thread(target=self._run, name="character-folder", daemon=True).start()

    def _run(self) -> None:
        try:
            event = GenericEvent(
                event_kind="character_package", data={"ok": True, **self._operation()}
            )
        except Exception as error:
            event = GenericEvent(
                event_kind="character_package", data={"ok": False, "error": str(error)}
            )
        try:
            self.completed.emit(event)
        except RuntimeError:
            pass  # The window may have closed while a staged copy was completing.
