"""Plugin manifest (Phase 8).

Reads ``data/config/plugins.yaml`` listing which plugin packages to load:

    plugins:
      - name: example_tts
        enabled: true
      - other_plugin           # shorthand: enabled by default

Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST_PATH = _REPO_ROOT / "data" / "config" / "plugins.yaml"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PluginEntry:
    name: str
    enabled: bool = True


def load_plugin_manifest(path: str | Path | None = None) -> list[PluginEntry]:
    manifest_path = Path(path) if path else DEFAULT_MANIFEST_PATH
    if not manifest_path.is_file():
        return []
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    raw = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    entries: list[PluginEntry] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            entries.append(PluginEntry(name=item.strip()))
        elif isinstance(item, dict) and item.get("name"):
            entries.append(PluginEntry(name=str(item["name"]), enabled=bool(item.get("enabled", True))))
    return entries


def resolve_effective_plugin_entries(
    config: Any | None = None,
    legacy_path: str | Path | None = None,
) -> list[PluginEntry]:
    """P0b step 3 (D6): carrier switch for the plugin manifest -- one WHOLE
    chain by legacy-file existence (plugins.yaml present -> old loader +
    WARNING; absent -> AppConfig.plugins typed entries from app.yaml)."""
    manifest_path = Path(legacy_path) if legacy_path else DEFAULT_MANIFEST_PATH
    if manifest_path.is_file():
        logger.warning(
            "plugin 清单仍由旧载体 %s 生效；已迁移至 data/config/app.yaml 的 plugins 节，"
            "请运行 scripts/migrate_config_p0b.py，下一版本停读旧 yaml",
            manifest_path,
        )
        return load_plugin_manifest(manifest_path)
    if config is None:
        from spica.config.manager import ConfigManager

        config = ConfigManager().load()
    return [PluginEntry(name=entry.name, enabled=entry.enabled) for entry in config.plugins]
