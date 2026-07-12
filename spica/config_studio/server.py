"""Loopback-only socket ownership for Config Studio."""

from __future__ import annotations

from dataclasses import dataclass, field
import socket
from types import TracebackType
from typing import Any, Self

import uvicorn


_LOOPBACK_FAMILIES = {
    "127.0.0.1": socket.AF_INET,
    "::1": socket.AF_INET6,
}


@dataclass(slots=True)
class LoopbackServer:
    """An already-bound loopback socket whose address cannot be retargeted."""

    host: str
    port: int
    socket: socket.socket = field(repr=False)

    @classmethod
    def bind(
        cls,
        host: str = "127.0.0.1",
        port: int = 8765,
        *,
        allow_test_port_zero: bool = False,
    ) -> Self:
        family = _LOOPBACK_FAMILIES.get(host)
        if family is None:
            raise ValueError("Config Studio may bind only to a literal loopback address")
        if isinstance(port, bool) or not isinstance(port, int):
            raise ValueError("port must be an integer")
        if port == 0:
            if not allow_test_port_zero:
                raise ValueError("port 0 is reserved for tests")
        elif not 1024 <= port <= 65535:
            raise ValueError("port must be between 1024 and 65535")

        bound_socket = socket.socket(family, socket.SOCK_STREAM)
        try:
            if family == socket.AF_INET6:
                bound_socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            bound_socket.bind((host, port))
            bound_socket.listen(socket.SOMAXCONN)
            actual_port = int(bound_socket.getsockname()[1])
            return cls(host=host, port=actual_port, socket=bound_socket)
        except BaseException:
            bound_socket.close()
            raise

    def close(self) -> None:
        self.socket.close()

    def uvicorn_config(self, app: Any) -> uvicorn.Config:
        """Build the server config used with this object's pre-bound socket."""

        return uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            proxy_headers=False,
            access_log=False,
            server_header=False,
            date_header=False,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
