"""Game launcher adapters (Phase 5)."""

from spica.adapters.game_launcher.linux_desktop import LinuxDesktopGameLauncher
from spica.adapters.game_launcher.windows_native import WindowsNativeGameLauncher

__all__ = ["LinuxDesktopGameLauncher", "WindowsNativeGameLauncher"]
