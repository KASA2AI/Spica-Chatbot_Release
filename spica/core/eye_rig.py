"""Data for the bounded, original-art eye animation used by character packs."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator


class _Data(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EyeCanvas(_Data):
    width: int = Field(ge=1, le=4096)
    height: int = Field(ge=1, le=4096)
    origin: Literal["top-left"] = "top-left"
    y_direction: Literal["down"] = "down"


class EyeTextures(_Data):
    open: str
    closed: str


class EyeGaze(_Data):
    origin: tuple[FiniteFloat, FiniteFloat]
    maximum_offset: tuple[FiniteFloat, FiniteFloat]
    strength: float = Field(default=0.7, ge=0, le=1)
    smoothing_ms: float = Field(default=95, ge=20, le=1000)


class EyeBlink(_Data):
    duration_ms: int = Field(default=210, ge=80, le=2000)
    initial_delay_ms: int = Field(default=1600, ge=0, le=60000)
    interval_ms: tuple[int, int] = (2700, 5200)

    @model_validator(mode="after")
    def interval(self):
        if not 1000 <= self.interval_ms[0] <= self.interval_ms[1] <= 60000:
            raise ValueError("invalid blink interval")
        return self


class EyeRegion(_Data):
    bounds: tuple[int, int, int, int]
    iris: tuple[FiniteFloat, FiniteFloat]
    closed_line: list[tuple[FiniteFloat, FiniteFloat]] = Field(
        alias="closedLine", min_length=2, max_length=128
    )
    contour: list[tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat]] = Field(
        min_length=3, max_length=128
    )


class EyeRig(_Data):
    format: Literal["spica-eye-rig"]
    version: Literal[1]
    description: str = Field(default="", max_length=2000)
    canvas: EyeCanvas
    textures: EyeTextures
    views: dict[Literal["portrait", "face"], tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat]] = Field(default_factory=dict)
    gaze: EyeGaze
    blink: EyeBlink = Field(default_factory=EyeBlink)
    eyes: list[EyeRegion] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def geometry(self):
        width, height = self.canvas.width, self.canvas.height
        if not (0 <= self.gaze.origin[0] <= width and 0 <= self.gaze.origin[1] <= height):
            raise ValueError("gaze origin is outside the artwork")
        dx, dy = self.gaze.maximum_offset
        if not (0 <= dx <= 32 and 0 <= dy <= 16):
            raise ValueError("eye movement exceeds the supported range")
        for eye in self.eyes:
            x, y, w, h = eye.bounds
            if not (0 <= x < x + w <= width and 0 <= y < y + h <= height and w <= 512 and h <= 512):
                raise ValueError("eye bounds are outside the artwork or too large")
            for points in (eye.contour, eye.closed_line):
                if any(b[0] - a[0] < 0.5 for a, b in zip(points, points[1:])):
                    raise ValueError("eye contour x coordinates must increase by at least half a pixel")
                if any(not (x <= point[0] <= x + w and all(y <= v <= y + h for v in point[1:])) for point in points):
                    raise ValueError("eye contour is outside its bounds")
            if any(not lash <= top <= bottom for _, top, bottom, lash in eye.contour):
                raise ValueError("eye lid contours are inverted")
            left, right = eye.contour[0][0], eye.contour[-1][0]
            if right - left < 4:
                raise ValueError("eye contour is too narrow")
            if not (left + dx < eye.iris[0] < eye.iris[1] < right - dx):
                raise ValueError("eye movement exceeds the surrounding white")
            if eye.closed_line[0][0] > left or eye.closed_line[-1][0] < right:
                raise ValueError("closed lid does not cover the eye")
        return self


def load_eye_rig(path: Path) -> EyeRig:
    if path.stat().st_size > 128 * 1024:
        raise ValueError("eye rig configuration is too large")
    return EyeRig.model_validate_json(path.read_bytes())
