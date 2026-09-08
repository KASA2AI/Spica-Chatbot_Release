"""Declarative dialogue artwork for the local desktop."""

from __future__ import annotations

from typing import Annotated, Literal
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

Color = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]
ImagePath = Annotated[str, Field(pattern=r"^images/[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\.png$")]


class StyleValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class StyleColors(StyleValue):
    text: Color = "#F1F5FA"
    speaker: Color = "#E6EEF8"
    outline: Color = "#1C2E46"
    accent: Color = "#DCE8F5"
    input: Color = "#EDF2F8"
    muted: Color = "#C2D3E5"


class StyleText(StyleValue):
    size: float = Field(default=18, ge=12, le=28)
    speaker_size: float = Field(default=16, ge=12, le=26)
    line_height: float = Field(default=29, ge=22, le=40)
    outline_width: float = Field(default=0.85, ge=0, le=3)


class StyleLayout(StyleValue):
    left: float = Field(default=140 / 900, ge=0.02, le=0.25)
    right: float = Field(default=78 / 900, ge=0.02, le=0.25)
    top: float = Field(default=29, ge=0, le=32)
    name_width: float = Field(default=110, ge=80, le=260)
    name_height: float = Field(default=26, ge=24, le=38)
    name_inset: float = Field(default=0, ge=0, le=30)
    gap: float = Field(default=10, ge=2, le=16)
    surface_top: float = Field(default=0, ge=0, le=40)
    surface_opacity: float = Field(default=1, ge=0.1, le=1)
    nameplate_opacity: float = Field(default=1, ge=0.1, le=1)


class StyleTail(StyleValue):
    placement: Literal["inline", "corner"] = "inline"
    columns: int = Field(default=17, ge=1, le=64, strict=True)
    rows: int = Field(default=4, ge=1, le=64, strict=True)
    frames: int = Field(default=68, ge=1, le=256, strict=True)
    frame_ms: int = Field(default=40, ge=20, le=2000, strict=True)
    width: float = Field(default=43, ge=12, le=60)
    height: float = Field(default=24, ge=12, le=40)

    @model_validator(mode="after")
    def valid_grid(self) -> StyleTail:
        if self.frames > self.columns * self.rows:
            raise ValueError("tail frames exceed the atlas grid")
        return self


class DialogueStyle(StyleValue):
    format: Literal["spica-dialogue-style"] = "spica-dialogue-style"
    format_version: Literal[1] = 1
    style_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(default="1.0.0", min_length=1, max_length=40)
    author: str = Field(default="", max_length=120)
    images: dict[Literal["surface", "tail", "nameplate"], ImagePath]
    colors: StyleColors = Field(default_factory=StyleColors)
    text: StyleText = Field(default_factory=StyleText)
    layout: StyleLayout = Field(default_factory=StyleLayout)
    tail: StyleTail = Field(default_factory=StyleTail)

    @model_validator(mode="after")
    def required_images(self) -> DialogueStyle:
        if not {"surface", "tail"} <= self.images.keys():
            raise ValueError("surface and tail PNG images are required")
        if len(set(self.images.values())) != len(self.images):
            raise ValueError("each artwork must have its own image")
        return self


@dataclass(frozen=True)
class LoadedDialogueStyle:
    root: Path
    style: DialogueStyle
    revision: str
    assets: dict[str, dict]

    def presentation(self) -> dict:
        return {"style": self.style.model_dump(mode="json"), "revision": self.revision,
                "assets": self.assets}
