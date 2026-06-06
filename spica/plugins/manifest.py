"""Plugin manifest (Phase 8).

Reads ``data/config/plugins.yaml`` listing which plugin packages to load:

    plugins:
      - name: example_tts
        enabled: true
      - other_plugin           # shorthand: enabled by default

Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST_PATH = _REPO_ROOT / "data" / "config" / "plugins.yaml"


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
