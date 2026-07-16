from __future__ import annotations

import os
import unittest
from collections import OrderedDict

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPixmap, QRegion
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from ui.qt_overlay import (
    CHARACTER_HIT_ALPHA_THRESHOLD,
    CHARACTER_HIT_MARGIN,
    OverlayWindow,
)


def _legacy_alpha_hit_region(image: QImage, origin: QPoint) -> QRegion:
    """Reference implementation retained in tests to pin pixel semantics."""
    width = image.width()
    height = image.height()
    margin = CHARACTER_HIT_MARGIN
    region = QRegion()

    def add_run(start: int, stop: int, y: int) -> None:
        nonlocal region
        left = max(0, start - margin)
        right = min(width, stop + margin)
        top = max(0, y - margin)
        bottom = min(height, y + margin + 1)
        if right > left and bottom > top:
            region = region.united(
                QRegion(
                    QRect(
                        origin.x() + left,
                        origin.y() + top,
                        right - left,
                        bottom - top,
                    )
                )
            )

    for y in range(height):
        run_start = -1
        for x in range(width):
            if image.pixelColor(x, y).alpha() > CHARACTER_HIT_ALPHA_THRESHOLD:
                if run_start < 0:
                    run_start = x
            elif run_start >= 0:
                add_run(run_start, x, y)
                run_start = -1
        if run_start >= 0:
            add_run(run_start, width, y)
    return region


def _fixture_image() -> QImage:
    image = QImage(36, 28, QImage.Format.Format_RGBA8888)
    image.fill(QColor(0, 0, 0, 0))
    # Include the exact threshold boundary plus separated opaque shapes.
    image.setPixelColor(1, 1, QColor(255, 255, 255, CHARACTER_HIT_ALPHA_THRESHOLD))
    image.setPixelColor(2, 1, QColor(255, 255, 255, CHARACTER_HIT_ALPHA_THRESHOLD + 1))
    for y in range(6, 14):
        for x in range(8, 18):
            image.setPixelColor(x, y, QColor(255, 255, 255, 255))
    for y in range(18, 25):
        for x in range(26, 34):
            image.setPixelColor(x, y, QColor(255, 255, 255, 180))
    return image


class _MaskWindow(QWidget):
    _alpha_hit_region = OverlayWindow._alpha_hit_region
    _character_hit_region = OverlayWindow._character_hit_region
    _character_pixmap_rect = OverlayWindow._character_pixmap_rect
    _scaled_pixmap_cache_key = OverlayWindow._scaled_pixmap_cache_key
    _remember_cache_value = OverlayWindow._remember_cache_value
    _cached_value = OverlayWindow._cached_value

    def __init__(self) -> None:
        super().__init__()
        self.resize(240, 180)
        self.character_label = QLabel(self)
        self.character_label.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom
        )
        self.character_label.setGeometry(20, 30, 80, 80)
        self.current_pixmap_cache_key = "fixture-a"
        self.ui_scale = 1.2
        self.character_scale = 1.0
        self.resize_origin_geometry = None
        self.character_hit_region_cache = OrderedDict()

    def _log_character_image_event(self, _event: str, **_fields: object) -> None:
        return None


class CharacterMaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_numpy_qt_region_is_exactly_equal_to_legacy_region(self) -> None:
        image = _fixture_image()
        for origin in (QPoint(), QPoint(13, 17), QPoint(-4, 9)):
            expected = _legacy_alpha_hit_region(image, origin)
            actual = OverlayWindow._alpha_hit_region(None, image, origin)
            self.assertEqual(actual, expected)

    def test_empty_and_fully_opaque_regions_preserve_legacy_semantics(self) -> None:
        transparent = QImage(12, 9, QImage.Format.Format_RGBA8888)
        transparent.fill(QColor(0, 0, 0, 0))
        opaque = QImage(12, 9, QImage.Format.Format_RGBA8888)
        opaque.fill(QColor(255, 255, 255, 255))
        for image in (transparent, opaque):
            self.assertEqual(
                OverlayWindow._alpha_hit_region(None, image, QPoint(5, 7)),
                _legacy_alpha_hit_region(image, QPoint(5, 7)),
            )

    def test_same_image_and_size_reuses_local_hit_region_after_label_move(self) -> None:
        window = _MaskWindow()
        image = _fixture_image()
        pixmap = QPixmap.fromImage(image)
        window.character_label.setPixmap(pixmap)

        calls = 0
        optimized = OverlayWindow._alpha_hit_region

        def counted(source: QImage, origin: QPoint) -> QRegion:
            nonlocal calls
            calls += 1
            return optimized(window, source, origin)

        window._alpha_hit_region = counted
        first = window._character_hit_region()
        window.character_label.move(37, 48)
        second = window._character_hit_region()

        self.assertEqual(calls, 1)
        self.assertEqual(second, first.translated(17, 18))
        window.close()

    def test_cache_key_separates_images_and_reuses_a_b_a(self) -> None:
        window = _MaskWindow()
        image_a = _fixture_image()
        image_b = QImage(36, 28, QImage.Format.Format_RGBA8888)
        image_b.fill(QColor(0, 0, 0, 0))
        image_b.setPixelColor(30, 3, QColor(255, 255, 255, 255))

        calls = 0
        optimized = OverlayWindow._alpha_hit_region

        def counted(source: QImage, origin: QPoint) -> QRegion:
            nonlocal calls
            calls += 1
            return optimized(window, source, origin)

        window._alpha_hit_region = counted
        window.character_label.setPixmap(QPixmap.fromImage(image_a))
        region_a = window._character_hit_region()
        window.current_pixmap_cache_key = "fixture-b"
        window.character_label.setPixmap(QPixmap.fromImage(image_b))
        region_b = window._character_hit_region()
        window.current_pixmap_cache_key = "fixture-a"
        window.character_label.setPixmap(QPixmap.fromImage(image_a))
        region_a_again = window._character_hit_region()

        self.assertEqual(calls, 2)
        self.assertNotEqual(region_a, region_b)
        self.assertEqual(region_a_again, region_a)
        window.close()

    def test_resize_in_progress_keeps_rectangular_fast_path(self) -> None:
        window = _MaskWindow()
        window.character_label.setPixmap(QPixmap.fromImage(_fixture_image()))
        window.resize_origin_geometry = window.geometry()

        def unexpected_scan(_source: QImage, _origin: QPoint) -> QRegion:
            raise AssertionError("alpha scan ran during interactive resize")

        window._alpha_hit_region = unexpected_scan
        region = window._character_hit_region()
        pixmap_rect = window._character_pixmap_rect(window.character_label.pixmap())
        expected = QRegion(
            pixmap_rect.adjusted(
                -CHARACTER_HIT_MARGIN,
                -CHARACTER_HIT_MARGIN,
                CHARACTER_HIT_MARGIN,
                CHARACTER_HIT_MARGIN,
            ).intersected(window.rect())
        )
        self.assertEqual(region, expected)
        window.close()

    def test_lru_helper_keeps_cache_bounded(self) -> None:
        cache: OrderedDict[int, object] = OrderedDict()
        owner = object()
        for key in range(5):
            OverlayWindow._remember_cache_value(owner, cache, key, object(), 3)
        self.assertEqual(list(cache), [2, 3, 4])
        OverlayWindow._cached_value(owner, cache, 2)
        self.assertEqual(list(cache), [3, 4, 2])


if __name__ == "__main__":
    unittest.main()
