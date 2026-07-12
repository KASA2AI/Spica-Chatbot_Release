"""Fixed local assets for Config Studio."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path


BACKGROUND_SIZE = 10_281_151
BACKGROUND_SHA256 = "0a1116f8fb71f48156b0fd29d6beba9478bee6890b847c58b37c0d36c186eb68"


@dataclass(frozen=True, repr=False)
class BackgroundAssetLoad:
    content: bytes | None = field(repr=False)
    health_code: str | None

    def __repr__(self) -> str:
        state = "valid" if self.content is not None else "invalid"
        return f"BackgroundAssetLoad({state})"


class BackgroundAsset:
    """Load the one accepted image into memory after stdlib integrity checks."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def load(self) -> BackgroundAssetLoad:
        try:
            before = self._path.lstat()
            if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
                return _invalid()
            if before.st_size != BACKGROUND_SIZE:
                return _invalid()
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self._path, flags)
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode):
                    return _invalid()
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    return _invalid()
                chunks: list[bytes] = []
                remaining = BACKGROUND_SIZE + 1
                while remaining:
                    chunk = os.read(descriptor, min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                content = b"".join(chunks)
            finally:
                os.close(descriptor)
        except (OSError, ValueError):
            return _invalid()
        if len(content) != BACKGROUND_SIZE:
            return _invalid()
        if hashlib.sha256(content).hexdigest() != BACKGROUND_SHA256:
            return _invalid()
        return BackgroundAssetLoad(content=content, health_code=None)

    def __repr__(self) -> str:
        return "BackgroundAsset(<fixed path>)"


def _invalid() -> BackgroundAssetLoad:
    return BackgroundAssetLoad(content=None, health_code="BACKGROUND_ASSET_INVALID")


@dataclass(frozen=True, repr=False)
class StaticUiAssets:
    index_html: bytes = field(repr=False)
    stylesheet: bytes = field(repr=False)
    javascript: bytes = field(repr=False)
    background: BackgroundAssetLoad = field(repr=False)

    def __repr__(self) -> str:
        return "StaticUiAssets(<fixed local assets>)"


def load_static_ui_assets(ui_root: Path | None = None) -> StaticUiAssets:
    root = ui_root or Path(__file__).resolve().parents[2] / "ui" / "config_studio"
    return StaticUiAssets(
        index_html=_read_fixed_regular(root / "index.html", max_bytes=512 * 1024),
        stylesheet=_read_fixed_regular(root / "studio.css", max_bytes=1024 * 1024),
        javascript=_read_fixed_regular(root / "studio.js", max_bytes=1024 * 1024),
        background=BackgroundAsset(
            root / "assets" / "0125_4_CG_GE01_0202_waifu2x_2x_3n_png.png"
        ).load(),
    )


def _read_fixed_regular(path: Path, *, max_bytes: int) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
        raise RuntimeError("fixed Config Studio asset is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (before.st_dev, before.st_ino):
            raise RuntimeError("fixed Config Studio asset changed during load")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise RuntimeError("fixed Config Studio asset exceeds its budget")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)
