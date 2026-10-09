"""Cancellation hides late output, but does not claim the producer has exited."""

import threading
from types import SimpleNamespace

import pytest
pytest.importorskip('PySide6')
from PySide6.QtTest import QTest
from test_voice_wake import qapp
from ui.workers.chat_worker import ChatWorker


def test_cancelled_worker_keeps_ownership_until_generator_cleanup(qapp):
    entered, cancelled, finish = threading.Event(), threading.Event(), threading.Event()
    def stream(*args, **kwargs):
        entered.set()
        assert kwargs['cancelled'].wait(1)
        yield {'event': 'unit_ready', 'data': {'display_text': 'late'}}
        cancelled.set()
        assert finish.wait(2)
    worker = ChatWorker(SimpleNamespace(stream_voice=stream), 'hello', 'default', {}, True, 'chat')
    emitted = []
    worker.stream_event.connect(lambda *args: emitted.append(args))
    try:
        worker.start()
        assert entered.wait(1)
        worker.requestInterruption()
        worker.cancel()
        assert cancelled.wait(1)
        QTest.qWait(10)
        assert worker.isRunning() and not worker.released.is_set()
        assert not emitted
        finish.set()
        assert worker.wait(1000)
        assert worker.released.is_set()
    finally:
        finish.set()
        worker.requestInterruption()
        worker.cancel()
        assert worker.wait(1000)
        worker.deleteLater()
