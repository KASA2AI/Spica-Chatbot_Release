"""Qt-free command contract shared with the overlay configuration owner."""

from __future__ import annotations

from dataclasses import dataclass, field


class OverlayOwnerError(RuntimeError):
    """Typed error raised by the overlay document owner."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def __repr__(self) -> str:
        return f"OverlayOwnerError(code={self.code!r})"


@dataclass(frozen=True, slots=True)
class OverlaySetValue:
    """Set one fixed owner-validated overlay preference."""

    key: str
    value: float = field(repr=False)


__all__ = ["OverlayOwnerError", "OverlaySetValue"]
