"""W1 platform-lane factories + fold (WINDOWS_COMPAT_PLAN §3.3 / §3.4).

``test_build_ocr_adapter`` 形制. The zero-diff pin is the REAL "Linux still
constructs the Linux adapters" guard (Layer A zero-diff is necessary but not
sufficient): ``build_*("linux")`` must construct exactly today's three classes.
Unknown lanes FAIL LOUD -- unlike build_ocr_adapter's graceful fallback, a
mis-selected platform must never silently degrade onto the other platform's
probes (factory docstrings pin the rationale).
"""

import unittest

from spica.adapters.game_launcher import LinuxDesktopGameLauncher, WindowsNativeGameLauncher
from spica.adapters.screen_capture import MssScreenCapture
from spica.adapters.window_locator import LinuxX11WindowLocator, WindowsWin32WindowLocator
from spica.host.agent_assembly import (
    build_game_launcher,
    build_screen_capture,
    build_window_locator,
    fold_platform,
)


class FoldPlatformTest(unittest.TestCase):
    """The four injected pins (linux / win32 / explicit / darwin-raises) -- same
    assertions as the Layer B pins in test_resolved_config_equivalence.py; both
    homes are mandated by §3.4 + gate (c)."""

    def test_auto_linux_host(self):
        self.assertEqual(fold_platform("auto", "linux"), "linux")

    def test_auto_win32_host(self):
        self.assertEqual(fold_platform("auto", "win32"), "windows")

    def test_explicit_ignores_host(self):
        self.assertEqual(fold_platform("windows", "linux"), "windows")
        self.assertEqual(fold_platform("linux", "win32"), "linux")

    def test_auto_unknown_host_raises(self):
        with self.assertRaises(ValueError):
            fold_platform("auto", "darwin")
        with self.assertRaises(ValueError):
            fold_platform("auto", "cygwin")

    def test_illegal_os_cfg_raises(self):
        # Backstop only -- config callers die earlier at the schema Literal.
        with self.assertRaises(ValueError):
            fold_platform("macos", "linux")


class BuildPlatformAdaptersTest(unittest.TestCase):
    def test_linux_lane_is_todays_three_classes(self):
        # Zero-diff pin (gate (c)): the linux lane == the formerly hardcoded
        # constructions in build_agent_services, byte-equivalent.
        locator = build_window_locator("linux")
        capture = build_screen_capture("linux")
        launcher = build_game_launcher("linux")
        self.assertIsInstance(locator, LinuxX11WindowLocator)
        self.assertIsInstance(capture, MssScreenCapture)
        self.assertIsInstance(launcher, LinuxDesktopGameLauncher)
        self.assertEqual(locator.name, "linux_x11")
        self.assertEqual(capture.name, "mss")
        self.assertEqual(launcher.name, "linux_desktop")

    def test_windows_lane_returns_windows_stubs(self):
        locator = build_window_locator("windows")
        capture = build_screen_capture("windows")
        launcher = build_game_launcher("windows")
        self.assertIsInstance(locator, WindowsWin32WindowLocator)
        # mss is cross-platform: BOTH lanes share the one adapter (no new class).
        self.assertIsInstance(capture, MssScreenCapture)
        self.assertIsInstance(launcher, WindowsNativeGameLauncher)

    def test_unknown_lane_fails_loud(self):
        for factory in (build_window_locator, build_screen_capture, build_game_launcher):
            with self.assertRaises(ValueError):
                factory("darwin")
            with self.assertRaises(ValueError):
                factory("")


if __name__ == "__main__":
    unittest.main()
