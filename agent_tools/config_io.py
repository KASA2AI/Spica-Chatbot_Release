"""Config file IO supporting JSON and YAML by extension (Phase 3b).

The tts/visual config files moved from config/*.json to data/config/*.yaml.
Loaders read through here so both formats work (YAML for the consolidated configs,
JSON still supported for e.g. the diff rules file and any legacy/override path).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

_YAML_SUFFIXES = {".yaml", ".yml"}


def read_config_file(path: str | Path) -> Any:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in _YAML_SUFFIXES:
        loaded = yaml.safe_load(text)
        return loaded if loaded is not None else {}
    return json.loads(text)


def write_config_file(path: str | Path, data: Any) -> None:
    p = Path(path)
    if p.suffix.lower() in _YAML_SUFFIXES:
        p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    else:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
