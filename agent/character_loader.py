from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SPICA_SKILL_DIR = BASE_DIR / "spica_data" / "Spica_skill"
DEFAULT_INTERLOCUTOR_NAME = "麦"

INTERLOCUTOR_PROFILE_TEMPLATE = """
对话者固定身份：
- 当前和スピカ对话的人是{name}，也就是她熟悉、会斗嘴、会嘴硬关心的对象。
- 角色卡中原本属于速川麦/麦的恋爱、同居、家人、重逢等事迹，现在都映射为{name}的事迹。
- [CURRENT_USER_INPUT] 中的内容都视为{name}说的话，而不是陌生用户、开发者或旁白。
- 回复时优先按スピカ对{name}的态度反应：可以直呼「{name}」，可以冷淡、吐槽、嘴硬、害羞或用行动式关心。
- 不要把{name}称为“用户”。如果输入里出现其他自称，除非明确是在剧情内开玩笑，否则仍保持“{name}是对话者”的关系框架。
- 这条对话者设定优先于长期记忆中的可变偏好；长期记忆只能补充{name}的偏好和两人的相处细节，不能覆盖角色卡和{name}的身份。
""".strip()


def normalize_interlocutor_name(name: str | None) -> str:
    cleaned = re.sub(r"\s+", " ", name or "").strip()
    return cleaned or DEFAULT_INTERLOCUTOR_NAME


def build_interlocutor_profile(name: str | None = None) -> str:
    return INTERLOCUTOR_PROFILE_TEMPLATE.format(name=normalize_interlocutor_name(name))


def replace_mugi_references(text: str, name: str | None = None) -> str:
    replacement = normalize_interlocutor_name(name)
    if not text:
        return ""

    text = text.replace("速川麦", replacement)
    text = text.replace("Mugi", replacement)
    text = text.replace("むぎいいん", f"{replacement}ああん")
    # Replace the character name 麦, while preserving ordinary wheat words like 小麦, 麦田, 麦畑, 麦子.
    return re.sub(r"(?<!小)麦(?![子田畑克])", replacement, text)


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
    name = meta.get("name") or "辻倉朱比華"
    source = meta.get("source") or "anemoi -アネモイ-"
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
