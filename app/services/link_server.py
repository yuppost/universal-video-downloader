"""The app side of the browser integration: one running instance listens for links sent by the extension.

``LinkServer`` listens on the per-user local pipe. A second launch of the exe (``--open-url <url>``) first tries
``forward_to_running_app`` and exits when an instance already exists - so there is never a second window.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from app.services import link_protocol as lp

log = logging.getLogger("uvs.links")


class LinkServer(QObject):
    urlReceived = Signal(str)

    def __init__(self, parent: Optional[QObject] = None, name: Optional[str] = None) -> None:
        super().__init__(parent)
        self.name = name or lp.pipe_name()
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.UserAccessOption)      # only this Windows user may connect
        self._server.newConnection.connect(self._on_connection)
        self.listening = False

    def start(self) -> bool:
        if self._server.listen(self.name):
            self.listening = True
            return True
        QLocalServer.removeServer(self.name)                              # stale socket of a crashed run
        self.listening = self._server.listen(self.name)
        if not self.listening:
            log.warning("link server could not listen: %s", self._server.errorString())
        return self.listening

    def stop(self) -> None:
        self._server.close()
        self.listening = False

    def _on_connection(self) -> None:
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            sock.setParent(self)
            buf = bytearray()

            def read(s=sock, b=buf) -> None:
                b += bytes(s.readAll())
                if len(b) > lp.MAX_URL + 100:                              # garbage / attack: drop
                    s.disconnectFromServer()
                    return
                if b"\n" in b:
                    line = bytes(b).split(b"\n", 1)[0]
                    url = lp.parse_request(line)
                    if url:
                        self.urlReceived.emit(url)
                    s.disconnectFromServer()

            sock.readyRead.connect(read)
            sock.disconnected.connect(sock.deleteLater)
            if sock.bytesAvailable():
                read()


def forward_to_running_app(url: str, name: Optional[str] = None, timeout_ms: int = 800) -> bool:
    """True when another instance accepted *url* (this process should then exit)."""
    sock = QLocalSocket()
    sock.connectToServer(name or lp.pipe_name())
    if not sock.waitForConnected(timeout_ms):
        return False
    sock.write(lp.encode_request(url))
    ok = sock.waitForBytesWritten(timeout_ms)
    sock.disconnectFromServer()
    return ok


def instance_running(name: Optional[str] = None, timeout_ms: int = 400) -> bool:
    sock = QLocalSocket()
    sock.connectToServer(name or lp.pipe_name())
    ok = sock.waitForConnected(timeout_ms)
    if ok:
        sock.disconnectFromServer()
    return ok
