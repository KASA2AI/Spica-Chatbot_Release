"""Portable animation data for the voice-only desktop character."""

from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

FloatingState = Literal['idle', 'listening', 'speaking', 'dragged', 'click', 'dizzy',
                        'petting', 'thinking', 'peek', 'notice', 'sleep', 'welcome', 'easter_egg']


class FloatingAnimation(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    file: str
    duration_ms: int = Field(gt=0)
    frames: int = Field(gt=0)
    start_frame: int = Field(default=0, ge=0)
    pivot_px: tuple[FiniteFloat, FiniteFloat] | None = None
    source: str | None = None
    keyframes: tuple[str, ...] = ()


class FloatingIdleAction(FloatingAnimation):
    weight: int = Field(default=1, gt=0)


class FloatingCharacter(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    name: str = ''
    canvas: tuple[int, int]
    suggested_display_px: int = Field(default=96, ge=48, le=256)
    fps: int = Field(ge=1, le=60)
    loop: bool = True
    states: dict[FloatingState, FloatingAnimation]
    poster: str
    sheet: str | None = None
    flustered: str | None = None
    dizzy_held: str | None = None
    idle_actions: dict[str, FloatingIdleAction] = Field(default_factory=dict)
    idle_interval_ms: tuple[int, int] = (30000, 45000)

    def animations(self) -> tuple[FloatingAnimation, ...]:
        return (*self.states.values(), *self.idle_actions.values())

    @model_validator(mode='after')
    def geometry(self):
        if not all(0 < side <= 2048 for side in self.canvas):
            raise ValueError('floating animation canvas exceeds the supported frame size')
        if 'idle' not in self.states:
            raise ValueError('floating character needs an idle animation')
        minimum, maximum = self.idle_interval_ms
        if not 0 < minimum <= maximum <= 2**31 - 1:
            raise ValueError('floating idle interval must be a positive ordered timer range')
        if any(not name.strip() for name in self.idle_actions):
            raise ValueError('floating idle actions need a non-empty ID')
        for animation in self.animations():
            if animation.start_frame >= animation.frames:
                raise ValueError('floating animation start frame is outside the animation')
            if animation.pivot_px is not None and not all(
                0 <= coordinate <= size for coordinate, size in zip(animation.pivot_px, self.canvas)
            ):
                raise ValueError('floating drag pivot is outside the canvas')
        return self

    def files(self) -> set[str]:
        paths = {self.poster}
        if self.sheet:
            paths.add(self.sheet)
        paths.update(path for path in (self.flustered, self.dizzy_held) if path)
        for animation in self.animations():
            paths.add(animation.file)
            paths.update(animation.keyframes)
            if animation.source:
                paths.add(animation.source)
        return paths


def load_floating_character(path: Path) -> FloatingCharacter:
    if path.stat().st_size > 128 * 1024:
        raise ValueError('floating character configuration is too large')
    return FloatingCharacter.model_validate_json(path.read_bytes())


def floating_files(root: Path, relative: str) -> set[str]:
    """Resolve every reference through the existing package path authority."""
    from spica.core.character_manifest import package_file

    spec = load_floating_character(package_file(root, relative))
    directory = PurePosixPath(relative).parent
    paths = {relative}
    for value in spec.files():
        # Validate before joining: PurePosixPath would normalize away `./`.
        package_file(root / directory, value)
        joined = (directory / value).as_posix()
        package_file(root, joined)
        paths.add(joined)
    return paths


def validate_floating_images(root: Path, relative: str) -> None:
    from PIL import Image
    from spica.core.character_manifest import package_file

    spec = load_floating_character(package_file(root, relative))
    directory = root / PurePosixPath(relative).parent
    for animation in spec.animations():
        with Image.open(package_file(directory, animation.file)) as image:
            if image.size != spec.canvas or getattr(image, 'n_frames', 1) != animation.frames:
                raise ValueError('floating animation dimensions or frame count do not match its declaration')
            if image.format not in {'WEBP', 'GIF'}:
                raise ValueError('floating animation must use WebP or GIF')
            duration = 0
            for frame in range(animation.frames):
                image.seek(frame)
                image.load()  # Sequential validation, never retain the decoded frames.
                delay = image.info.get('duration', 0)
                if delay <= 0:
                    raise ValueError('floating animation timing needs positive frame delays')
                duration += delay
            # WebP/GIF can coalesce identical timeline frames into one longer
            # encoded frame. Playback uses these delays, not frames / fps.
            if duration != animation.duration_ms:
                raise ValueError('floating animation timing does not match its declaration')
    for relative_image in (spec.poster, spec.flustered, spec.dizzy_held):
        if relative_image is not None:
            with Image.open(package_file(directory, relative_image)) as image:
                if image.size != spec.canvas:
                    raise ValueError('floating static image dimensions do not match its declaration')
                image.verify()
