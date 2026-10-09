"""Release the local voice service after its dialogue consumers have stopped."""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


def begin_tts_shutdown(host):
    worker = getattr(host, "_tts_cleanup", None)
    if worker is None:
        adapter = getattr(host, "tts_adapter", None)
        def close():
            try:
                shutdown = getattr(adapter, "close", None)
                if callable(shutdown):
                    shutdown()
            except Exception as exc:
                host._tts_cleanup_error = type(exc).__name__
                logger.exception("角色语音资源回收失败")
        worker = threading.Thread(target=close, name="tts-shutdown", daemon=True)
        host._tts_cleanup = worker
        worker.start()
    return worker
