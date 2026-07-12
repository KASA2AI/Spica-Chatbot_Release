"""In-memory browser session security for the loopback Config Studio."""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import re
import secrets
import threading
import time
from typing import Callable


SESSION_COOKIE_NAME = "spica_config_studio_session"
BOOTSTRAP_HEADER_NAME = "X-Spica-Bootstrap"
CSRF_HEADER_NAME = "X-Spica-CSRF"


def _authority(host: str, port: int) -> str:
    if host == "::1":
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _secure_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True, slots=True, repr=False)
class SessionCredentials:
    session_token: str
    csrf_token: str

    def __repr__(self) -> str:
        return "SessionCredentials(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class BootstrapGrant:
    security_context: SecurityContext
    bootstrap_token: str

    def __repr__(self) -> str:
        return "BootstrapGrant(<redacted>)"


@dataclass(slots=True, repr=False)
class _Session:
    token: str
    csrf_token: str
    expires_at: float


class SecurityContext:
    """Owns a one-shot bootstrap token and its resulting browser session."""

    @classmethod
    def issue(
        cls,
        *,
        host: str,
        port: int,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] = _secure_token,
        bootstrap_ttl_seconds: float = 60.0,
        session_ttl_seconds: float = 8 * 60 * 60,
        max_bootstrap_attempts: int = 5,
    ) -> BootstrapGrant:
        """Create a context and the random token passed to the browser launcher."""

        bootstrap_token = token_factory()
        context = cls(
            host=host,
            port=port,
            bootstrap_token=bootstrap_token,
            clock=clock,
            token_factory=token_factory,
            bootstrap_ttl_seconds=bootstrap_ttl_seconds,
            session_ttl_seconds=session_ttl_seconds,
            max_bootstrap_attempts=max_bootstrap_attempts,
        )
        return BootstrapGrant(
            security_context=context,
            bootstrap_token=bootstrap_token,
        )

    def __init__(
        self,
        *,
        host: str,
        port: int,
        bootstrap_token: str,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] = _secure_token,
        bootstrap_ttl_seconds: float = 60.0,
        session_ttl_seconds: float = 8 * 60 * 60,
        max_bootstrap_attempts: int = 5,
    ) -> None:
        if host not in {"127.0.0.1", "::1"}:
            raise ValueError("security context requires a literal loopback host")
        if not 1 <= port <= 65535:
            raise ValueError("security context requires a bound TCP port")
        if not isinstance(bootstrap_token, str) or re.fullmatch(
            r"[A-Za-z0-9_-]{22,256}", bootstrap_token
        ) is None:
            raise ValueError("bootstrap token must be a high-entropy URL-safe value")
        if bootstrap_ttl_seconds <= 0 or session_ttl_seconds <= 0:
            raise ValueError("security token lifetimes must be positive")
        if (
            type(max_bootstrap_attempts) is not int
            or not 1 <= max_bootstrap_attempts <= 32
        ):
            raise ValueError("bootstrap attempt limit is invalid")
        self.host = host
        self.port = port
        self.authority = _authority(host, port)
        self.origin = f"http://{self.authority}"
        self._bootstrap_token: str | None = bootstrap_token
        self._clock = clock
        self._token_factory = token_factory
        self._bootstrap_expires_at = clock() + bootstrap_ttl_seconds
        self._bootstrap_attempts_remaining = max_bootstrap_attempts
        self._session_ttl_seconds = session_ttl_seconds
        self._session: _Session | None = None
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return (
            f"SecurityContext(host={self.host!r}, port={self.port!r}, "
            "credentials=<redacted>)"
        )

    def exchange_bootstrap(self, candidate: str) -> SessionCredentials | None:
        with self._lock:
            expected = self._bootstrap_token
            if expected is None or self._clock() >= self._bootstrap_expires_at:
                self._bootstrap_token = None
                return None
            if not _constant_time_equal(candidate, expected):
                self._bootstrap_attempts_remaining -= 1
                if self._bootstrap_attempts_remaining <= 0:
                    self._bootstrap_token = None
                return None

            session_token = self._token_factory()
            csrf_token = self._token_factory()
            if not session_token or not csrf_token:
                raise RuntimeError("security token factory returned an empty token")
            self._bootstrap_token = None
            self._session = _Session(
                token=session_token,
                csrf_token=csrf_token,
                expires_at=self._clock() + self._session_ttl_seconds,
            )
            return SessionCredentials(
                session_token=session_token,
                csrf_token=csrf_token,
            )

    def bootstrap_is_pending(self) -> bool:
        """Report whether the one-shot grant may still be redeemed."""

        with self._lock:
            if self._bootstrap_token is None:
                return False
            if self._clock() >= self._bootstrap_expires_at:
                self._bootstrap_token = None
                return False
            return self._bootstrap_attempts_remaining > 0

    def session_is_valid(self, candidate: str | None) -> bool:
        with self._lock:
            session = self._active_session()
            return bool(
                session is not None
                and candidate is not None
                and _constant_time_equal(candidate, session.token)
            )

    def csrf_is_valid(
        self,
        session_candidate: str | None,
        csrf_candidate: str | None,
    ) -> bool:
        with self._lock:
            session = self._active_session()
            return bool(
                session is not None
                and session_candidate is not None
                and csrf_candidate is not None
                and _constant_time_equal(session_candidate, session.token)
                and _constant_time_equal(csrf_candidate, session.csrf_token)
            )

    def csrf_for_session(self, session_candidate: str | None) -> str | None:
        """Return the existing CSRF token for one authenticated session."""

        with self._lock:
            session = self._active_session()
            if (
                session is None
                or session_candidate is None
                or not _constant_time_equal(session_candidate, session.token)
            ):
                return None
            return session.csrf_token

    def _active_session(self) -> _Session | None:
        session = self._session
        if session is not None and self._clock() >= session.expires_at:
            self._session = None
            return None
        return session
