"""Linux desktop game launcher (Phase 5).

Scans the user + system application dirs for ``.desktop`` entries, and launches a
game by desktop_entry / command / exe (manual_bind launches nothing). The actual
process spawn is a single injectable seam (``runner``) so tests never spawn a real
process. App dirs default to the XDG standard locations via ``Path.home()`` -- NO
``os.getenv`` (CLAUDE.md #4 / test_no_getenv); extra dirs can be injected.

Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable

from spica.galgame.models import LaunchProfile
from spica.ports.game_launcher import DesktopEntry, LaunchResult

logger = logging.getLogger(__name__)


class LinuxDesktopGameLauncher:
    name = "linux_desktop"

    def __init__(
        self,
        app_dirs: list[Path] | None = None,
        *,
        runner: Callable[..., Any] | None = None,
    ) -> None:
        self._app_dirs = (
            [Path(d) for d in app_dirs]
            if app_dirs is not None
            else [Path.home() / ".local" / "share" / "applications", Path("/usr/share/applications")]
        )
        # Injectable so tests don't spawn real processes; default fire-and-forget Popen.
        self._run = runner or subprocess.Popen

    # -- scan -----------------------------------------------------------------
    def scan_desktop_entries(self) -> list[DesktopEntry]:
        entries: list[DesktopEntry] = []
        seen: set[str] = set()
        for directory in self._app_dirs:
            try:
                files = sorted(directory.glob("*.desktop"))
            except OSError:
                continue
            for path in files:
                entry = self._parse_desktop(path)
                if entry is not None and entry.entry_id not in seen:
                    seen.add(entry.entry_id)
                    entries.append(entry)
        return entries

    def _parse_desktop(self, path: Path) -> DesktopEntry | None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        in_entry = False
        name: str | None = None
        exec_cmd: str | None = None
        no_display = False
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                in_entry = line == "[Desktop Entry]"
                continue
            if not in_entry or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key == "Name" and name is None:
                name = value
            elif key == "Exec" and exec_cmd is None:
                exec_cmd = value
            elif key == "NoDisplay" and value.lower() == "true":
                no_display = True
        if not exec_cmd or no_display:
            return None
        return DesktopEntry(entry_id=path.stem, name=name or path.stem, exec_cmd=exec_cmd, path=str(path))

    # -- launch ---------------------------------------------------------------
    def launch(self, profile: LaunchProfile) -> LaunchResult:
        if profile.launch_type == "manual_bind":
            return LaunchResult(ok=True)  # the game is already open; nothing to launch
        command = self._command_for(profile)
        if not command:
            return LaunchResult(ok=False, error=f"no launch command for launch_type={profile.launch_type!r}")
        try:
            proc = self._run(command, cwd=profile.working_dir or None)
            return LaunchResult(ok=True, pid=getattr(proc, "pid", None))
        except FileNotFoundError as exc:
            return LaunchResult(ok=False, error=f"executable not found: {exc}")
        except Exception as exc:  # noqa: BLE001 -- surfaced as ok=False, never raised to the binder
            logger.warning("game launch failed (%s): %s", profile.launch_type, exc, exc_info=True)
            return LaunchResult(ok=False, error=f"launch failed: {type(exc).__name__}: {exc}")

    def _command_for(self, profile: LaunchProfile) -> list[str]:
        if profile.launch_type == "command" and profile.command:
            return shlex.split(profile.command)
        if profile.launch_type == "exe" and profile.launch_target:
            return [profile.launch_target]
        if profile.launch_type == "desktop_entry" and profile.launch_target:
            entry = next(
                (e for e in self.scan_desktop_entries() if e.entry_id == profile.launch_target), None
            )
            if entry is None:
                return []
            return self._strip_field_codes(shlex.split(entry.exec_cmd))
        return []

    @staticmethod
    def _strip_field_codes(parts: list[str]) -> list[str]:
        # Drop desktop-entry field codes like %U %f %i (len-2 tokens starting with %).
        return [p for p in parts if not (len(p) == 2 and p.startswith("%"))]
