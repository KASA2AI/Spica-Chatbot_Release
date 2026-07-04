"""W1 L3/L4 geometry goldens (WINDOWS_COMPAT_PLAN gate (d), P2-1/F3).

THREE dpr=1 layouts -- single screen, dual side-by-side, dual stacked -- pin that
the NEW multi-screen functions are byte-equal to the OLD semantics (uniform dpr
scaling == identity at dpr=1; screenAt == physical containment at dpr=1). The old
``selection_to_physical_rect`` itself stays pinned by tests/test_companion_bridge.py
(NOT touched here -- P1-1).

Plus synthetic dpr/origin unit tests for the new behaviour the old function could
not express (per-screen dpr + origin folding) -- W2 validates these on a real
per-monitor-DPI machine.
"""

import unittest

from ui.controllers.galgame_controller import (
    ScreenGeometry,
    physical_point_to_screen_index,
    selection_to_physical_rect,
    selection_to_physical_screen_rect,
)

# The three golden layouts (all dpr=1: logical == physical, the X11 norm).
SINGLE = [ScreenGeometry(logical=(0, 0, 1920, 1080), physical=(0, 0, 1920, 1080))]
DUAL_HORIZONTAL = [
    ScreenGeometry(logical=(0, 0, 1920, 1080), physical=(0, 0, 1920, 1080)),
    ScreenGeometry(logical=(1920, 0, 1920, 1080), physical=(1920, 0, 1920, 1080)),
]
DUAL_VERTICAL = [
    ScreenGeometry(logical=(0, 0, 1920, 1080), physical=(0, 0, 1920, 1080)),
    ScreenGeometry(logical=(0, 1080, 1920, 1080), physical=(0, 1080, 1920, 1080)),
]

SAMPLE_RECTS = [
    (0, 0, 100, 50),
    (10, 20, 30, 40),
    (500, 900, 640, 120),        # dialog-box shaped, first screen
    (1919, 1079, 1, 1),          # bottom-right corner of screen 1
    (2000, 100, 640, 200),       # second screen (horizontal layout)
    (2500, 500, 320, 240),
    (100, 1200, 640, 200),       # second screen (vertical layout)
    (500, 2000, 320, 100),
]


class SelectionGoldenDpr1Test(unittest.TestCase):
    """gate (d): new output == old semantics byte-for-byte on all three layouts."""

    def _assert_layout_identity(self, screens):
        for rect in SAMPLE_RECTS:
            old = selection_to_physical_rect(rect, 1.0)
            new = selection_to_physical_screen_rect(rect, screens)
            self.assertEqual(new, old, f"rect={rect} diverged from the old semantics")
            self.assertEqual(new, rect)  # dpr=1: both are the identity

    def test_single_screen_dpr1(self):
        self._assert_layout_identity(SINGLE)

    def test_dual_horizontal_dpr1(self):
        self._assert_layout_identity(DUAL_HORIZONTAL)

    def test_dual_vertical_dpr1(self):
        self._assert_layout_identity(DUAL_VERTICAL)


class ScreenIndexGoldenDpr1Test(unittest.TestCase):
    """L4: at dpr=1 physical == logical, so physical containment must agree with
    what screenAt(logical point) used to resolve."""

    def test_single_screen_dpr1(self):
        self.assertEqual(physical_point_to_screen_index((960, 540), SINGLE), 0)
        self.assertEqual(physical_point_to_screen_index((0, 0), SINGLE), 0)
        self.assertEqual(physical_point_to_screen_index((1919, 1079), SINGLE), 0)
        self.assertIsNone(physical_point_to_screen_index((1920, 540), SINGLE))  # off-edge
        self.assertIsNone(physical_point_to_screen_index((-1, 0), SINGLE))

    def test_dual_horizontal_dpr1(self):
        self.assertEqual(physical_point_to_screen_index((960, 540), DUAL_HORIZONTAL), 0)
        self.assertEqual(physical_point_to_screen_index((1920, 0), DUAL_HORIZONTAL), 1)
        self.assertEqual(physical_point_to_screen_index((2880, 540), DUAL_HORIZONTAL), 1)
        self.assertIsNone(physical_point_to_screen_index((3840, 540), DUAL_HORIZONTAL))

    def test_dual_vertical_dpr1(self):
        self.assertEqual(physical_point_to_screen_index((960, 540), DUAL_VERTICAL), 0)
        self.assertEqual(physical_point_to_screen_index((960, 1080), DUAL_VERTICAL), 1)
        self.assertEqual(physical_point_to_screen_index((0, 2159), DUAL_VERTICAL), 1)
        self.assertIsNone(physical_point_to_screen_index((960, 2160), DUAL_VERTICAL))


class SyntheticDprOriginTest(unittest.TestCase):
    """Injected mixed-dpr layout: what the OLD function could not express.
    Screen B: logical (1920,0,1280,720) at dpr=2 -> physical (1920,0,2560,1440)."""

    MIXED = [
        ScreenGeometry(logical=(0, 0, 1920, 1080), physical=(0, 0, 1920, 1080), device_pixel_ratio=1.0),
        ScreenGeometry(logical=(1920, 0, 1280, 720), physical=(1920, 0, 2560, 1440), device_pixel_ratio=2.0),
    ]

    def test_selection_on_hidpi_screen_folds_origin_and_dpr(self):
        # (2000,100) is 80 logical px into screen B -> 160 physical px past its
        # physical origin 1920; size doubles.
        self.assertEqual(
            selection_to_physical_screen_rect((2000, 100, 200, 150), self.MIXED),
            (2080, 200, 400, 300),
        )

    def test_selection_on_dpr1_screen_is_identity(self):
        self.assertEqual(
            selection_to_physical_screen_rect((10, 20, 30, 40), self.MIXED), (10, 20, 30, 40)
        )

    def test_dead_zone_topleft_and_centre_identity_fallback(self):
        # Top-left (1921,730) is in the dead zone below B (B is only 720 tall,
        # A ends at x=1920); the centre (1926,735) is too -> identity fallback.
        rect = (1921, 730, 10, 10)
        self.assertEqual(selection_to_physical_screen_rect(rect, self.MIXED), rect)

    def test_topleft_in_gap_falls_back_to_centre_match(self):
        # Gapped layout: top-left (150,10) is between the screens, the centre
        # (200,20) is on hidpi screen B -> B's dpr/origin folding applies.
        gapped = [
            ScreenGeometry(logical=(0, 0, 100, 100), physical=(0, 0, 100, 100), device_pixel_ratio=1.0),
            ScreenGeometry(logical=(200, 0, 100, 100), physical=(200, 0, 200, 200), device_pixel_ratio=2.0),
        ]
        self.assertEqual(
            selection_to_physical_screen_rect((150, 10, 100, 20), gapped),
            (200 + round((150 - 200) * 2.0), 0 + round((10 - 0) * 2.0), 200, 40),
        )

    def test_no_screens_identity_fallback(self):
        self.assertEqual(selection_to_physical_screen_rect((5, 6, 7, 8), []), (5, 6, 7, 8))

    def test_physical_index_on_mixed_dpr(self):
        # (2200,300) physical is inside B's PHYSICAL rect (1920..4480 x 0..1440).
        self.assertEqual(physical_point_to_screen_index((2200, 300), self.MIXED), 1)
        self.assertEqual(physical_point_to_screen_index((100, 100), self.MIXED), 0)
        self.assertIsNone(physical_point_to_screen_index((4480, 0), self.MIXED))


if __name__ == "__main__":
    unittest.main()
