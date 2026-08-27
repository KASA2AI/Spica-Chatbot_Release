"""Qt adapter that resolves one logical sprite into one display-ready pixmap."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Hashable
from dataclasses import dataclass
import logging
from pathlib import Path

from PySide6.QtGui import QPainter, QPixmap

from agent_tools.visual.layered_assets import (
    LayeredAssetError,
    LayeredAssetManifest,
    load_layered_manifest,
)


DEFAULT_CACHE_BYTES = 128 * 1024 * 1024
DEFAULT_BUNDLE_ROOT = Path(__file__).resolve().parents[1] / "spica_data" / "derived" / "current"
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResolvedSprite:
    pixmap: QPixmap
    cache_key: str
    cache_hit: bool
    layered: bool


class PixmapByteCache:
    """Small QPixmap LRU shared by raw/composed and scaled display caches."""

    def __init__(self, *, max_bytes: int) -> None:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("pixmap cache byte limit must be positive")
        self._max_bytes = max_bytes
        self._cached_bytes = 0
        self._entries: OrderedDict[Hashable, tuple[QPixmap, int]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: Hashable) -> QPixmap | None:
        cached = self._entries.pop(key, None)
        if cached is None:
            return None
        self._entries[key] = cached
        return cached[0]

    def put(self, key: Hashable, pixmap: QPixmap) -> bool:
        decoded_bytes = max(0, pixmap.width()) * max(0, pixmap.height()) * 4
        if decoded_bytes > self._max_bytes:
            return False
        previous = self._entries.pop(key, None)
        if previous is not None:
            self._cached_bytes -= previous[1]
        while self._entries and self._cached_bytes + decoded_bytes > self._max_bytes:
            _, (_, evicted_bytes) = self._entries.popitem(last=False)
            self._cached_bytes -= evicted_bytes
        self._entries[key] = (pixmap, decoded_bytes)
        self._cached_bytes += decoded_bytes
        return True

    def clear(self) -> None:
        self._entries.clear()
        self._cached_bytes = 0


class LocalSpriteStore:
    """Hide bundle lookup, composition, legacy fallback, and byte-bounded LRU."""

    def __init__(
        self,
        *,
        bundle_root: Path = DEFAULT_BUNDLE_ROOT,
        max_bytes: int = DEFAULT_CACHE_BYTES,
    ) -> None:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("sprite cache byte limit must be positive")
        self._cache = PixmapByteCache(max_bytes=max_bytes)
        self._manifest = self._load_manifest(bundle_root)

    @property
    def entry_count(self) -> int:
        return len(self._cache)

    def resolve(
        self,
        logical_path: str | Path,
        *,
        identity: str | None = None,
    ) -> ResolvedSprite | None:
        path = Path(logical_path)
        cache_identity = identity or str(path.absolute())
        final_key = f"sprite:{cache_identity}"
        cached = self._take(final_key)
        if cached is not None:
            return ResolvedSprite(
                cached,
                cache_identity,
                True,
                self._has_recipe(path.stem),
            )

        recipe = self._manifest.resolve(path.stem) if self._manifest is not None else None
        if recipe is not None:
            body = self._load_layer(recipe.body_path)
            face = self._load_layer(recipe.face_path)
            if body is not None and face is not None:
                composed = QPixmap(body)
                painter = QPainter(composed)
                painter.drawPixmap(recipe.x, recipe.y, face)
                painter.end()
                if not composed.isNull():
                    self._remember(final_key, composed)
                    return ResolvedSprite(composed, cache_identity, False, True)

        legacy = QPixmap(str(path))
        if legacy.isNull():
            return None
        self._remember(final_key, legacy)
        return ResolvedSprite(legacy, cache_identity, False, False)

    def _has_recipe(self, sprite_id: str) -> bool:
        return self._manifest is not None and self._manifest.resolve(sprite_id) is not None

    def _load_layer(self, relative_path: str) -> QPixmap | None:
        if self._manifest is None:
            return None
        cache_key = f"asset:{relative_path}"
        cached = self._take(cache_key)
        if cached is not None:
            return cached
        pixmap = QPixmap(str(self._manifest.root / relative_path))
        if pixmap.isNull():
            return None
        self._remember(cache_key, pixmap)
        return pixmap

    def _take(self, key: str) -> QPixmap | None:
        return self._cache.get(key)

    def _remember(self, key: str, pixmap: QPixmap) -> None:
        self._cache.put(key, pixmap)

    @staticmethod
    def _load_manifest(bundle_root: Path) -> LayeredAssetManifest | None:
        try:
            return load_layered_manifest(bundle_root)
        except LayeredAssetError as error:
            _LOGGER.info("layered sprite bundle inactive: %s", error)
            return None
