"""W1 Windows stub adapters: Linux clean-import guard + graceful degradation +
A3 ``format_native_window_id`` contract (WINDOWS_COMPAT_PLAN §3.5 / §5-W1).

The module-level imports below ARE the clean-import check: any module-level
``ctypes.windll`` / win32 API access in the windows adapters explodes right here
on Linux (§3.5 -- the mechanism that keeps W2's real implementation honest too).
"""

import unittest

# Clean-import guard: importing + instantiating on Linux must just work.
from spica.adapters.game_launcher.windows_native import WindowsNativeGameLauncher
from spica.adapters.window_locator.linux_x11 import LinuxX11WindowLocator, _same_window
from spica.adapters.window_locator.windows_win32 import WindowsWin32WindowLocator
from spica.galgame.models import LaunchProfile, WindowMatchRule
from spica.ports.game_launcher import GameLauncherPort
from spica.ports.window_locator import WindowLocatorPort


class WindowsWin32StubTest(unittest.TestCase):
    def test_satisfies_port_protocol(self):
        self.assertIsInstance(WindowsWin32WindowLocator(), WindowLocatorPort)

    def test_enumerate_degrades_structurally(self):
        # Never raises, never a silent empty list: available=False + reason_code.
        result = WindowsWin32WindowLocator().enumerate_windows()
        self.assertEqual(result.windows, [])
        self.assertFalse(result.available)
        self.assertEqual(result.reason_code, "WIN32_LOCATOR_PENDING")
        self.assertTrue(result.reason)

    def test_geometry_and_safety_graceful(self):
        locator = WindowsWin32WindowLocator()
        self.assertIsNone(locator.get_window_geometry("12345"))
        safety = locator.check_safety("12345", WindowMatchRule(title_keywords=["x"]))
        self.assertFalse(safety.ok)
        self.assertEqual(safety.reason_code, "SAFETY_PROBE_FAILED")


class WindowsNativeLauncherStubTest(unittest.TestCase):
    def test_satisfies_port_protocol(self):
        self.assertIsInstance(WindowsNativeGameLauncher(), GameLauncherPort)

    def test_scan_returns_empty(self):
        self.assertEqual(WindowsNativeGameLauncher().scan_desktop_entries(), [])

    def test_manual_bind_is_usable_today(self):
        result = WindowsNativeGameLauncher().launch(LaunchProfile(launch_type="manual_bind"))
        self.assertTrue(result.ok)

    def test_exe_and_command_pending_w2(self):
        launcher = WindowsNativeGameLauncher()
        for launch_type in ("exe", "command"):
            result = launcher.launch(
                LaunchProfile(launch_type=launch_type, launch_target="C:\\game\\a.exe", command="C:\\game\\a.exe")
            )
            self.assertFalse(result.ok)
            self.assertIn("W2", result.error or "")

    def test_desktop_entry_permanently_unsupported(self):
        result = WindowsNativeGameLauncher().launch(
            LaunchProfile(launch_type="desktop_entry", launch_target="foo")
        )
        self.assertFalse(result.ok)
        self.assertIn("desktop_entry", result.error or "")


class FormatNativeWindowIdContractTest(unittest.TestCase):
    """A3 contract: the native winId() int exists ONLY as this method's argument;
    each lane formats it into ITS OWN window_id string form."""

    def test_x11_formats_hex_byte_identical_to_legacy_inline(self):
        native = 0x5000003
        formatted = LinuxX11WindowLocator().format_native_window_id(native)
        # Byte-identical to the string ui/qt_overlay.py used to build inline
        # (`hex(int(self.winId()))`), so the focus exemption is unchanged.
        self.assertEqual(formatted, hex(native))
        self.assertEqual(formatted, "0x5000003")

    def test_x11_formatted_id_matches_padded_active_window(self):
        # xprop pads ids (0x05000003); _same_window compares hex-tolerant, and
        # the formatted overlay id must land on that tolerant path.
        formatted = LinuxX11WindowLocator().format_native_window_id(0x5000003)
        self.assertTrue(_same_window("0x05000003", formatted))

    def test_x11_focus_exemption_fires_with_formatted_overlay_id(self):
        # End-to-end through check_safety with injected probes: focus is on the
        # overlay (padded id from xprop) -> exempt, ok=True.
        locator = LinuxX11WindowLocator(
            runner=lambda cmd: "0xdead 0 100 200 300 400 host game\n"
            if cmd[:2] == ["wmctrl", "-lG"]
            else (_ for _ in ()).throw(FileNotFoundError()),
            active_window_probe=lambda: "0x05000003",
            window_title_probe=lambda _wid: None,
            minimized_probe=lambda _wid: False,
        )
        overlay_id = locator.format_native_window_id(0x5000003)
        result = locator.check_safety(
            "0xdead", WindowMatchRule(title_keywords=["nomatch"]), overlay_window_id=overlay_id
        )
        self.assertTrue(result.ok)

    def test_windows_lane_formats_decimal_hwnd(self):
        self.assertEqual(WindowsWin32WindowLocator().format_native_window_id(83886083), "83886083")


if __name__ == "__main__":
    unittest.main()
