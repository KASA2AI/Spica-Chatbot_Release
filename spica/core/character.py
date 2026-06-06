"""CharacterPackage: a portable character data package (Phase 7).

Makes the engine character-agnostic: identity, persona, sprite/voice asset
references and worldbook travel together as data, keyed by ``character_id`` (which
must align with ``MemoryScope.character_id`` so memory is isolated per character).

Phase 7a fixes the manifest + persona + identity (this file) and memory isolation.
Phase 7b wires the visual/tts asset references so VisualDiffService / TTS read
from the package. Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agent.character_compat import DEFAULT_CHARACTER_NAME


class CharacterPackage(BaseModel):
    character_id: str
    name: str = ""                       # source/display name (e.g. 辻倉朱比華)
    char_name: str = DEFAULT_CHARACTER_NAME  # in-dialogue name ({{char}}, e.g. スピカ)
    skill_dir: str | None = None         # persona source dir (role card)
    worldbook: str = ""
    # Asset references resolved by the engine in Phase 7b; relative to the package.
    visual_config_path: str | None = None
    tts_config_path: str | None = None


def load_character_package(package_dir: str | Path) -> CharacterPackage:
    """Load a character package from a directory containing ``meta.json``.

    Reuses the existing Spica role-card layout: ``meta.json`` provides the
    identity (``slug`` -> character_id, ``name``, optional ``char_name``), and the
    directory itself is the persona ``skill_dir`` (SKILL.md / self.md / persona.md).
    """
    root = Path(package_dir)
    meta = _read_json(root / "meta.json")
    character_id = str(meta.get("slug") or root.name)
    return CharacterPackage(
        character_id=character_id,
        name=str(meta.get("name") or ""),
        char_name=str(meta.get("char_name") or DEFAULT_CHARACTER_NAME),
        skill_dir=str(root),
        worldbook=str(meta.get("worldbook") or ""),
        visual_config_path=_resolve_path(root, meta.get("visual_config_path")),
        tts_config_path=_resolve_path(root, meta.get("tts_config_path")),
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _resolve_path(root: Path, value: Any) -> str | None:
    """Resolve a package asset path: absolute as-is, relative to the package dir."""
    if not value:
        return None
    path = Path(str(value))
    return str(path if path.is_absolute() else root / path)
