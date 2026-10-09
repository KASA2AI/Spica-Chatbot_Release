"""PluginHost (Phase 8): load external plugin packages and let them register
capabilities into the CapabilityRegistry.

A plugin is a directory ``plugins/<name>/`` whose ``__init__.py`` exposes
``register(registry)``; it may register adapters / tools (no UI widgets in this
phase). Loaded by file path (no sys.path coupling), so this is decoupled and
test-friendly. Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from spica.plugins.manifest import (
    PluginEntry,
    load_plugin_manifest,
    resolve_effective_plugin_entries,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PLUGINS_ROOT = _REPO_ROOT / "plugins"


def _unload_package(module_name: str) -> None:
    # The namespace belongs to one load attempt, including lazy submodules.
    # Shared third-party imports remain owned by Python's normal import cache.
    for name in tuple(sys.modules):
        if name == module_name or name.startswith(module_name + "."):
            sys.modules.pop(name, None)


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
        self._features: dict[str, Any] = {}
        self._cleanup = []
        self._loaded_packages: list[str] = []
        self._closed = False
        self._external_loaded = False

    def load(self) -> None:
        """Import each enabled plugin and call its ``register(registry)``.

        A failing plugin is recorded in ``errors()`` and skipped -- one bad
        plugin must not break startup.
        """
        if self._external_loaded or self._closed:
            return
        self._external_loaded = True
        # P0b step 3 (D6): an explicit manifest_path (tests/tools) keeps the old
        # loader; the production default (None) goes through the carrier switch
        # (legacy plugins.yaml entirely, or app.yaml's plugins section).
        try:
            entries = (
                load_plugin_manifest(self.manifest_path)
                if self.manifest_path
                else resolve_effective_plugin_entries()
            )
        except Exception as exc:
            self._errors['manifest'] = str(exc)
            return
        for entry in entries:
            if not entry.enabled:
                continue
            try:
                with self.registry.registration():
                    cleanup = self._load_one(entry)
                if callable(cleanup):
                    self._cleanup.append((entry.name, cleanup))
                self._loaded.append(entry.name)
            except Exception as exc:
                self._errors[entry.name] = str(exc)

    def _load_one(self, entry: PluginEntry) -> Any:
        init_path = self.plugins_root / entry.name / "__init__.py"
        if not init_path.is_file():
            raise FileNotFoundError(f"plugin package not found: {init_path}")
        # Each host previously executed its own module. Keep that isolation for
        # package submodules too; two hosts must not replace each other's cache.
        module_name = f"spica_plugin_{entry.name}_{uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, init_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load plugin {entry.name!r}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            register = getattr(module, "register", None)
            if not callable(register):
                raise RuntimeError(f"plugin {entry.name!r} has no register(registry) function")
            cleanup = register(self.registry)
        except BaseException:
            _unload_package(module_name)
            raise
        self._loaded_packages.append(module_name)
        return cleanup

    def activate(self, name, start, *, close=None):
        """Assemble one optional builtin using the same registry and error seam.

        No hot reload: existing typed feature config takes effect on core restart.
        Initializers must clean partial resources if construction raises. A
        successful resource can supply a close callback owned by this host.
        """
        if self._closed:
            raise RuntimeError('feature host is closed')
        if name in self._features:
            return self._features[name]
        try:
            with self.registry.registration():
                resource = start()
            self._features[name] = resource
            if close is not None:
                self._cleanup.append(('builtin:' + name, lambda: close(resource)))
            return resource
        except Exception as exc:
            self._errors['builtin:' + name] = str(exc)
            return None

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        for name, cleanup in reversed(self._cleanup):
            try:
                cleanup()
            except Exception as exc:
                self._errors[name] = 'shutdown: ' + str(exc)
        self._cleanup.clear()
        # Cleanup callbacks can still import their own package. Unload only
        # after they finish, leaving every other host's namespace intact.
        for module_name in self._loaded_packages:
            _unload_package(module_name)
        self._loaded_packages.clear()

    def loaded_plugins(self) -> list[str]:
        return list(self._loaded)

    def errors(self) -> dict[str, str]:
        return dict(self._errors)
