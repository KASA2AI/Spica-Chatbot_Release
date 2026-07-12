"""Typed paths used by Config Studio catalogue and authoring operations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldSegment:
    name: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("field segment must not be empty")

    def plain_value(self) -> str:
        return self.name

    def display_text(self, *, first: bool) -> str:
        return self.name if first else f".{self.name}"

    def to_wire(self) -> dict[str, str | int]:
        return {"kind": "field", "name": self.name}


@dataclass(frozen=True)
class MapKeySegment:
    key: str

    def plain_value(self) -> str:
        return self.key

    def display_text(self, *, first: bool) -> str:
        del first
        return f"[{self.key!r}]"

    def to_wire(self) -> dict[str, str | int]:
        return {"kind": "map_key", "key": self.key}


@dataclass(frozen=True)
class ListIndexSegment:
    index: int

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("list index must not be negative")

    def plain_value(self) -> int:
        return self.index

    def display_text(self, *, first: bool) -> str:
        del first
        return f"[{self.index}]"

    def to_wire(self) -> dict[str, str | int]:
        return {"kind": "list_index", "index": self.index}


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

    def plain_values(self) -> tuple[str | int, ...]:
        return tuple(segment.plain_value() for segment in self.segments)

    def display_path(self) -> str:
        return "".join(
            segment.display_text(first=index == 0)
            for index, segment in enumerate(self.segments)
        )

    def to_wire(self) -> list[dict[str, str | int]]:
        return [segment.to_wire() for segment in self.segments]
