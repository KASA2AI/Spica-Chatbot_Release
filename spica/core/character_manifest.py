"""Portable character data. No scripts, machine settings, or model loading."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$"
Emotion = Literal["happy", "angry", "sad", "surprised"]
ROLE_FILES = ("SKILL.md", "self.md", "persona.md")
_DEVICE_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def portable_name(value: str) -> bool:
    return (
        not value.endswith((".", " "))
        and value.split(".", 1)[0].upper() not in _DEVICE_NAMES
    )


class _Data(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Costume(_Data):
    id: str = Field(pattern=IDENTIFIER)
    label: str = Field(min_length=1, max_length=120)
    default_sprite: str
    emotions: dict[Emotion, list[str]] = Field(default_factory=dict)
    # Optional exact pose / expression mapping for the existing visual classifier.
    expression_sprites: dict[str, dict[str, str]] = Field(default_factory=dict)


class Visuals(_Data):
    renderer: Literal["sprite", "eye-rig"] = "sprite"
    sprites: dict[str, str] = Field(min_length=1, max_length=2048)
    eye_rigs: dict[str, str] = Field(default_factory=dict, max_length=128)
    costumes: list[Costume] = Field(min_length=1, max_length=128)
    default_costume: str
    rules_file: str | None = None

    @model_validator(mode="after")
    def references(self) -> Visuals:
        if (self.renderer == "eye-rig") != bool(self.eye_rigs):
            raise ValueError("eye-rig renderer requires eye_rigs")
        if not set(self.eye_rigs).issubset(self.sprites):
            raise ValueError("eye rig references an unknown sprite")
        if any(
            re.fullmatch(IDENTIFIER, key) is None or not portable_name(key)
            for key in self.sprites
        ):
            raise ValueError("invalid sprite id")
        if len({key.casefold() for key in self.sprites}) != len(self.sprites):
            raise ValueError("sprite ids must be distinct on Windows")
        ids = [costume.id for costume in self.costumes]
        if (
            len(set(ids)) != len(ids)
            or self.default_costume not in ids
            or any(not portable_name(key) for key in ids)
        ):
            raise ValueError("duplicate or missing default costume")
        for costume in self.costumes:
            used = [costume.default_sprite]
            for choices in costume.emotions.values():
                used.extend(choices)
            if costume.expression_sprites and not self.rules_file:
                raise ValueError("expression_sprites requires rules_file")
            for pose, expressions in costume.expression_sprites.items():
                if pose not in {"normal", "arms_crossed", "index_finger"} or any(
                    re.fullmatch(r"[0-9]{3}", key) is None for key in expressions
                ):
                    raise ValueError("invalid pose or expression id")
                used.extend(expressions.values())
            if any(key not in self.sprites for key in used):
                raise ValueError(f"unknown sprite in costume {costume.id}")
        return self


class TtsParameters(_Data):
    top_k: int = Field(default=5, ge=1, le=100)
    top_p: float = Field(default=1.0, gt=0, le=1)
    temperature: float = Field(default=1.0, gt=0, le=2)
    speed: float = Field(default=1.0, ge=0.5, le=2)


class VoiceReference(_Data):
    audio: str
    text: str = Field(min_length=1, max_length=2000)
    language: Literal["日文", "中文", "英文"] = "日文"
    additional_audio: list[str] = Field(default_factory=list, max_length=32)


class TtsVoice(_Data):
    engine: Literal["gptsovits"] = "gptsovits"
    model_version: Literal["v2Pro", "v2ProPlus"] = "v2ProPlus"
    gpt: str
    sovits: str
    target_language: Literal["日文", "中文", "英文"] = "日文"
    reference: VoiceReference
    emotions: dict[Emotion, VoiceReference] = Field(default_factory=dict)
    parameters: TtsParameters = Field(default_factory=TtsParameters)


class RvcVoice(_Data):
    engine: Literal["rvc_v2"] = "rvc_v2"
    model: str
    index: str | None = None
    index_rate: float = Field(default=0.5, ge=0, le=1)
    transpose: int = Field(default=0, ge=-24, le=24)
    protect: float = Field(default=0.33, ge=0, le=0.5)


class Scene(_Data):
    background: str | None = None
    background_color: str = Field(default="#233b55", pattern=r"^#[0-9a-fA-F]{6}$")
    # Fractions consumed by CSS object-position / the robot's cover crop.
    background_position: tuple[float, float] = (0.5, 0.5)

    @model_validator(mode="after")
    def position(self) -> Scene:
        if not all(0 <= value <= 1 for value in self.background_position):
            raise ValueError("background_position must be in 0..1")
        return self


class CharacterManifest(BaseModel):
    # Existing role-card metadata (profile/tags/source/etc.) stays compatible.
    # Only the declared fields below can change executable runtime settings.
    model_config = ConfigDict(extra="allow")

    pack_format: Literal[1, 2]
    slug: str = Field(pattern=IDENTIFIER)
    version: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    char_name: str = Field(min_length=1, max_length=120)
    user_aliases: list[str] = Field(default_factory=list, max_length=24)
    visuals: Visuals
    tts: TtsVoice | None = None
    rvc: RvcVoice | None = None
    backgrounds: dict[str, str] = Field(default_factory=dict, max_length=128)
    scenes: dict[Literal["mobile", "robot"], Scene] = Field(default_factory=dict)
    avatar: str | None = None
    settings_background: str | None = None
    worldbook_file: str | None = None
    memory_file: str | None = None

    @model_validator(mode="after")
    def references(self) -> CharacterManifest:
        if self.pack_format == 1 and self.visuals.renderer != "sprite":
            raise ValueError("animated character packages require pack_format: 2")
        if not portable_name(self.slug):
            raise ValueError("invalid character id")
        if any(
            re.fullmatch(IDENTIFIER, key) is None or not portable_name(key)
            for key in self.backgrounds
        ):
            raise ValueError("invalid background id")
        if len({key.casefold() for key in self.backgrounds}) != len(self.backgrounds):
            raise ValueError("background ids must be distinct on Windows")
        if any(not alias.strip() or len(alias) > 120 for alias in self.user_aliases):
            raise ValueError("invalid user alias")
        for scene in self.scenes.values():
            if (
                scene.background is not None
                and scene.background not in self.backgrounds
            ):
                raise ValueError("unknown scene background")
        return self

    def files(self, root: Path | None = None) -> set[str]:
        """Declared resources, including each eye rig's texture references."""
        paths = set(self.visuals.sprites.values()) | set(self.backgrounds.values())
        if self.visuals.eye_rigs:
            from spica.core.eye_rig import load_eye_rig

            if root is None:
                raise ValueError("eye rig resource resolution requires a package root")
            for sprite_id, relative in self.visuals.eye_rigs.items():
                rig = load_eye_rig(package_file(root, relative))
                directory = PurePosixPath(relative).parent
                opened = (directory / rig.textures.open).as_posix()
                closed = (directory / rig.textures.closed).as_posix()
                if opened != self.visuals.sprites[sprite_id]:
                    raise ValueError("eye rig open texture must match its sprite")
                paths.update((relative, opened, closed))
        paths.update(
            path
            for path in (self.avatar, self.settings_background, self.worldbook_file,
                         self.memory_file, self.visuals.rules_file)
            if path
        )
        if self.tts:
            paths.update((self.tts.gpt, self.tts.sovits, self.tts.reference.audio))
            for reference in [self.tts.reference, *self.tts.emotions.values()]:
                paths.add(reference.audio)
                paths.update(reference.additional_audio)
        if self.rvc:
            paths.add(self.rvc.model)
            if self.rvc.index:
                paths.add(self.rvc.index)
        return paths


def package_file(root: Path, value: str) -> Path:
    """Resolve an ordinary package file, rejecting escapes and symlinks."""
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or ":" in value
        or path.as_posix() != value
        or any(part in {".", ".."} for part in path.parts)
        or any(
            not portable_name(part) or any(char in '<>"|?*' for char in part)
            for part in path.parts
        )
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError(f"invalid package path: {value!r}")
    root = root.resolve()
    candidate = root
    for part in path.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError(f"package symlink is not allowed: {value}")
    if not candidate.is_file() or not candidate.resolve().is_relative_to(root):
        raise ValueError(f"missing package file: {value}")
    return candidate
