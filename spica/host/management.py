"""ManagementSurface (Phase 8): the settings-centre entry point.

Replaces the Phase 1 NotImplementedError placeholder. Lists registered adapters,
installed characters and loaded plugins; reads/writes typed config; and
enables/disables plugins in the manifest (which take effect on restart). Qt-free
(CLAUDE.md #1) -- a future settings UI is just a consumer of this surface.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import yaml

from spica.core.character import load_character_package
from spica.host.character_packages import import_character_folder
from spica.host.dialogue_styles import import_dialogue_style, selected_dialogue_style
from spica.plugins.manifest import DEFAULT_MANIFEST_PATH


def _removal_marker(path: Path) -> Path:
    # Keep listing state outside the immutable installation. Repairing a style
    # must not discard it; the running host may still be reading these assets.
    return path.with_name(f".{path.name}.removed")


class ManagementSurface:
    def __init__(
        self,
        *,
        registry: Any,
        config_manager: Any,
        plugin_host: Any,
        characters_root: str | Path,
        plugins_manifest_path: str | Path | None = None,
        builtin_character_dir: Path | None = None,
    ) -> None:
        self.registry = registry
        self.config_manager = config_manager
        self.plugin_host = plugin_host
        self.characters_root = Path(characters_root)
        self.builtin_character_dir = builtin_character_dir
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
        if self.builtin_character_dir is not None:
            pkg = load_character_package(self.builtin_character_dir)
            out.append({"character_id": pkg.character_id, "name": pkg.name,
                        "dir": str(self.builtin_character_dir)})
        if self.characters_root.is_dir():
            for child in sorted(self.characters_root.iterdir()):
                if (child / "meta.json").is_file():
                    pkg = load_character_package(child)
                    if pkg.manifest is None:
                        out.append({"character_id": pkg.character_id, "name": pkg.name, "dir": str(child)})
            installed = self.characters_root / "characters"
            for marker in sorted(installed.glob("*/*/.installed.json")):
                root = marker.parent
                try:
                    if (_removal_marker(root).exists()
                            or re.fullmatch(r"[a-f0-9]{64}", root.name) is None
                            or json.loads(marker.read_text(encoding="utf-8")).get("revision") != root.name
                            or not (root / ".presentation" / "manifest.json").is_file()):
                        continue
                    pkg = load_character_package(root)
                    if pkg.manifest is None or pkg.character_id != root.parent.name:
                        continue
                    out.append({"character_id": pkg.character_id, "name": pkg.name,
                                "dir": str(root), "version": pkg.manifest.version,
                                "removable": True})
                except (OSError, ValueError, AttributeError):
                    continue
        return out

    def import_character(self, source: str | Path) -> dict[str, Any]:
        package = import_character_folder(source, self.characters_root / "characters")
        self._select_imported_package(package.package_root)
        return {"character_id": package.character_id, "name": package.name,
                "dir": package.package_root, "restart_required": True,
                "message": f"{package.name} 导入完成，重启桌宠后生效。"}

    def select_character(self, package_dir: str | Path) -> dict[str, Any]:
        with self.config_manager._update_lock:
            path = Path(package_dir).resolve()
            installed = {Path(item["dir"]).resolve() for item in self.list_characters()}
            if path not in installed:
                raise ValueError("请先导入该角色文件夹。")
            package = load_character_package(path)
            self.write_config({"character": {"package_dir": str(path), "profile_override": None}})
            return {"character_id": package.character_id, "name": package.name, "restart_required": True}

    def remove_character(self, package_dir: str | Path) -> dict[str, Any]:
        return self._remove_package(package_dir)

    # -- config ---------------------------------------------------------------
    def list_dialogue_styles(self) -> list[dict[str, Any]]:
        styles = [{"name": "Spica · 原有对话框", "dir": None}]
        root = self.characters_root / "dialogue_styles"
        for path in sorted(root.glob("*/*/style.json")):
            try:
                if _removal_marker(path.parent).exists():
                    continue
                loaded = selected_dialogue_style(str(path.parent), root)
                styles.append({"name": loaded.style.name, "dir": str(loaded.root),
                               "version": loaded.style.version, "removable": True})
            except (OSError, ValueError):
                continue
        return styles

    def import_dialogue_style(self, source: str | Path) -> dict[str, Any]:
        loaded = import_dialogue_style(source, self.characters_root / "dialogue_styles")
        return self._select_imported_package(loaded.root, style=True)

    def select_dialogue_style(self, package_dir: str | None) -> dict[str, Any]:
        with self.config_manager._update_lock:
            loaded = selected_dialogue_style(package_dir, self.characters_root / "dialogue_styles")
            if loaded and _removal_marker(loaded.root).exists():
                raise ValueError("请先导入该样式文件夹。")
            self.write_config({"dialogue_style": {"package_dir": str(loaded.root) if loaded else None}})
            return {"name": loaded.style.name if loaded else "Spica · 原有对话框",
                    "restart_required": True}

    def remove_dialogue_style(self, package_dir: str | Path | None) -> dict[str, Any]:
        return self._remove_package(package_dir, style=True)

    def _managed_package_path(self, package_dir: str | Path | None, *, style: bool) -> Path:
        if not package_dir:
            raise ValueError("内置默认项不能移除。")
        path = Path(package_dir).resolve()
        base = (self.characters_root / ("dialogue_styles" if style else "characters")).resolve()
        if path.parent.parent != base or re.fullmatch(r"[a-f0-9]{64}", path.name) is None:
            raise ValueError("只能移除已导入列表中的项目。")
        return path

    def _select_imported_package(self, package_dir: str | Path, *, style: bool = False) -> dict[str, Any]:
        with self.config_manager._update_lock:
            path = self._managed_package_path(package_dir, style=style)
            marker = _removal_marker(path)
            removed = marker.exists()
            if removed:
                marker.unlink()
            try:
                return self.select_dialogue_style(str(path)) if style else self.select_character(path)
            except Exception:
                if removed:
                    marker.touch(exist_ok=False)
                raise

    def _remove_package(self, package_dir: str | Path | None, *, style: bool = False) -> dict[str, Any]:
        # Serialize selection, removal and the conditional default reset with
        # all config writes, including edits through another settings surface.
        with self.config_manager._update_lock:
            path = self._managed_package_path(package_dir, style=style)
            items = self.list_dialogue_styles() if style else self.list_characters()
            item = next((item for item in items if item.get("removable")
                         and Path(item["dir"]).resolve() == path), None)
            if item is None:
                raise ValueError("该项目不在已导入列表中。")
            section = "dialogue_style" if style else "character"
            selected = self.read_config()[section]["package_dir"]
            reset = bool(selected and Path(selected).resolve() == path)
            marker = _removal_marker(path)
            with marker.open("x"):
                pass
            try:
                if reset:
                    patch = {"package_dir": None}
                    if not style:
                        patch["profile_override"] = None
                    self.write_config({section: patch})
            except Exception:
                marker.unlink()
                raise
            message = f"已将 {item['name']} 从列表移除。"
            if reset:
                message += "重启后恢复内置 Spica。"
            return {"name": item["name"], "restart_required": reset,
                    "message": message + "需要时可重新导入。"}

    def read_config(self) -> dict[str, Any]:
        return self.config_manager.load().model_dump()

    def write_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        return self.config_manager.update(patch).model_dump()

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
