"""Window locator adapters (Phase 5)."""

from spica.adapters.window_locator.linux_x11 import LinuxX11WindowLocator
from spica.adapters.window_locator.windows_win32 import WindowsWin32WindowLocator

__all__ = ["LinuxX11WindowLocator", "WindowsWin32WindowLocator"]
