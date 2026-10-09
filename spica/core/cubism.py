"""Portable Cubism bindings. This module never imports a renderer or loads a MOC."""

from __future__ import annotations

import json
import math
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spica.core.character_manifest import IDENTIFIER, package_file


class _Data(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class CubismMotion(_Data):
    group: str = Field(max_length=120)
    index: int = Field(ge=0, le=2047)
    kind: Literal["idle", "gesture", "skill"] = "gesture"
    cooldown: float = Field(default=3, ge=0, le=120)


class ParameterDrive(_Data):
    id: str = Field(min_length=1, max_length=120)
    scale: float = Field(default=1, ge=-100, le=100)


class CubismParameters(_Data):
    eyes: list[str] = Field(default_factory=list, max_length=8)
    mouth: list[str] = Field(default_factory=list, max_length=8)
    gaze_x: list[ParameterDrive] = Field(default_factory=list, max_length=8)
    gaze_y: list[ParameterDrive] = Field(default_factory=list, max_length=8)
    fixed: dict[str, float] = Field(default_factory=dict, max_length=128)
    blink: Literal["auto", "motion"] = "auto"


class CubismModel(_Data):
    model: str
    idle: str = Field(pattern=IDENTIFIER)
    motions: dict[str, CubismMotion] = Field(min_length=1, max_length=2048)
    neutral_expression: str | None = None
    expressions: dict[str, str] = Field(default_factory=dict, max_length=256)
    idle_motions: dict[str, str] = Field(default_factory=dict, max_length=256)
    effects: dict[str, str] = Field(default_factory=dict, max_length=32)
    parameters: CubismParameters = Field(default_factory=CubismParameters)
    scale: float = Field(default=1, ge=0.1, le=4)
    offset: tuple[float, float] = (0, 0)
    physics: bool = True

    @model_validator(mode="after")
    def references(self):
        if self.idle not in self.motions or self.motions[self.idle].kind != "idle":
            raise ValueError("Cubism model needs a valid idle motion")
        if any(expression not in self.expressions or motion not in self.motions
               or self.motions[motion].kind != "idle" for expression, motion in self.idle_motions.items()):
            raise ValueError("Cubism emotion idle must reference an expression and a looping idle motion")
        if any(abs(v) > 4 for v in self.offset):
            raise ValueError("Cubism offset must be within -4..4")
        return self


class CubismBindings(_Data):
    format: Literal["spica-cubism"]
    version: Literal[1]
    director: str
    models: dict[str, CubismModel] = Field(min_length=1, max_length=128)
    costume_groups: dict[str, list[str]] = Field(default_factory=dict, max_length=128)

    @model_validator(mode="after")
    def groups(self):
        seen = set()
        for label, variants in self.costume_groups.items():
            if not label.strip() or len(label) > 120 or not variants:
                raise ValueError("invalid Cubism costume group")
            for variant in variants:
                if variant not in self.models or variant in seen:
                    raise ValueError("Cubism costume groups need unique, existing variants")
                seen.add(variant)
        return self


class CubismRule(_Data):
    id: str = Field(pattern=IDENTIFIER)
    signals: dict[str, float] = Field(default_factory=dict, max_length=64)
    keywords: list[str] = Field(default_factory=list, max_length=128)
    exclude: list[str] = Field(default_factory=list, max_length=128)
    expression: str | None = None
    motion: str | None = None
    motions: list[str] = Field(default_factory=list, max_length=32)
    effect: str | None = None
    threshold: float = Field(default=3, ge=0.1, le=100)
    priority: int = Field(default=0, ge=0, le=10)


class CubismDirector(_Data):
    format: Literal["spica-cubism-director"]
    version: Literal[1]
    hold_seconds: float = Field(default=0.8, ge=0, le=10)
    idle_return_seconds: float = Field(default=20, ge=1, le=300)
    rules: list[CubismRule] = Field(default_factory=list, max_length=128)


def load_director(root: Path, bindings: CubismBindings) -> CubismDirector:
    return CubismDirector.model_validate(read_json(package_file(root, bindings.director)))


def read_json(path: Path) -> dict:
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError(f"Cubism JSON is too large: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Cubism JSON must be an object: {path.name}")
    return value


def load_bindings(root: Path, relative: str) -> CubismBindings:
    return CubismBindings.model_validate(read_json(package_file(root, relative)))


def cubism_files(root: Path, relative: str, costumes: set[str]) -> set[str]:
    """Close the declared resource graph before installation or export."""
    bindings = load_bindings(root, relative)
    if set(bindings.models) != costumes:
        raise ValueError("Cubism models must match the costume list")
    files = {relative, bindings.director}
    load_director(root, bindings)
    for spec in bindings.models.values():
        path = package_file(root, spec.model)
        if not spec.model.endswith(".model3.json"):
            raise ValueError("Cubism model must be a .model3.json file")
        model = read_json(path)
        refs = model.get("FileReferences")
        if model.get("Version") != 3 or not isinstance(refs, dict):
            raise ValueError("invalid Cubism model references")
        files.add(spec.model)
        directory = PurePosixPath(spec.model).parent

        def include(value, suffix):
            if not isinstance(value, str) or not value.endswith(suffix):
                raise ValueError(f"invalid Cubism resource: {value!r}")
            # Validate before joining: PurePosixPath must not normalize escapes.
            child = package_file(path.parent, value)
            files.add((directory / value).as_posix())
            return child

        moc = include(refs.get("Moc"), ".moc3")
        with moc.open("rb") as stream:
            if stream.read(4) != b"MOC3" or moc.stat().st_size < 64:
                raise ValueError("invalid Cubism MOC header")
        textures = refs.get("Textures")
        if not isinstance(textures, list) or not 1 <= len(textures) <= 32:
            raise ValueError("invalid Cubism textures")
        for texture in textures:
            from PIL import Image

            with Image.open(include(texture, ".png")) as image:
                if image.format != "PNG" or max(image.size) > 8192:
                    raise ValueError("Cubism textures must be PNG, at most 8192 pixels per side")
                image.verify()
        for key, suffix in (("Physics", ".physics3.json"), ("Pose", ".pose3.json"),
                            ("DisplayInfo", ".cdi3.json"), ("UserData", ".userdata3.json")):
            if key in refs:
                read_json(include(refs[key], suffix))
        expressions = refs.get("Expressions", [])
        if not isinstance(expressions, list) or len(expressions) > 256:
            raise ValueError("invalid Cubism expressions")
        names = set()
        for expression in expressions:
            if not isinstance(expression, dict) or not isinstance(expression.get("Name"), str):
                raise ValueError("invalid Cubism expression")
            if expression["Name"] in names:
                raise ValueError("duplicate Cubism expression")
            names.add(expression["Name"])
            read_json(include(expression.get("File"), ".exp3.json"))
        if spec.neutral_expression is not None and spec.neutral_expression not in names:
            raise ValueError("missing Cubism neutral expression")
        if not set(spec.expressions.values()) <= names:
            raise ValueError("Cubism binding references a missing expression")
        for effect in spec.effects.values():
            from spica.core.cubism_motion import ParameterEffect

            effect_path = package_file(root, effect)
            ParameterEffect(read_json(effect_path))
            files.add(effect)
        groups = refs.get("Motions", {})
        if not isinstance(groups, dict) or sum(len(v) for v in groups.values() if isinstance(v, list)) > 2048:
            raise ValueError("invalid Cubism motions")
        motion_data = {}
        for group, entries in groups.items():
            if not isinstance(entries, list):
                raise ValueError("invalid Cubism motion group")
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    raise ValueError("invalid Cubism motion entry")
                data = read_json(include(entry.get("File"), ".motion3.json"))
                duration = data.get("Meta", {}).get("Duration")
                if data.get("Version") != 3 or not isinstance(duration, (float, int)) or not math.isfinite(duration) or not 0 < duration <= 600:
                    raise ValueError("invalid Cubism motion duration or version")
                if not isinstance(data.get("Curves"), list):
                    raise ValueError("missing Cubism motion curves")
                motion_data[group, index] = data
                if "Sound" in entry:
                    include(entry["Sound"], ".wav")
        for motion in spec.motions.values():
            if motion.index >= len(groups.get(motion.group, [])):
                raise ValueError("Cubism binding references a missing motion")
            if motion_data[motion.group, motion.index]["Meta"].get("Loop") is not (motion.kind == "idle"):
                raise ValueError("Cubism idle must loop; gestures and skills must not loop")
    return files
