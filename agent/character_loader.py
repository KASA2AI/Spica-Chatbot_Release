from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agent.character_compat import (
    DEFAULT_CHARACTER_NAME,
    DEFAULT_INTERLOCUTOR_NAME,
    SPICA_META_DEFAULTS,
    replace_mugi_references,
)

# Re-exported so existing callers/tests keep importing these from here.
__all__ = [
    "DEFAULT_CHARACTER_NAME",
    "DEFAULT_INTERLOCUTOR_NAME",
    "DEFAULT_SPICA_SKILL_DIR",
    "INTERLOCUTOR_PROFILE_TEMPLATE",
    "build_character_profile",
    "build_interlocutor_profile",
    "load_spica_character_profile",
    "normalize_interlocutor_name",
    "render_character_template",
    "replace_mugi_references",
]

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SPICA_SKILL_DIR = BASE_DIR / "spica_data" / "Spica_skill"

# Generic template: {{char}} = the character, {{user}} = the interlocutor.
# No character-specific literals live here -- the Spica values come from
# character_compat and are substituted in at build time.
INTERLOCUTOR_PROFILE_TEMPLATE = """
对话者固定身份：
- 当前和{{char}}对话的人是{{user}}，也就是她熟悉、会斗嘴、会嘴硬关心的对象。
- 角色卡中原本属于{{user}}的恋爱、同居、家人、重逢等事迹，现在都映射为{{user}}的事迹。
- [CURRENT_USER_INPUT] 中的内容都视为{{user}}说的话，而不是陌生用户、开发者或旁白。
- 回复时优先按{{char}}对{{user}}的态度反应：可以直呼「{{user}}」，可以冷淡、吐槽、嘴硬、害羞或用行动式关心。
- 不要把{{user}}称为"用户"。如果输入里出现其他自称，除非明确是在剧情内开玩笑，否则仍保持"{{user}}是对话者"的关系框架。
- 这条对话者设定优先于长期记忆中的可变偏好；长期记忆只能补充{{user}}的偏好和两人的相处细节，不能覆盖角色卡和{{user}}的身份。
""".strip()


def normalize_interlocutor_name(name: str | None) -> str:
    cleaned = re.sub(r"\s+", " ", name or "").strip()
    return cleaned or DEFAULT_INTERLOCUTOR_NAME


def render_character_template(text: str, *, char: str, user: str) -> str:
    """Substitute the generic {{char}} / {{user}} placeholders."""
    if not text:
        return text
    return text.replace("{{char}}", char).replace("{{user}}", user)


def build_interlocutor_profile(
    name: str | None = None,
    character_name: str = DEFAULT_CHARACTER_NAME,
) -> str:
    return render_character_template(
        INTERLOCUTOR_PROFILE_TEMPLATE,
        char=character_name or DEFAULT_CHARACTER_NAME,
        user=normalize_interlocutor_name(name),
    )


def load_spica_character_profile(skill_dir: str | Path | None = None, interlocutor_name: str | None = None) -> str:
    """Load the local Spica role card into one prompt-ready profile string."""
    root = Path(skill_dir) if skill_dir else DEFAULT_SPICA_SKILL_DIR
    if not root.exists():
        return ""

    parts: list[str] = []
    meta = _read_json(root / "meta.json")
    if meta:
        parts.append(_format_meta(meta))

    for filename, title in (
        ("SKILL.md", "Role Card"),
        ("self.md", "Self Memory"),
        ("persona.md", "Persona"),
    ):
        text = _read_text(root / filename)
        if text:
            parts.append(f"# {title}\n{text}")

    return replace_mugi_references("\n\n".join(parts).strip(), interlocutor_name)


def build_character_profile(
    profile_override: str | None,
    skill_dir: str | Path | None,
    interlocutor_name: str | None = None,
) -> str:
    """Assemble the prompt-ready character profile (Phase 6D, moved from SimpleAgent).

    Used both at assembly time and when the interlocutor name changes. An explicit
    override wins; otherwise the local Spica role card is loaded from ``skill_dir``.
    """
    name = normalize_interlocutor_name(interlocutor_name)
    if profile_override:
        return replace_mugi_references(profile_override, name)
    root = Path(skill_dir) if skill_dir else DEFAULT_SPICA_SKILL_DIR
    if not root.is_absolute():
        root = BASE_DIR / root
    return load_spica_character_profile(root, interlocutor_name=name) or ""


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _format_meta(meta: dict[str, Any]) -> str:
    name = meta.get("name") or SPICA_META_DEFAULTS["name"]
    source = meta.get("source") or SPICA_META_DEFAULTS["source"]
    impression = meta.get("impression") or ""
    profile = meta.get("profile") if isinstance(meta.get("profile"), dict) else {}
    tags = meta.get("tags") if isinstance(meta.get("tags"), dict) else {}

    lines = [
        "# Role Card Meta",
        f"- name: {name}",
        f"- source: {source}",
    ]
    for key in ("height", "weight", "birthday", "gender", "role"):
        value = profile.get(key)
        if value:
            lines.append(f"- {key}: {value}")
    if impression:
        lines.append(f"- impression: {impression}")
    for key, values in tags.items():
        if isinstance(values, list) and values:
            lines.append(f"- {key}: {', '.join(str(value) for value in values)}")
    return "\n".join(lines)
