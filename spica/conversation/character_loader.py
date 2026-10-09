from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from spica.conversation.character_compat import (
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

# C4: moved from agent/ to spica/conversation/, one level deeper, so the repo
# root is parents[2] (conversation -> spica -> root) not parents[1]. The resolved
# path is unchanged -- this preserves DEFAULT_SPICA_SKILL_DIR exactly.
BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SPICA_SKILL_DIR = BASE_DIR / "spica_data" / "Spica_skill"
_RUNTIME_PREFIX = '# SPICA_RUNTIME_MATERIAL v1\n'

# Generic template: {{char}} = the character, {{user}} = the interlocutor.
# No character-specific literals live here -- the Spica values come from
# character_compat and are substituted in at build time.
INTERLOCUTOR_PROFILE_TEMPLATE = """
对话者固定身份：
- 当前和{{char}}对话的人是{{user}}。两人的亲疏、关系与相处方式以当前角色卡的明确设定为准。
- 角色卡中的原作经历用于理解人物背景，不自动成为{{user}}在现实中经历或完成过的事情。
- [CURRENT_USER_INPUT] 中的内容都视为{{user}}说的话，而不是陌生用户、开发者或旁白。
- 回复时按{{char}}对{{user}}的关系和当前交流自然反应，语气由角色资料决定，需要称呼时可以直呼「{{user}}」。
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
    if meta.get('runtime_prompt_file'):
        from spica.core.character_manifest import read_runtime_prompt
        material = read_runtime_prompt(root, meta)
        name = normalize_interlocutor_name(interlocutor_name)
        def render(text):
            for alias in sorted(meta.get('user_aliases', []), key=len, reverse=True):
                text = text.replace(alias, name)
            return render_character_template(text, char=meta.get('char_name') or DEFAULT_CHARACTER_NAME, user=name)
        material['core'] = render(material['core'])
        for item in material['expressions']:
            item['text'] = render(item['text'])
            item['triggers'] = [render(trigger) for trigger in item['triggers']]
        for item in material['background']:
            # Story participants keep their author names. Replacing 新吾 with
            # today's owner turns fictional history into apparent shared life.
            item['text'] = render_character_template(item['text'],
                char=meta.get('char_name') or DEFAULT_CHARACTER_NAME, user=name)
        return _RUNTIME_PREFIX + json.dumps(material, ensure_ascii=False, separators=(',', ':'))
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

    if meta.get("pack_format") in (1, 2, 3):
        from spica.core.character_manifest import CharacterManifest, package_file

        manifest = CharacterManifest.model_validate(meta)
        if manifest.worldbook_file:
            parts.append("# Worldbook\n" + _read_text(package_file(root, manifest.worldbook_file)))
        profile = "\n\n".join(parts).strip()
        name = normalize_interlocutor_name(interlocutor_name)
        for alias in sorted(manifest.user_aliases, key=len, reverse=True):
            profile = profile.replace(alias, name)
        return render_character_template(profile, char=manifest.char_name, user=name)
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
        prefix = '# Author Profile Override\n' if profile_override.startswith(_RUNTIME_PREFIX) else ''
        return prefix + replace_mugi_references(profile_override, name)
    root = Path(skill_dir) if skill_dir else DEFAULT_SPICA_SKILL_DIR
    if not root.is_absolute():
        root = BASE_DIR / root
    return load_spica_character_profile(root, interlocutor_name=name) or ""


def select_character_material(profile: str, query: str, *, background_budget: int = 4000, scene: str = '') -> tuple[str, str]:
    """Separate loader-authored section boundaries before model composition.

    Freeform profile overrides remain author instructions. Known Self Memory
    and Worldbook sections are narrative sources, selected as whole passages.
    """
    if profile.startswith(_RUNTIME_PREFIX):
        material = json.loads(profile[len(_RUNTIME_PREFIX):])
        return _select_runtime_material(material, query, scene, background_budget)
    sections = re.split(r"(?m)^# (Role Card Meta|Role Card|Self Memory|Persona|Worldbook)\s*\n", profile)
    core = [sections[0]] if sections[0].strip() else []
    background = []
    examples = []
    for title, content in zip(sections[1::2], sections[2::2]):
        if title == "Persona":
            # Situation-specific example replies do not become unconditional
            # personality instructions (e.g. every closure sounding like bedtime).
            blocks = re.split(r"(?m)(^#{1,6}[^\n]*\n)", content)
            kept, example_level, situation_level = [blocks[0]], None, None
            example_text = []
            for heading, body in zip(blocks[1::2], blocks[2::2]):
                level = len(heading) - len(heading.lstrip("#"))
                if re.search(r"示例|例文|Examples", heading, re.IGNORECASE):
                    example_level = level
                elif example_level is not None and level <= example_level:
                    example_level = None
                if situation_level is not None and level <= situation_level:
                    situation_level = None
                if re.search(r'情绪与亲密|面对不同的人|深层内核|对话中的主动性|原文的表达锚点', heading):
                    situation_level = level
                if example_level is not None:
                    example_text.append(body)
                elif situation_level is not None:
                    # These authored sections describe particular situations,
                    # relationships and example utterances. Keep them available
                    # as expression references without prompting every chat to
                    # enact all of them (care, jealousy, comfort, invitations).
                    if body.strip():
                        background.append(heading + body)
                else:
                    kept.append(heading + body)
            for text in example_text:
                examples.extend(part.strip() for part in re.split(r"(?m)(?=^\*\*(?:对方|本人|用户)[：:])", text) if part.strip())
            content = "".join(kept)
        if title in {"Self Memory", "Worldbook"}:
            headings: list[tuple[int, str]] = []
            for part in re.split(r"\n\s*\n", content):
                part = part.strip()
                if not part:
                    continue
                heading = re.fullmatch(r"(#{1,6})\s+([^\n]+)", part)
                if heading:
                    level = len(heading[1])
                    headings = [(depth, text) for depth, text in headings if depth < level]
                    headings.append((level, part))
                else:
                    background.append("\n".join([*(text for _, text in headings), part]))
        else:
            core.append(f"# {title}\n{content.strip()}")
    tokens = set(re.findall(r"[a-zA-Z]{3,}", query.lower()))
    for span in re.findall(r"[\u3040-\u30ff\u3400-\u9fff]+", query):
        tokens.update(span[i:i + 2] for i in range(len(span) - 1)
                      if not any(char in "的了着在是我你他她它这那有也就和与都吧吗呢" for char in span[i:i + 2]))
    tokens -= {"今天", "现在", "最近", "这样", "什么", "刚才", "觉得", "一下", "一点", "的话", "我想", "今日", "こと", "完了", "好了", "了吧"}
    example_tokens = tokens
    for example in examples:
        trigger = example.split("\n", 1)[0]
        if any(token in trigger.lower() for token in example_tokens):
            background.append("[STYLE_EXAMPLE — 只用于对应情境的表达参考]\n" + example)
    # Words repeated throughout a biography (e.g. 喜欢) do not identify the
    # current subject. Otherwise an ordinary preference selects nearly every
    # relationship passage, repeatedly prompting care and invitations.
    tokens = {token for token in tokens
              if sum(token in part.lower() for part in background) < max(3, len(background) * .2)}
    broad = bool(re.search(r"原作|角色背景|世界观|人物经历|身世|你的过去|原作|生い立ち", query))
    ranked = sorted(((sum(token in part.lower() for token in tokens), i, part)
                     for i, part in enumerate(background)), key=lambda value: (-value[0], value[1]))
    chosen, used = [], 0
    for score, index, passage in ranked:
        if not score and not broad:
            continue
        if used + len(passage) > background_budget:
            continue
        chosen.append((index, passage))
        used += len(passage)
    return "\n\n".join(core).strip(), "\n\n".join(part for _, part in sorted(chosen))


def _select_runtime_material(material, query, scene, background_budget):
    home = scene in {'home.wake', 'home.wake_finished', 'home.welcome', 'home.farewell', 'home.bedtime'}
    def tokens(text):
        return sum(1 if ord(char) > 127 else .25 for char in text)
    expressions, used = [], 0
    allowance = max(0, background_budget)
    for item in material['expressions']:
        if scene not in item['scenes'] and not any(trigger.lower() in query.lower() for trigger in item['triggers']):
            continue
        reference = '[对应情境表达参考，不是现实经历；仅在本轮确有该交流意图时参考]\n' + item['text']
        size = tokens(reference)
        if used + size <= min(allowance, 200 if home else 300):
            expressions.append(reference)
            used += size
    broad = bool(re.search(r'原作|角色背景|世界观|人物经历|身世|你的过去|生い立ち', query))
    budget = min(max(0, allowance-used), 1600 if broad else 600) if query.strip() else 0
    background, used = [], 0
    ranked = sorted(enumerate(material['background']), key=lambda pair: (
        -sum(trigger.lower() in query.lower() for trigger in pair[1]['triggers']), pair[0]))
    for index, item in ranked:
        if not broad and not any(trigger.lower() in query.lower() for trigger in item['triggers']):
            continue
        size = tokens(item['text'])
        if used + size <= budget:
            background.append((index, item['text']))
            used += size
    return material['core'], '\n\n'.join([*expressions, *(text for _, text in sorted(background))])


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
    source = meta.get("source") or ("原创角色" if meta.get("pack_format") in (1, 2, 3) else SPICA_META_DEFAULTS["source"])
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
