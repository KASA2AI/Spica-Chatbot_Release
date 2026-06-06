"""PluginHost (Phase 8): load external plugin packages and let them register
capabilities into the CapabilityRegistry.

A plugin is a directory ``plugins/<name>/`` whose ``__init__.py`` exposes
``register(registry)``; it may register adapters / tools (no UI widgets in this
phase). Loaded by file path (no sys.path coupling), so this is decoupled and
test-friendly. Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from spica.plugins.manifest import PluginEntry, load_plugin_manifest

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PLUGINS_ROOT = _REPO_ROOT / "plugins"


class PluginHost:
    def __init__(
        self,
        registry: Any,
        *,
        plugins_root: str | Path | None = None,
        manifest_path: str | Path | None = None,
    ) -> None:
        self.registry = registry
        self.plugins_root = Path(plugins_root) if plugins_root else DEFAULT_PLUGINS_ROOT
        self.manifest_path = manifest_path
        self._loaded: list[str] = []
        self._errors: dict[str, str] = {}

    def load(self) -> None:
        """Import each enabled plugin and call its ``register(registry)``.

        A failing plugin is recorded in ``errors()`` and skipped -- one bad
        plugin must not break startup.
        """
        self._loaded = []
        self._errors = {}
        for entry in load_plugin_manifest(self.manifest_path):
            if not entry.enabled:
                continue
            try:
                self._load_one(entry)
                self._loaded.append(entry.name)
            except Exception as exc:
                self._errors[entry.name] = str(exc)

    def _load_one(self, entry: PluginEntry) -> None:
        init_path = self.plugins_root / entry.name / "__init__.py"
        if not init_path.is_file():
            raise FileNotFoundError(f"plugin package not found: {init_path}")
        spec = importlib.util.spec_from_file_location(f"spica_plugin_{entry.name}", init_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load plugin {entry.name!r}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        register = getattr(module, "register", None)
        if not callable(register):
            raise RuntimeError(f"plugin {entry.name!r} has no register(registry) function")
        register(self.registry)

    def loaded_plugins(self) -> list[str]:
        return list(self._loaded)

    def errors(self) -> dict[str, str]:
        return dict(self._errors)
