"""Typed paths used by Config Studio catalogue and authoring operations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldSegment:
    name: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("field segment must not be empty")


@dataclass(frozen=True)
class MapKeySegment:
    key: str


@dataclass(frozen=True)
class ListIndexSegment:
    index: int

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("list index must not be negative")


PathSegment = FieldSegment | MapKeySegment | ListIndexSegment


@dataclass(frozen=True)
class ConfigFieldPath:
    segments: tuple[PathSegment, ...]

    @classmethod
    def fields(cls, *names: str) -> "ConfigFieldPath":
        return cls(tuple(FieldSegment(name) for name in names))

    def field_names(self) -> tuple[str, ...]:
        names: list[str] = []
        for segment in self.segments:
            if not isinstance(segment, FieldSegment):
                raise TypeError("path contains a dynamic segment")
            names.append(segment.name)
        return tuple(names)
