"""CompanionEventBridge (Phase 0 ④, landed in Phase 6).

The Qt side of the long-lived, per-turn-independent galgame event channel. The
backend (sessions / binder / calibrator) emits Qt-free ``RuntimeEvent``s through
an injected ``CompanionEventSink``; this bridge IS that sink. ``sink`` may be
called from any (non-GUI) backend thread -- the Qt signal it emits is delivered to
slots via a queued connection, marshalling onto the GUI thread (same mechanism as
``ChatWorker.stream_event``).

Inject with ``AppHost.attach_companion_sink(bridge.sink)``. Qt lives here in ui/;
``spica/`` stays Qt-free.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from spica.core.events import RuntimeEvent


class CompanionEventBridge(QObject):
    companion_event = Signal(object)  # carries a RuntimeEvent

    def sink(self, event: RuntimeEvent) -> None:
        self.companion_event.emit(event)
