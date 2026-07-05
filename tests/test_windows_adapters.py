"""W2 Windows adapters: real Win32 locator + native launcher contract tests
(WINDOWS_COMPAT_PLAN §5-W2; supersedes the W1 stub tests).

The module-level imports below ARE the Linux clean-import check (§3.5): any
module-level ``ctypes.windll`` / win32 API access in the windows adapters
explodes right here on Linux. The Win32 call layer is an injectable seam
(``api=``), so the FULL locator contract runs on Linux against a fake; the
launcher's spawn seam (``runner=``) never starts a real process.
"""

import unittest

# Clean-import guard: importing + instantiating on Linux must just work.
from spica.adapters.game_launcher.windows_native import WindowsNativeGameLauncher
from spica.adapters.window_locator.linux_x11 import LinuxX11WindowLocator, _same_window
from spica.adapters.window_locator.windows_win32 import WindowsWin32WindowLocator, _same_hwnd
from spica.galgame.models import LaunchProfile, WindowMatchRule
from spica.ports.game_launcher import GameLauncherPort
from spica.ports.window_locator import WindowLocatorPort


class _FakeWin32Api:
    """Duck-typed stand-in for _RealWin32Api (the injectable seam)."""

    def __init__(
        self,
        windows: dict[int, dict] | None = None,
        foreground: int = 0,
        raise_on_enum: Exception | None = None,
    ) -> None:
        # windows: hwnd -> {title, visible, rect, iconic, pid}
        self.windows = windows or {}
        self.foreground = foreground
        self.raise_on_enum = raise_on_enum

    def list_top_level_windows(self):
        if self.raise_on_enum is not None:
            raise self.raise_on_enum
        return list(self.windows)

    def window_title(self, hwnd):
        return self.windows.get(hwnd, {}).get("title", "")

    def is_window_visible(self, hwnd):
        return self.windows.get(hwnd, {}).get("visible", False)

    def window_rect(self, hwnd):
        return self.windows.get(hwnd, {}).get("rect")

    def foreground_window(self):
        return self.foreground

    def is_iconic(self, hwnd):
        return self.windows.get(hwnd, {}).get("iconic", False)

    def window_pid(self, hwnd):
        return self.windows.get(hwnd, {}).get("pid")


_GAME = 83886083  # arbitrary hwnd; decimal window_id "83886083"
_RULE = WindowMatchRule(title_keywords=["anemoi"])


def _game_api(**overrides) -> _FakeWin32Api:
    spec = {"title": "anemoi Day 1", "visible": True, "rect": (100, 200, 740, 680), "pid": 4242}
    spec.update(overrides)
    return _FakeWin32Api(windows={_GAME: spec}, foreground=_GAME)


class Win32LocatorEnumerationTest(unittest.TestCase):
    def test_satisfies_port_protocol(self):
        self.assertIsInstance(WindowsWin32WindowLocator(), WindowLocatorPort)

    def test_enumerates_visible_titled_windows_decimal_ids(self):
        api = _FakeWin32Api(
            windows={
                _GAME: {"title": "anemoi Day 1", "visible": True, "pid": 4242},
                111: {"title": "hidden helper", "visible": False},  # invisible -> filtered
                222: {"title": "   ", "visible": True},  # untitled -> filtered
            }
        )
        result = WindowsWin32WindowLocator(api=api).enumerate_windows()
        self.assertTrue(result.available)
        self.assertEqual(len(result.windows), 1)
        candidate = result.windows[0]
        self.assertEqual(candidate.window_id, "83886083")  # DECIMAL str(hwnd) -- W2/A3
        self.assertEqual(candidate.title, "anemoi Day 1")
        self.assertEqual(candidate.pid, 4242)
        self.assertTrue(candidate.visible)

    def test_unavailable_on_linux_degrades_structurally(self):
        # No injected api on a Linux host: the lazy real-api load fails ->
        # available=False + reason_code, NEVER a raise / silent empty.
        result = WindowsWin32WindowLocator().enumerate_windows()
        self.assertEqual(result.windows, [])
        self.assertFalse(result.available)
        self.assertEqual(result.reason_code, "WIN32_UNAVAILABLE")
        self.assertTrue(result.reason)

    def test_enum_blowup_degrades_structurally(self):
        api = _FakeWin32Api(raise_on_enum=RuntimeError("boom"))
        result = WindowsWin32WindowLocator(api=api).enumerate_windows()
        self.assertFalse(result.available)
        self.assertEqual(result.reason_code, "ENUMERATION_FAILED")
        self.assertIn("boom", result.reason)


class Win32LocatorGeometryTest(unittest.TestCase):
    def test_rect_converts_to_geometry(self):
        geometry = WindowsWin32WindowLocator(api=_game_api()).get_window_geometry(str(_GAME))
        self.assertIsNotNone(geometry)
        # GetWindowRect gives (left, top, right, bottom) -> x/y/width/height.
        self.assertEqual((geometry.x, geometry.y, geometry.width, geometry.height), (100, 200, 640, 480))

    def test_gone_window_and_bad_id_return_none(self):
        locator = WindowsWin32WindowLocator(api=_game_api(rect=None))
        self.assertIsNone(locator.get_window_geometry(str(_GAME)))  # rect call failed -> gone
        self.assertIsNone(locator.get_window_geometry("not-a-hwnd"))
        self.assertIsNone(WindowsWin32WindowLocator().get_window_geometry(str(_GAME)))  # no api on Linux


class Win32LocatorSafetyTest(unittest.TestCase):
    def test_window_gone(self):
        result = WindowsWin32WindowLocator(api=_game_api(rect=None)).check_safety(str(_GAME), _RULE)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "WINDOW_GONE")

    def test_minimized(self):
        result = WindowsWin32WindowLocator(api=_game_api(iconic=True)).check_safety(str(_GAME), _RULE)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "WINDOW_MINIMIZED")

    def test_no_foreground_is_conservative_probe_failure(self):
        api = _game_api()
        api.foreground = 0
        result = WindowsWin32WindowLocator(api=api).check_safety(str(_GAME), _RULE)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "SAFETY_PROBE_FAILED")

    def test_no_api_on_linux_is_probe_failure_not_gone(self):
        result = WindowsWin32WindowLocator().check_safety(str(_GAME), _RULE)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "SAFETY_PROBE_FAILED")

    def test_overlay_focus_exemption_via_formatted_hwnd(self):
        # §7.4: foreground is the Spica overlay -> exempt. The overlay id arrives
        # in the lane's own decimal form via format_native_window_id (A3).
        overlay_hwnd = 72340
        api = _game_api()
        api.windows[overlay_hwnd] = {"title": "Spica", "visible": True, "rect": (0, 0, 10, 10)}
        api.foreground = overlay_hwnd
        locator = WindowsWin32WindowLocator(api=api)
        overlay_id = locator.format_native_window_id(overlay_hwnd)
        # Rule deliberately does NOT match "Spica": ok must come from the id exemption.
        result = locator.check_safety(str(_GAME), _RULE, overlay_window_id=overlay_id)
        self.assertTrue(result.ok)

    def test_foreground_title_keyword_match_is_safe(self):
        # §17.3: focus judged by the FOREGROUND window's TITLE, not id equality --
        # a second engine window with a different hwnd but matching title is "on the game".
        inner = 999
        api = _game_api()
        api.windows[inner] = {"title": "anemoi - render", "visible": True, "rect": (0, 0, 1, 1)}
        api.foreground = inner
        result = WindowsWin32WindowLocator(api=api).check_safety(str(_GAME), _RULE)
        self.assertTrue(result.ok)

    def test_unrelated_foreground_pauses(self):
        other = 555
        api = _game_api()
        api.windows[other] = {"title": "Notepad", "visible": True, "rect": (0, 0, 1, 1)}
        api.foreground = other
        result = WindowsWin32WindowLocator(api=api).check_safety(str(_GAME), _RULE)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "WINDOW_NOT_FOCUSED")

    def test_probe_blowup_is_conservative(self):
        class _ExplodingApi(_FakeWin32Api):
            def is_iconic(self, hwnd):
                raise RuntimeError("probe boom")

        api = _ExplodingApi(windows={_GAME: {"title": "anemoi", "visible": True, "rect": (0, 0, 1, 1)}})
        result = WindowsWin32WindowLocator(api=api).check_safety(str(_GAME), _RULE)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "SAFETY_PROBE_FAILED")


class WindowsNativeLauncherTest(unittest.TestCase):
    def _recording_runner(self):
        calls = []

        class _Proc:
            pid = 1234

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return _Proc()

        return calls, runner

    def test_satisfies_port_protocol(self):
        self.assertIsInstance(WindowsNativeGameLauncher(), GameLauncherPort)

    def test_scan_returns_empty(self):
        self.assertEqual(WindowsNativeGameLauncher().scan_desktop_entries(), [])

    def test_manual_bind_launches_nothing(self):
        calls, runner = self._recording_runner()
        result = WindowsNativeGameLauncher(runner=runner).launch(LaunchProfile(launch_type="manual_bind"))
        self.assertTrue(result.ok)
        self.assertEqual(calls, [])

    def test_desktop_entry_permanently_unsupported(self):
        calls, runner = self._recording_runner()
        result = WindowsNativeGameLauncher(runner=runner).launch(
            LaunchProfile(launch_type="desktop_entry", launch_target="foo")
        )
        self.assertFalse(result.ok)
        self.assertIn("desktop_entry", result.error or "")
        self.assertEqual(calls, [])

    def test_exe_spawns_list_of_one_with_cwd(self):
        calls, runner = self._recording_runner()
        target = r"C:\Games\A B\game.exe"
        result = WindowsNativeGameLauncher(runner=runner).launch(
            LaunchProfile(launch_type="exe", launch_target=target, working_dir=r"C:\Games\A B")
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.pid, 1234)
        (command, kwargs), = calls
        # List-of-one: Popen quotes it, so the spaced path survives un-split.
        self.assertEqual(command, [target])
        self.assertEqual(kwargs.get("cwd"), r"C:\Games\A B")

    def test_command_passes_whole_string_never_shlex(self):
        # P3-1: the command string reaches Popen WHOLE -- backslashes and quoted
        # spaced path intact (POSIX shlex.split would eat C:\ backslashes).
        calls, runner = self._recording_runner()
        command_line = r'"C:\Games\A B\game.exe" --fullscreen'
        result = WindowsNativeGameLauncher(runner=runner).launch(
            LaunchProfile(launch_type="command", command=command_line)
        )
        self.assertTrue(result.ok)
        (command, kwargs), = calls
        self.assertIsInstance(command, str)
        self.assertEqual(command, command_line)
        self.assertIn(r"C:\Games\A B\game.exe", command)
        self.assertEqual(kwargs.get("cwd"), None)

    def test_missing_target_or_command_fails_readably(self):
        launcher = WindowsNativeGameLauncher(runner=lambda *a, **k: None)
        for profile in (
            LaunchProfile(launch_type="exe", launch_target=None),
            LaunchProfile(launch_type="command", command=None),
            LaunchProfile(launch_type="totally-unknown"),
        ):
            result = launcher.launch(profile)
            self.assertFalse(result.ok)
            self.assertIn(profile.launch_type, result.error or "")

    def test_file_not_found_becomes_error_result(self):
        def runner(command, **kwargs):
            raise FileNotFoundError(r"C:\nope\game.exe")

        result = WindowsNativeGameLauncher(runner=runner).launch(
            LaunchProfile(launch_type="exe", launch_target=r"C:\nope\game.exe")
        )
        self.assertFalse(result.ok)
        self.assertIn("not found", result.error or "")

    def test_runner_blowup_never_raises(self):
        def runner(command, **kwargs):
            raise OSError("access denied")

        result = WindowsNativeGameLauncher(runner=runner).launch(
            LaunchProfile(launch_type="command", command="game.exe")
        )
        self.assertFalse(result.ok)
        self.assertIn("access denied", result.error or "")


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

    def test_same_hwnd_compares_numerically(self):
        self.assertTrue(_same_hwnd("83886083", "83886083"))
        self.assertTrue(_same_hwnd(" 83886083 ", "83886083"))  # int() tolerates whitespace
        self.assertFalse(_same_hwnd("83886083", "1"))
        self.assertFalse(_same_hwnd(None, "1"))
        self.assertFalse(_same_hwnd("83886083", None))


if __name__ == "__main__":
    unittest.main()
