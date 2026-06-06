"""ManagementSurface (Phase 8): the settings-centre entry point.

Replaces the Phase 1 NotImplementedError placeholder. Lists registered adapters,
installed characters and loaded plugins; reads/writes typed config; and
enables/disables plugins in the manifest (which take effect on restart). Qt-free
(CLAUDE.md #1) -- a future settings UI is just a consumer of this surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from spica.core.character import load_character_package
from spica.plugins.manifest import DEFAULT_MANIFEST_PATH


class ManagementSurface:
    def __init__(
        self,
        *,
        registry: Any,
        config_manager: Any,
        plugin_host: Any,
        characters_root: str | Path,
        plugins_manifest_path: str | Path | None = None,
    ) -> None:
        self.registry = registry
        self.config_manager = config_manager
        self.plugin_host = plugin_host
        self.characters_root = Path(characters_root)
        self.plugins_manifest_path = Path(plugins_manifest_path) if plugins_manifest_path else DEFAULT_MANIFEST_PATH

    # -- listings -------------------------------------------------------------
    def list_adapters(self, kind: str) -> list[str]:
        return self.registry.list_adapters(kind)

    def list_plugins(self) -> list[str]:
        return self.plugin_host.loaded_plugins()

    def plugin_errors(self) -> dict[str, str]:
        return self.plugin_host.errors()

    def list_characters(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if self.characters_root.is_dir():
            for child in sorted(self.characters_root.iterdir()):
                if (child / "meta.json").is_file():
                    pkg = load_character_package(child)
                    out.append({"character_id": pkg.character_id, "name": pkg.name, "dir": str(child)})
        return out

    # -- config ---------------------------------------------------------------
    def read_config(self) -> dict[str, Any]:
        return self.config_manager.load().model_dump()

    def write_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.config_manager.load().model_dump()
        merged = self.config_manager.merge(current, patch)
        config = self.config_manager.validate(merged)
        self.config_manager.save(config)
        return config.model_dump()

    # -- plugins (manifest edits take effect on restart) ----------------------
    def install_plugin(self, name: str) -> None:
        self._set_plugin_enabled(name, True)

    def uninstall_plugin(self, name: str) -> None:
        self._set_plugin_enabled(name, False)

    def _set_plugin_enabled(self, name: str, enabled: bool) -> None:
        path = self.plugins_manifest_path
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(data, dict):
            data = {}
        raw = data.get("plugins") if isinstance(data.get("plugins"), list) else []
        normalized: list[dict[str, Any]] = []
        found = False
        for item in raw:
            entry = {"name": item, "enabled": True} if isinstance(item, str) else dict(item)
            if entry.get("name") == name:
                entry = {"name": name, "enabled": enabled}
                found = True
            normalized.append(entry)
        if not found:
            normalized.append({"name": name, "enabled": enabled})
        data["plugins"] = normalized
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
