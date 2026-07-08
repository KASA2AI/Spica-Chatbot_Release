"""Phase 5: linux desktop launcher -- desktop-entry parse + launch (mocked spawn)."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from spica.adapters.game_launcher.linux_desktop import LinuxDesktopGameLauncher
from spica.galgame.models import LaunchProfile

DESKTOP_OK = "[Desktop Entry]\nType=Application\nName=My Game\nExec=/opt/mygame/run.sh %U\n"
DESKTOP_NODISPLAY = "[Desktop Entry]\nName=Hidden\nExec=/bin/true\nNoDisplay=true\n"
DESKTOP_NOEXEC = "[Desktop Entry]\nName=NoExec\n"


class _FakeRunner:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, command, cwd=None):
        self.calls.append((command, cwd))
        return SimpleNamespace(pid=4321)


class _RaisingRunner:
    def __init__(self, exc) -> None:
        self.exc = exc

    def __call__(self, command, cwd=None):
        raise self.exc


class DesktopScanTest(unittest.TestCase):
    def _dir(self, files: dict[str, str]) -> Path:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        for name, content in files.items():
            (directory / name).write_text(content, encoding="utf-8")
        return directory

    def test_scan_parses_entries_and_skips_invalid(self):
        directory = self._dir(
            {
                "mygame.desktop": DESKTOP_OK,
                "hidden.desktop": DESKTOP_NODISPLAY,
                "noexec.desktop": DESKTOP_NOEXEC,
                "notes.txt": "ignore me",
            }
        )
        launcher = LinuxDesktopGameLauncher(app_dirs=[directory])
        entries = launcher.scan_desktop_entries()
        self.assertEqual([e.entry_id for e in entries], ["mygame"])  # others skipped
        self.assertEqual(entries[0].name, "My Game")
        self.assertEqual(entries[0].exec_cmd, "/opt/mygame/run.sh %U")

    def test_missing_dir_is_ignored(self):
        launcher = LinuxDesktopGameLauncher(app_dirs=[Path("/nonexistent/xyz123")])
        self.assertEqual(launcher.scan_desktop_entries(), [])


class LaunchTest(unittest.TestCase):
    def test_command_launch(self):
        runner = _FakeRunner()
        launcher = LinuxDesktopGameLauncher(runner=runner)
        res = launcher.launch(
            LaunchProfile(launch_type="command", command="bottles-cli run -b X -p Game", working_dir="/tmp")
        )
        self.assertTrue(res.ok)
        self.assertEqual(res.pid, 4321)
        self.assertEqual(runner.calls[0], (["bottles-cli", "run", "-b", "X", "-p", "Game"], "/tmp"))

    def test_exe_launch(self):
        runner = _FakeRunner()
        launcher = LinuxDesktopGameLauncher(runner=runner)
        res = launcher.launch(LaunchProfile(launch_type="exe", launch_target="/opt/g/game"))
        self.assertTrue(res.ok)
        self.assertEqual(runner.calls[0][0], ["/opt/g/game"])

    def test_desktop_entry_launch_strips_field_codes(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        (directory / "mygame.desktop").write_text(DESKTOP_OK, encoding="utf-8")
        runner = _FakeRunner()
        launcher = LinuxDesktopGameLauncher(app_dirs=[directory], runner=runner)
        res = launcher.launch(LaunchProfile(launch_type="desktop_entry", launch_target="mygame"))
        self.assertTrue(res.ok)
        self.assertEqual(runner.calls[0][0], ["/opt/mygame/run.sh"])  # %U field code stripped

    def test_manual_bind_launches_nothing(self):
        runner = _FakeRunner()
        launcher = LinuxDesktopGameLauncher(runner=runner)
        res = launcher.launch(LaunchProfile(launch_type="manual_bind"))
        self.assertTrue(res.ok)
        self.assertEqual(runner.calls, [])

    def test_launch_failure_returns_not_ok(self):
        launcher = LinuxDesktopGameLauncher(runner=_RaisingRunner(FileNotFoundError("no exe")))
        res = launcher.launch(LaunchProfile(launch_type="command", command="missing-bin"))
        self.assertFalse(res.ok)
        self.assertIn("not found", res.error or "")

    def test_empty_command_returns_not_ok(self):
        launcher = LinuxDesktopGameLauncher(runner=_FakeRunner())
        res = launcher.launch(LaunchProfile(launch_type="command", command=None))
        self.assertFalse(res.ok)


if __name__ == "__main__":
    unittest.main()
