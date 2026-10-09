"""A real local socket accepts only bounded text and never starts a conversation."""

import json
import uuid

import pytest

pytest.importorskip('PySide6')
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtTest import QTest
from test_voice_wake import qapp
from ui.local_notifications import LocalNotifications


def test_owner_lease_rejects_second_server_and_socket_delivers_once(qapp):
    delivered = []
    name = 'spica-test-' + uuid.uuid4().hex
    server = LocalNotifications(delivered.append, name=name)
    duplicate = LocalNotifications(lambda _event: pytest.fail('second instance stole the socket'), name=name)
    socket = QLocalSocket()
    try:
        assert server.listening and not duplicate.listening
        duplicate.shutdown()  # A failed lease must not remove the live endpoint.
        socket.connectToServer(name)
        assert socket.waitForConnected(1000)
        data = {'notification_id': 'one', 'title': '任务完成', 'message': '<b>纯文字</b>'}
        socket.write(json.dumps(data).encode() + b'\n' + json.dumps(data).encode() + b'\n')
        socket.flush()
        for _ in range(20):
            QTest.qWait(10)
            if socket.canReadLine():
                break
        result = json.loads(bytes(socket.readLine()))
        assert result['status'] == 'queued'
        assert [event.notification_id for event in delivered] == ['one']
        server.shutdown()
        replacement = LocalNotifications(delivered.append, name=name)
        assert replacement.listening
        replacement.shutdown()
    finally:
        socket.abort()
        server.shutdown()
        duplicate.shutdown()
        server.deleteLater()
        duplicate.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.mark.parametrize('payload', [b'[]\n', b'{}\n', b'x' * 4100, json.dumps({
    'notification_id': 'bad', 'title': '任务', 'message': 'hello', 'command': 'execute',
}).encode() + b'\n', json.dumps({
    'notification_id': 'bad', 'title': '任务', 'message': '\0secret',
}).encode() + b'\n'])
def test_invalid_or_oversized_frames_cannot_deliver(qapp, payload):
    delivered = []
    name = 'spica-test-' + uuid.uuid4().hex
    server = LocalNotifications(delivered.append, name=name)
    socket = QLocalSocket()
    try:
        socket.connectToServer(name)
        assert socket.waitForConnected(1000)
        socket.write(payload)
        socket.flush()
        QTest.qWait(30)
        assert not delivered
    finally:
        socket.abort()
        server.shutdown()
        server.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
