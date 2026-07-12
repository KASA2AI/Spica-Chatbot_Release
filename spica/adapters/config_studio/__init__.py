"""Operating-system adapters for the independent Config Studio sidecar."""

from .platform import current_platform_capabilities, platform_capabilities_for

__all__ = ["current_platform_capabilities", "platform_capabilities_for"]
