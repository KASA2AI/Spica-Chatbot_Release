"""Same-user, installation-local text notifications. No chat, tools or audio authority."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

from PySide6.QtCore import QCoreApplication, QLockFile, QObject, QStandardPaths, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from spica.core.events import DesktopNoticeEvent

_MAX_PACKET = 4096
_MAX_CLIENTS = 8


def server_name() -> str:
    # Separate installations/worktrees without storing credentials or a discovery server.
    installation = str(Path(__file__).resolve().parents[1])
    owner = str(Path.home())
    return 'spica-notice-' + hashlib.sha256((owner + '\0' + installation).encode()).hexdigest()[:24]


class LocalNotifications(QObject):
    """Owned by the desktop window. Each client can submit exactly one bounded event."""

    def __init__(self, deliver, parent=None, *, name=None):
        super().__init__(parent)
        self._deliver = deliver
        self._clients = set()
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.setMaxPendingConnections(_MAX_CLIENTS)
        self._server.newConnection.connect(self._accept)
        identity = name or server_name()
        lock_dir = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)) / 'spica-notifications'
        self.listening = False
        self._lock = None
        try:
            lock_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return  # Notifications are optional; a read-only home cannot prevent startup.
        lock_name = hashlib.sha256(identity.encode()).hexdigest()
        self._lock = QLockFile(str(lock_dir / (lock_name + '.lock')))
        self._lock.setStaleLockTime(0)  # A living slow instance never loses its lease.
        self.listening = False
        if self._lock.tryLock(0):
            # A crashed process can leave a Unix socket. Only the exclusive
            # installation owner may remove it; a second live instance cannot.
            QLocalServer.removeServer(identity)
            self.listening = self._server.listen(identity)
            if not self.listening:
                self._lock.unlock()

    def _accept(self):
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if len(self._clients) >= _MAX_CLIENTS:
                socket.abort()
                socket.deleteLater()
                continue
            self._clients.add(socket)
            socket.setReadBufferSize(_MAX_PACKET + 1)
            deadline = QTimer(socket)
            deadline.setSingleShot(True)
            deadline.timeout.connect(socket.abort)
            deadline.start(2000)
            socket.disconnected.connect(lambda sock=socket: self._release(sock))
            socket.readyRead.connect(lambda sock=socket: self._receive(sock))
            self._receive(socket)

    def _release(self, socket):
        self._clients.discard(socket)
        socket.deleteLater()

    def _receive(self, socket):
        if socket not in self._clients or socket.property("handled"):
            return
        if socket.bytesAvailable() > _MAX_PACKET:
            socket.abort()
            return
        if not socket.canReadLine():
            return
        raw = bytes(socket.readLine(_MAX_PACKET + 1))
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) != {'title', 'message', 'notification_id'}:
                raise ValueError('text notification fields required')
            event = DesktopNoticeEvent(**payload)
            # Notification surfaces render plain text. Reject invisible control sequences.
            if any(ord(c) < 32 and c not in '\n\t' for text in (event.title, event.message, event.notification_id) for c in text):
                raise ValueError('control characters are not supported')
            self._deliver(event)
            result = {'status': 'queued', 'notification_id': event.notification_id}
        except (ValueError, TypeError, UnicodeError):
            result = {'status': 'rejected'}
        except Exception:
            result = {'status': 'unavailable'}
        # Remove before writing so a second message on this connection cannot be delivered.
        socket.setProperty("handled", True)
        socket.write(json.dumps(result).encode() + b'\n')
        socket.disconnectFromServer()

    def shutdown(self):
        self.listening = False
        self._server.close()
        for socket in tuple(self._clients):
            socket.abort()
        self._clients.clear()
        if self._lock is not None:
            self._lock.unlock()


def send_notice(title: str, message: str, notification_id: str | None = None) -> dict:
    event = DesktopNoticeEvent(notification_id or uuid.uuid4().hex, title, message)
    application = QCoreApplication.instance() or QCoreApplication([sys.argv[0]])
    socket = QLocalSocket()
    try:
        socket.connectToServer(server_name())
        if not socket.waitForConnected(1000):
            return {'status': 'unavailable'}
        socket.write(json.dumps(event._data(), ensure_ascii=False).encode() + b'\n')
        socket.flush()
        deadline = time.monotonic() + 2.
        while not socket.canReadLine() and time.monotonic() < deadline:
            if not socket.waitForReadyRead(max(1, int((deadline - time.monotonic()) * 1000))):
                break
        if socket.canReadLine():
            return json.loads(bytes(socket.readLine(_MAX_PACKET)))
        return {'status': 'unavailable'}
    finally:
        socket.abort()
        del application
