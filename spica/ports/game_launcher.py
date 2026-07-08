"""Game launcher capability port (Phase 5).

Launching a galgame goes through this port -- the LLM NEVER execs a command
directly (CLAUDE.md #1.9). v1 supports Ubuntu desktop-entry scan + command/exe
launch + manual_bind (the game is already open; nothing to launch). Windows is a
``LaunchProfile.platform == "windows"`` branch left unimplemented (a future
adapter under the same ``spica/adapters/`` tree -- no parallel ``spica/platform/``).

Qt-free (CLAUDE.md #1): adapters never import Qt nor pop dialogs; user choices
flow through the companion event channel + GameBinder methods (ui/ layer).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from spica.galgame.models import LaunchProfile


@dataclass(frozen=True)
class DesktopEntry:
    """A discovered launchable desktop application (the user picks one to add a game)."""

    entry_id: str
    name: str
    exec_cmd: str
    path: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"entry_id": self.entry_id, "name": self.name, "exec_cmd": self.exec_cmd, "path": self.path}


@dataclass(frozen=True)
class LaunchResult:
    ok: bool
    pid: int | None = None
    error: str | None = None


@runtime_checkable
class GameLauncherPort(Protocol):
    def scan_desktop_entries(self) -> list[DesktopEntry]:
        """Scan the user + system application dirs for launchable desktop entries."""
        ...

    def launch(self, profile: LaunchProfile) -> LaunchResult:
        """Launch per the profile. ``launch_type == "manual_bind"`` launches nothing
        (returns ok). A failure is returned as ``LaunchResult(ok=False, error=...)``,
        NOT raised -- the binder turns it into a user-facing ``galgame_bind_failed``."""
        ...
